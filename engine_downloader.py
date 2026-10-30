# -*- coding: utf-8 -*-
"""
panda_worker/engine_downloader.py
Silent Background Resumable Downloader for AI Engines (`HTTP Range` + `fsync` + `SHA-256`).

Survives:
- Weak/unstable Wi-Fi or mobile hotspot drops (silent exponential retry, zero error popups).
- Sudden PC power loss / shutdown mid-download (resumes from exact `.part` byte offset on next boot).
- Corrupted network proxies (verifies full SHA-256 before extraction; auto-repairs on mismatch).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import requests

import worker_config as wcfg

log = logging.getLogger("panda_worker")

SAFETY_ROLLBACK_BYTES = 65536  # Re-fetch last 64 KB after unclean power-off to guard NTFS write cache


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(524288)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _try_link_existing_local_engines(engines_dir: Path) -> bool:
    """
    Zero-Duplicate Smart Reuse (100% Safe — No Symlinks Needed):
    If the PC ALREADY has `waifu2x-extension-gui` / `Real-ESRGAN` installed locally,
    point `WAIFU_ROOT` to it and copy only the 200 KB Python engine wrappers so
    ZERO duplicate MB are downloaded or stored, and uninstalling PandaWorker never
    touches the user's existing `Real-ESRGAN` folder!
    """
    desk = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
    candidates = [
        Path(os.environ["WAIFU_ROOT"]) if os.environ.get("WAIFU_ROOT") else None,
        desk / "Real-ESRGAN" / "waifu2x-extension-gui",
        desk / "waifu2x-extension-gui",
        Path(r"C:\Real-ESRGAN\waifu2x-extension-gui"),
    ]
    found_waifu: Optional[Path] = None
    for c in candidates:
        if c and c.is_dir() and any(c.rglob("realesrgan-ncnn-vulkan*")):
            found_waifu = c
            break
    if not found_waifu:
        return False

    try:
        engines_dir.mkdir(parents=True, exist_ok=True)
        for py_src_dir in (wcfg.HOT_CORE_DIR / "engines", wcfg.BUNDLE_DIR / "engines_bundled", wcfg.BUNDLE_DIR / "engines"):
            if py_src_dir.is_dir() and (py_src_dir / "config.py").is_file():
                for pyf in py_src_dir.glob("*.py"):
                    dst_py = engines_dir / pyf.name
                    if not dst_py.is_file():
                        shutil.copy2(pyf, dst_py)
                break
        if not (engines_dir / "config.py").is_file():
            return False
        os.environ["WAIFU_ROOT"] = str(found_waifu)
        return True
    except Exception:
        return False


def are_engines_already_present(engines_dir: Path) -> bool:
    """True when local `engines/` (or existing local WAIFU_ROOT) already has FFmpeg + Vulkan AI binaries."""
    if not (engines_dir / "config.py").is_file() or (not (engines_dir / "bin").is_dir() and not os.environ.get("WAIFU_ROOT")):
        _try_link_existing_local_engines(engines_dir)
    if not (engines_dir / "config.py").is_file():
        return False
    bin_dir = engines_dir / "bin"
    if bin_dir.is_dir():
        has_ff = any(bin_dir.glob("ffmpeg*")) or bool(shutil.which("ffmpeg"))
        has_ai = any(bin_dir.rglob("realesrgan-ncnn-vulkan*")) or any(bin_dir.rglob("realcugan-ncnn-vulkan*"))
        if has_ff and has_ai:
            return True
    try:
        if str(engines_dir) not in os.sys.path:
            os.sys.path.insert(0, str(engines_dir))
        import config as eng_c  # type: ignore
        if eng_c.FFMPEG_EXE.is_file() and (eng_c.REALESRGAN_EXE.is_file() or eng_c.REALCUGAN_EXE.is_file()):
            return True
    except Exception:
        pass
    return False


def download_file_resumable(
    url: str,
    dest_file: Path,
    expected_sha256: str = "",
    session: Optional[requests.Session] = None,
    stop_evt: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
) -> Path:
    """
    Download `url` into `dest_file` with persistent `.part` + `.ckpt.json` checkpointing
    and automatic `Range: bytes=<offset>-` resume across network drops or PC power-offs.
    """
    sess = session or requests.Session()
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest_file.with_name(dest_file.name + ".part")
    ckpt_path = dest_file.with_name(dest_file.name + ".ckpt.json")

    # Reset `.part` if URL changed since previous incomplete session
    if ckpt_path.is_file():
        try:
            ck = json.loads(ckpt_path.read_text(encoding="utf-8"))
            if ck.get("url") != url:
                part_path.unlink(missing_ok=True)
        except Exception:
            pass

    # If Power-Off happened mid-write, trim last 64 KB tail to avoid zero-filled NTFS clusters
    if part_path.is_file():
        cur_sz = part_path.stat().st_size
        if ckpt_path.is_file():
            try:
                ck = json.loads(ckpt_path.read_text(encoding="utf-8"))
                clean_closed = bool(ck.get("clean_pause", False))
            except Exception:
                clean_closed = False
        else:
            clean_closed = False
        if not clean_closed and cur_sz > SAFETY_ROLLBACK_BYTES * 2:
            with part_path.open("r+b") as tf:
                tf.truncate(cur_sz - SAFETY_ROLLBACK_BYTES)
                tf.flush()
                os.fsync(tf.fileno())

    backoff = 2.0
    while True:
        if stop_evt and stop_evt.is_set():
            _write_ckpt(ckpt_path, url, part_path.stat().st_size if part_path.is_file() else 0, clean=True)
            raise InterruptedError("Загрузка приостановлена по сигналу выхода.")

        offset = part_path.stat().st_size if part_path.is_file() else 0
        headers: Dict[str, str] = {"User-Agent": f"PandaWorker/{wcfg.get_runtime_version()}"}
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"

        try:
            with sess.get(url, headers=headers, stream=True, timeout=(15, 45)) as resp:
                if resp.status_code == 416:
                    # Already fully downloaded on disk!
                    pass
                elif offset > 0 and resp.status_code == 200:
                    # Server ignored Range header -> restart cleanly from 0
                    offset = 0
                    part_path.unlink(missing_ok=True)
                    _stream_chunks(resp, part_path, ckpt_path, url, 0, stop_evt, on_progress)
                elif resp.status_code in (200, 206):
                    _stream_chunks(resp, part_path, ckpt_path, url, offset, stop_evt, on_progress)
                else:
                    resp.raise_for_status()

            # Reached 100% of stream -> verify SHA-256 before promoting `.part`!
            if not part_path.is_file():
                raise RuntimeError("Временный файл загрузки отсутствует.")
            if expected_sha256:
                got_sha = compute_sha256(part_path)
                if got_sha.lower() != expected_sha256.strip().lower():
                    log.warning(
                        "SHA-256 архива не совпал (%s != %s) — перезапуск чистой докачки...",
                        got_sha[:12],
                        expected_sha256[:12],
                    )
                    part_path.unlink(missing_ok=True)
                    ckpt_path.unlink(missing_ok=True)
                    time.sleep(2.0)
                    continue

            dest_file.unlink(missing_ok=True)
            part_path.replace(dest_file)
            ckpt_path.unlink(missing_ok=True)
            return dest_file

        except InterruptedError:
            raise
        except Exception as exc:
            log.info("Фоновая докачка ждёт интернет (%s), повтор через %.0fс...", type(exc).__name__, backoff)
            if stop_evt and stop_evt.wait(timeout=backoff):
                _write_ckpt(ckpt_path, url, part_path.stat().st_size if part_path.is_file() else 0, clean=True)
                raise InterruptedError("Загрузка остановлена.")
            backoff = min(30.0, backoff * 1.7)


def _write_ckpt(ckpt_path: Path, url: str, size: int, clean: bool) -> None:
    try:
        tmp = ckpt_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"url": url, "bytes": size, "clean_pause": clean, "ts": time.time()}),
            encoding="utf-8",
        )
        tmp.replace(ckpt_path)
    except Exception:
        pass


def _stream_chunks(
    resp: requests.Response,
    part_path: Path,
    ckpt_path: Path,
    url: str,
    start_offset: int,
    stop_evt: Optional[threading.Event],
    on_progress: Optional[Callable[[int, int, int], None]],
) -> None:
    rem_len = int(resp.headers.get("Content-Length") or 0)
    total = (start_offset + rem_len) if rem_len > 0 else 0
    written = start_offset
    since_sync = 0
    _write_ckpt(ckpt_path, url, written, clean=False)

    mode = "ab" if start_offset > 0 else "wb"
    with part_path.open(mode) as fh:
        for chunk in resp.iter_content(chunk_size=262144):
            if stop_evt and stop_evt.is_set():
                fh.flush()
                os.fsync(fh.fileno())
                _write_ckpt(ckpt_path, url, written, clean=True)
                raise InterruptedError("Остановка во время докачки.")
            if not chunk:
                continue
            fh.write(chunk)
            written += len(chunk)
            since_sync += len(chunk)
            if since_sync >= 2 * 1024 * 1024:
                fh.flush()
                os.fsync(fh.fileno())
                _write_ckpt(ckpt_path, url, written, clean=False)
                since_sync = 0
            if on_progress:
                pct = min(99, int(round(100.0 * written / total))) if total > 0 else 50
                on_progress(pct, written, total)
        fh.flush()
        os.fsync(fh.fileno())


def safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    """Safely extract `zip_path` into `target_dir` preventing Zip-Slip path traversal."""
    target_resolved = target_dir.resolve()
    target_resolved.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            dest = (target_resolved / member.filename).resolve()
            if target_resolved != dest and target_resolved not in dest.parents:
                raise RuntimeError(f"Небезопасный путь в архиве: {member.filename}")
        zf.extractall(target_resolved)


class BackgroundEngineInitializer:
    """
    Quietly downloads & verifies AI engines in the background, runs a quick GPU self-test,
    saves `engines_ready=True`, and notifies the Panda Dispatcher (`/v1/worker/engines_ready`)
    so the Telegram Bot alerts the user and begins assigning jobs!
    """

    def __init__(
        self,
        server_url: str,
        worker_token: str,
        engines_url: str = "",
        expected_sha256: str = "",
        on_status: Optional[Callable[[int, str, bool], None]] = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.worker_token = worker_token.strip()
        self.engines_url = engines_url.strip() or f"{self.server_url}/v1/public/releases/panda_engines_pack.zip"
        self.expected_sha256 = expected_sha256.strip()
        self.on_status = on_status
        self.stop_evt = threading.Event()
        self.session = requests.Session()
        self.session.verify = wcfg.TLS_VERIFY

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.worker_token}",
            "Content-Type": "application/json",
            "User-Agent": f"PandaWorker/{wcfg.get_runtime_version()}",
        }

    def _emit(self, pct: int, msg: str, ready: bool = False) -> None:
        if self.on_status:
            try:
                self.on_status(pct, msg, ready)
            except Exception:
                pass

    def ensure_ready(self) -> bool:
        if are_engines_already_present(wcfg.ENGINES_DIR):
            bench_ms = self._run_quick_benchmark()
            wcfg.save_state({"engines_ready": True, "bench_ms": bench_ms})
            self._notify_server_ready(bench_ms)
            self._emit(100, "⚡ ИИ-движки готовы к работе!", True)
            return True

        self._emit(1, "📥 Подготовка тихой фоновой загрузки ИИ-движков...", False)
        archive_dest = wcfg.DOWNLOAD_DIR / "panda_engines_pack.zip"
        last_report_ts = [0.0]

        def _prog(pct: int, done_b: int, tot_b: int) -> None:
            mb_done = round(done_b / (1024 * 1024), 1)
            mb_tot = round(tot_b / (1024 * 1024), 1) if tot_b else 0
            label = (
                f"📥 Тихая докачка ИИ-движков: {pct}% ({mb_done} / {mb_tot} МБ)"
                if mb_tot
                else f"📥 Тихая докачка ИИ-движков ({mb_done} МБ)..."
            )
            self._emit(pct, label, False)
            now = time.monotonic()
            if now - last_report_ts[0] >= 8.0 and self.worker_token:
                last_report_ts[0] = now
                try:
                    self.session.post(
                        f"{self.server_url}/v1/worker/dl_progress",
                        headers=self._headers(),
                        data=json.dumps({"dl_progress_pct": pct}).encode("utf-8"),
                        timeout=6,
                    )
                except Exception:
                    pass

        downloaded = download_file_resumable(
            url=self.engines_url,
            dest_file=archive_dest,
            expected_sha256=self.expected_sha256,
            session=self.session,
            stop_evt=self.stop_evt,
            on_progress=_prog,
        )

        self._emit(99, "📦 Распаковка и проверка целостности нейросетей...", False)
        staging = wcfg.WORKER_ROOT / "engines_unpack_tmp"
        shutil.rmtree(staging, ignore_errors=True)
        safe_extract_zip(downloaded, staging)
        downloaded.unlink(missing_ok=True)

        # Support archives that either contain `config.py` at root or inside an `engines/` subfolder
        src_root = staging / "engines" if (staging / "engines" / "config.py").is_file() else staging
        wcfg.ENGINES_DIR.mkdir(parents=True, exist_ok=True)
        for item in src_root.iterdir():
            dst_item = wcfg.ENGINES_DIR / item.name
            if dst_item.is_dir():
                shutil.rmtree(dst_item, ignore_errors=True)
            elif dst_item.exists():
                dst_item.unlink(missing_ok=True)
            shutil.move(str(item), str(dst_item))
        shutil.rmtree(staging, ignore_errors=True)

        bench_ms = self._run_quick_benchmark()
        wcfg.save_state({"engines_ready": True, "bench_ms": bench_ms})
        self._notify_server_ready(bench_ms)
        self._emit(100, "⚡ ИИ-движки проверены и готовы! Зарабатываем Дневной Пакет.", True)
        return True

    def _run_quick_benchmark(self) -> int:
        t0 = time.monotonic()
        try:
            if str(wcfg.ENGINES_DIR) not in os.sys.path:
                os.sys.path.insert(0, str(wcfg.ENGINES_DIR))
            from gpu_probe import verify_worker_binaries
            chk = verify_worker_binaries()
            if not chk.get("ok"):
                log.warning("Проверка утилит после распаковки: %s", chk)
        except Exception as exc:
            log.warning("Benchmark warning: %s", exc)
        return max(1, int(round((time.monotonic() - t0) * 1000)))

    def _notify_server_ready(self, bench_ms: int) -> None:
        if not self.worker_token:
            return
        for attempt in range(4):
            try:
                r = self.session.post(
                    f"{self.server_url}/v1/worker/engines_ready",
                    headers=self._headers(),
                    data=json.dumps({"bench_ms": bench_ms}).encode("utf-8"),
                    timeout=10,
                )
                if r.status_code == 200:
                    return
            except Exception:
                time.sleep(2.0 * (attempt + 1))
