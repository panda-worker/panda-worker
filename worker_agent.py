# -*- coding: utf-8 -*-
"""
panda_worker/worker_agent.py
Outbound-only GPU Worker Agent v2.0 for Panda AI (Trusted Cluster + Volunteer GPU Helper).
- Initiates all HTTP/HTTPS connections itself (works behind NAT, Carrier-Grade NAT, Wi-Fi, Tailscale).
- Opens ZERO listening TCP/UDP ports and stores ZERO server secrets.
- Supports silent background AI engine downloading, live Load Modes (Light / Medium / Turbo / Smart Auto),
  automatic signed Hot-Updates without reinstalling `.exe`, and immediate Zero-Trace cleanup.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import shutil
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

import worker_config as wcfg
from engine_downloader import BackgroundEngineInitializer, are_engines_already_present
from executors import execute_whitelisted_task, is_error_retryable
from gpu_probe import (
    has_active_gaming_process,
    is_running_on_battery,
    query_nvidia_telemetry,
    verify_worker_binaries,
)
from hot_updater import apply_hot_update, relaunch_self_silently, rollback_last_hot_update
from hw_gate import compute_hwid_hash, get_user_idle_seconds
from panda_protocol import (
    PROTOCOL_VERSION,
    ProtocolValidationError,
    parse_worker_bearer,
    sha256_file,
)


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("panda_worker")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
    wcfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        wcfg.LOG_DIR / "panda_worker.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


log = _setup_logging()


def _redact(text: str, token: str) -> str:
    if not text:
        return ""
    out = text
    if token:
        out = out.replace(token, "pwk_***REDACTED***")
    return out


def pair_with_6digit_code(
    server_url: str,
    pair_code: str,
    gpu_name: str,
    vram_mb: int,
) -> Dict[str, Any]:
    """Exchange a 6-digit Telegram code + hardware telemetry for a unique `pwk_vol_...` token."""
    url = f"{server_url.rstrip('/')}/v1/volunteer/pair"
    payload = {
        "pair_code": pair_code,
        "hwid_hash": compute_hwid_hash(),
        "gpu_name": gpu_name,
        "vram_mb": int(vram_mb),
        "worker_version": wcfg.get_runtime_version(),
    }
    r = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
        timeout=15,
        verify=wcfg.TLS_VERIFY,
    )
    body = r.json()
    if r.status_code == 200 and body.get("ok"):
        wcfg.save_state(
            {
                "server_url": server_url.rstrip("/"),
                "worker_id": body["worker_id"],
                "worker_token": body["worker_token"],
                "gpu_tier": body.get("gpu_tier", "standard_8gb"),
                "gpu_tier_label": body.get("gpu_tier_label", ""),
                "load_mode": body.get("load_mode", "auto"),
            }
        )
    return body


class LeaseAbortedError(RuntimeError):
    """Raised when server indicates lease was revoked or expired."""


class UpgradeRequiredError(RuntimeError):
    """Raised when server returns HTTP 426 mandatory update gate."""

    def __init__(self, version_gate: Dict[str, Any]) -> None:
        super().__init__("Требуется обновление Panda Worker")
        self.version_gate = version_gate


class PandaWorkerAgent:
    def __init__(
        self,
        server_url: Optional[str] = None,
        worker_token: Optional[str] = None,
        work_dir: Optional[Path] = None,
        on_ui_state: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        st = wcfg.load_state()
        self.server_url = (server_url or str(st.get("server_url") or wcfg.SERVER_URL)).rstrip("/")
        self.worker_token = (
            worker_token if worker_token is not None else str(st.get("worker_token") or wcfg.WORKER_TOKEN)
        ).strip()
        self.work_dir = Path(work_dir or wcfg.WORK_DIR)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.on_ui_state = on_ui_state

        try:
            self.node_id, _ = parse_worker_bearer(self.worker_token)
        except ProtocolValidationError as exc:
            raise ValueError(
                "WORKER_TOKEN не задан или имеет неверный формат (ожидается pwk_<worker_id>.<secret>)."
            ) from exc

        self.session = requests.Session()
        self.session.verify = wcfg.TLS_VERIFY

        self._stop_evt = threading.Event()
        self._busy_lock = threading.Lock()
        self._busy = False
        self._current_job_id = ""
        self._thermal_cooldown = False
        self._pause_reason = ""
        self._manual_paused = False
        self.capabilities: List[str] = []

        self.load_mode: str = str(st.get("load_mode") or "auto")
        self._pending_mode_push: Optional[str] = None
        self.inter_job_cooldown_sec: float = 1.5
        self.engines_ready: bool = are_engines_already_present(wcfg.ENGINES_DIR) or (
            str(wcfg.os.environ.get("PANDA_ALLOW_FALLBACK_CAPS", "0")).strip() == "1"
        )
        self.dl_progress_pct: int = 100 if self.engines_ready else 0
        self.status_banner: str = "Подключение к серверу Panda AI..."
        self.volunteer_snapshot: Dict[str, Any] = {}
        self._updating_lock = threading.Lock()
        self._is_updating = False

    def _emit_ui(self, extra: Optional[Dict[str, Any]] = None) -> None:
        if not self.on_ui_state:
            return
        tel = query_nvidia_telemetry()
        snap = {
            "node_id": self.node_id,
            "version": wcfg.get_runtime_version(),
            "gpu_name": tel.get("gpu_name") or "",
            "vram_mb": int(tel.get("vram_total_mb") or 0),
            "temp_c": int(tel.get("temp_c") or 0),
            "gpu_util": int(tel.get("gpu_util") or 0),
            "busy": self._busy,
            "paused": bool(self._pause_reason),
            "pause_reason": self._pause_reason,
            "manual_paused": self._manual_paused,
            "load_mode": self.load_mode,
            "effective_mode": self.compute_effective_mode(),
            "engines_ready": self.engines_ready,
            "dl_progress_pct": self.dl_progress_pct,
            "status_banner": self.status_banner,
            "volunteer": self.volunteer_snapshot,
        }
        if extra:
            snap.update(extra)
        try:
            self.on_ui_state(snap)
        except Exception:
            pass

    def set_user_load_mode(self, mode: str) -> None:
        if mode not in ("light", "medium", "turbo", "auto"):
            return
        self.load_mode = mode
        self._pending_mode_push = mode
        wcfg.save_state({"load_mode": mode})
        try:
            self.session.post(
                f"{self.server_url}/v1/worker/set_mode",
                headers=self._headers({"Content-Type": "application/json"}),
                data=json.dumps({"load_mode": mode}).encode("utf-8"),
                timeout=6,
            )
        except Exception:
            pass
        self._emit_ui()

    def set_manual_pause(self, paused: bool) -> None:
        self._manual_paused = bool(paused)
        if paused:
            wcfg.PAUSE_FLAG_FILE.write_text("paused\n", encoding="utf-8")
        else:
            wcfg.PAUSE_FLAG_FILE.unlink(missing_ok=True)
        self._emit_ui()

    def compute_effective_mode(self) -> str:
        if self.load_mode in ("light", "medium", "turbo"):
            return self.load_mode
        # Smart Auto: Turbo when user idle >= 10 minutes, Light while user actively uses PC
        idle_s = get_user_idle_seconds()
        return "turbo" if idle_s >= 600.0 else "light"

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        ver = wcfg.get_runtime_version()
        h = {
            "Authorization": f"Bearer {self.worker_token}",
            "X-Panda-Protocol": PROTOCOL_VERSION,
            "User-Agent": f"PandaWorker/{ver} ({self.node_id})",
        }
        if extra:
            h.update(extra)
        return h

    def purge_stale_work_dir(self) -> int:
        removed = 0
        if not self.work_dir.exists():
            return 0
        for item in self.work_dir.iterdir():
            if item.name.startswith("job_"):
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
        return removed

    def startup_self_check(self) -> Dict[str, Any]:
        stale = self.purge_stale_work_dir()
        if stale:
            log.info("Очищено незавершённых временных папок после прошлого запуска: %d", stale)
        try:
            chk = verify_worker_binaries()
        except Exception as exc:
            if rollback_last_hot_update():
                relaunch_self_silently()
            raise RuntimeError(f"Ошибка проверки бинарников: {exc}") from exc

        if not chk["ok"]:
            raise RuntimeError(f"Не найдены обязательные утилиты: {', '.join(chk['missing'])}")
        self.capabilities = list(chk["capabilities"])
        self.engines_ready = True
        self.dl_progress_pct = 100
        tel = query_nvidia_telemetry()
        log.info(
            "Panda Worker v%s готов | Узел: %s | GPU: %s (Vulkan idx=%s, VRAM=%s MB) | Роли: %s",
            wcfg.get_runtime_version(),
            self.node_id,
            tel.get("gpu_name") or "CPU/Auto",
            chk.get("vulkan_gpu_id"),
            tel.get("vram_total_mb") or 0,
            ",".join(self.capabilities),
        )
        return chk

    def evaluate_pause_state(self, telemetry: Dict[str, Any]) -> bool:
        """Apply thermal hysteresis, manual pause, battery guard, and gaming guard."""
        temp = int(telemetry.get("temp_c") or 0)
        if temp >= wcfg.MAX_TEMP_C:
            if not self._thermal_cooldown:
                log.warning(
                    "Термозащита GPU: %d°C >= %d°C — пауза набора новых задач до остывания (%d°C)",
                    temp,
                    wcfg.MAX_TEMP_C,
                    wcfg.COOLDOWN_TEMP_C,
                )
            self._thermal_cooldown = True
        elif self._thermal_cooldown and temp <= wcfg.COOLDOWN_TEMP_C:
            log.info("GPU остыл до %d°C — возобновляю приём задач.", temp)
            self._thermal_cooldown = False

        if self._manual_paused or wcfg.PAUSE_FLAG_FILE.exists():
            self._pause_reason = "На паузе пользователем"
            return True
        if self._thermal_cooldown:
            self._pause_reason = f"Охлаждение видеокарты ({temp}°C)"
            return True
        if wcfg.PAUSE_ON_BATTERY and is_running_on_battery():
            self._pause_reason = "Пауза: работа от батареи"
            return True
        game = has_active_gaming_process(wcfg.GAMING_PROCESS_NAMES)
        if game:
            self._pause_reason = f"Пауза на время игры ({game})"
            return True

        self._pause_reason = ""
        return False

    def _apply_volunteer_server_sync(self, vol_data: Dict[str, Any]) -> None:
        if not vol_data:
            return
        self.volunteer_snapshot = vol_data
        srv_mode = str(vol_data.get("load_mode") or "")
        if srv_mode in ("light", "medium", "turbo", "auto") and not self._pending_mode_push:
            self.load_mode = srv_mode
            wcfg.save_state({"load_mode": srv_mode})

        mspec = vol_data.get("mode_spec") or {}
        if mspec.get("gpu_threads") and mspec.get("rife_threads"):
            wcfg.apply_load_mode_threads(
                str(mspec["gpu_threads"]),
                str(mspec["rife_threads"]),
                int(mspec.get("max_temp_c") or 84),
            )
            self.inter_job_cooldown_sec = float(mspec.get("cooldown_between_jobs_sec") or 1.5)

        vgate = vol_data.get("version_gate") or {}
        if vgate.get("update_available") and vgate.get("update_url"):
            self._trigger_background_hot_update(vgate)
        elif vgate.get("blocked"):
            self.status_banner = (
                f"⚠️ Требуется обновление до v{vgate.get('min_required_version')} — начисление пакетов на паузе"
            )

    def _trigger_background_hot_update(self, vgate: Dict[str, Any]) -> None:
        if self._is_updating or self._busy:
            return

        def _upd_worker() -> None:
            with self._updating_lock:
                if self._is_updating:
                    return
                self._is_updating = True
            try:
                ok = apply_hot_update(
                    vgate,
                    stop_evt=self._stop_evt,
                    on_progress=lambda msg: (setattr(self, "status_banner", msg), self._emit_ui()),
                )
                if ok and not self._busy:
                    self.status_banner = "✅ Обновление установлено! Перезапуск модулей..."
                    self._emit_ui()
                    time.sleep(1.0)
                    relaunch_self_silently()
            except Exception as exc:
                log.warning("Фоновое автообновление не удалось: %s", exc)
            finally:
                self._is_updating = False

        threading.Thread(target=_upd_worker, name="panda-hot-updater", daemon=True).start()

    def send_heartbeat(self) -> Dict[str, Any]:
        tel = query_nvidia_telemetry()
        paused = self.evaluate_pause_state(tel)
        eff_mode = self.compute_effective_mode()
        payload: Dict[str, Any] = {
            "gpu_name": tel.get("gpu_name") or "",
            "vram_mb": int(tel.get("vram_total_mb") or 0),
            "temp_c": int(tel.get("temp_c") or 0),
            "capabilities": [] if (paused or not self.engines_ready) else self.capabilities,
            "version": wcfg.get_runtime_version(),
            "effective_mode": eff_mode,
            "engines_ready": self.engines_ready,
            "dl_progress_pct": self.dl_progress_pct,
            "paused": paused,
        }
        if self._pending_mode_push:
            payload["load_mode"] = self._pending_mode_push

        r = self.session.post(
            f"{self.server_url}/v1/worker/heartbeat",
            headers=self._headers({"Content-Type": "application/json"}),
            data=json.dumps(payload).encode("utf-8"),
            timeout=12,
        )
        if r.status_code in (401, 403):
            raise PermissionError("Токен воркера отклонён или отозван сервером (HTTP 401/403).")
        r.raise_for_status()
        self._pending_mode_push = None
        body = r.json()
        self._apply_volunteer_server_sync(body.get("volunteer") or {})
        self._emit_ui()
        return body

    def claim_next_job(self) -> Optional[Dict[str, Any]]:
        if not self.engines_ready or self._is_updating:
            return None
        tel = query_nvidia_telemetry()
        if self.evaluate_pause_state(tel):
            return None
        payload = {
            "gpu_name": tel.get("gpu_name") or "",
            "vram_mb": int(tel.get("vram_total_mb") or 0),
            "capabilities": self.capabilities,
            "version": wcfg.get_runtime_version(),
        }
        r = self.session.post(
            f"{self.server_url}/v1/worker/claim",
            headers=self._headers({"Content-Type": "application/json"}),
            data=json.dumps(payload).encode("utf-8"),
            timeout=15,
        )
        if r.status_code == 204:
            return None
        if r.status_code == 426:
            body_426 = r.json()
            raise UpgradeRequiredError(body_426.get("version_gate") or {})
        if r.status_code in (401, 403):
            raise PermissionError("Токен воркера отклонён сервером (HTTP 401/403).")
        r.raise_for_status()
        body = r.json()
        return body.get("job")

    def _download_and_verify_input(
        self, job_id: str, lease_id: str, expected_sha256: str, dest_path: Path
    ) -> None:
        h = hashlib.sha256()
        with self.session.get(
            f"{self.server_url}/v1/worker/jobs/{job_id}/input",
            headers=self._headers({"X-Lease-Id": lease_id}),
            stream=True,
            timeout=(15, 180),
        ) as resp:
            if resp.status_code in (403, 409):
                raise LeaseAbortedError("Аренда задачи истекла при скачивании входного файла.")
            resp.raise_for_status()
            with dest_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=262144):
                    if not chunk:
                        continue
                    h.update(chunk)
                    f.write(chunk)
        actual_sha = h.hexdigest()
        if expected_sha256 and actual_sha.lower() != expected_sha256.lower():
            dest_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Контрольная сумма входного файла не совпала ({actual_sha[:12]} != {expected_sha256[:12]})."
            )

    def _post_progress(
        self, job_id: str, lease_id: str, progress_text: str, progress_pct: int = 10
    ) -> bool:
        try:
            r = self.session.post(
                f"{self.server_url}/v1/worker/jobs/{job_id}/progress",
                headers=self._headers({"Content-Type": "application/json", "X-Lease-Id": lease_id}),
                data=json.dumps(
                    {
                        "lease_id": lease_id,
                        "pct": int(progress_pct),
                        "text": progress_text[:280],
                    }
                ).encode("utf-8"),
                timeout=10,
            )
            if r.status_code in (403, 409):
                return False
            if r.status_code == 200:
                data = r.json()
                return bool(data.get("ok", True))
            return True
        except Exception:
            return True

    def _upload_result(
        self,
        job_id: str,
        lease_id: str,
        payload_path: Path,
        is_bundle: bool,
    ) -> None:
        out_sha, size = sha256_file(payload_path)
        headers = self._headers(
            {
                "X-Lease-Id": lease_id,
                "X-Content-SHA256": out_sha,
                "X-Result-Format": "bundle" if is_bundle else "raw",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            }
        )
        with payload_path.open("rb") as f:
            r = self.session.put(
                f"{self.server_url}/v1/worker/jobs/{job_id}/result",
                headers=headers,
                data=f,
                timeout=(15, 360),
            )
        if r.status_code in (403, 409):
            raise LeaseAbortedError("Сервер отклонил результат: аренда истекла или передана другому узлу.")
        r.raise_for_status()

    def _report_failure(
        self, job_id: str, lease_id: str, error_msg: str, retryable: bool
    ) -> None:
        try:
            self.session.post(
                f"{self.server_url}/v1/worker/jobs/{job_id}/fail",
                headers=self._headers({"Content-Type": "application/json", "X-Lease-Id": lease_id}),
                data=json.dumps(
                    {
                        "lease_id": lease_id,
                        "error_code": "worker_exec_error",
                        "error_text": _redact(error_msg, self.worker_token)[:400],
                        "retryable": bool(retryable),
                    }
                ).encode("utf-8"),
                timeout=12,
            )
        except Exception as exc:
            log.warning("Не удалось отправить отчёт об ошибке job=%s: %s", job_id, type(exc).__name__)

    def process_claimed_job(self, job: Dict[str, Any]) -> bool:
        job_id = str(job["job_id"])
        lease_id = str(job["lease_id"])
        task_type = str(job["task_type"])
        params = dict(job.get("params") or {})
        ext = str(job.get("input_ext") or ".bin")
        expected_sha = str(job.get("input_sha256") or "")

        jdir = self.work_dir / f"job_{job_id}"
        if jdir.exists():
            shutil.rmtree(jdir, ignore_errors=True)
        jdir.mkdir(parents=True, exist_ok=True)

        abort_flag = threading.Event()
        done_flag = threading.Event()
        latest_txt = ["🚀 GPU-узел обрабатывает задачу..."]

        def _lease_renewer() -> None:
            while not done_flag.wait(timeout=max(5.0, wcfg.HEARTBEAT_SEC * 0.8)):
                ok = self._post_progress(job_id, lease_id, latest_txt[0])
                if not ok:
                    abort_flag.set()
                    return

        def _on_progress(txt: str) -> None:
            if abort_flag.is_set() or self._stop_evt.is_set():
                raise LeaseAbortedError("Задача остановлена сервером или завершением воркера.")
            latest_txt[0] = txt
            self.status_banner = "⚡ Обрабатываю анонимный фрагмент ИИ..."
            self._emit_ui()
            if not self._post_progress(job_id, lease_id, txt):
                abort_flag.set()
                raise LeaseAbortedError("Сервер отозвал аренду задачи.")

        with self._busy_lock:
            self._busy = True
            self._current_job_id = job_id
        self.status_banner = "⚡ Обрабатываю анонимный фрагмент ИИ..."
        self._emit_ui()

        t0 = time.monotonic()
        renew_thread = threading.Thread(target=_lease_renewer, daemon=True)
        renew_thread.start()
        try:
            log.info("Взята задача job=%s type=%s", job_id, task_type)
            in_file = jdir / f"input_source{ext}"
            self._download_and_verify_input(job_id, lease_id, expected_sha, in_file)
            if abort_flag.is_set():
                raise LeaseAbortedError("Аренда отозвана до старта GPU.")

            upload_path, is_bundle, _res_meta = execute_whitelisted_task(
                task_type=task_type,
                raw_params=params,
                input_path=in_file,
                work_dir=jdir,
                on_progress=_on_progress,
            )
            if abort_flag.is_set():
                raise LeaseAbortedError("Аренда отозвана во время рендера.")

            self._upload_result(job_id, lease_id, upload_path, is_bundle)
            log.info(
                "Задача завершена и проверена сервером job=%s type=%s за %.1fs",
                job_id,
                task_type,
                time.monotonic() - t0,
            )
            return True
        except LeaseAbortedError as lae:
            log.warning("Задача %s прервана по сигналу аренды: %s", job_id, lae)
            return False
        except Exception as exc:
            safe_err = _redact(str(exc), self.worker_token)
            retry = is_error_retryable(exc)
            log.error("Ошибка выполнения job=%s (retryable=%s): %s", job_id, retry, safe_err)
            self._report_failure(job_id, lease_id, safe_err, retryable=retry)
            return False
        finally:
            done_flag.set()
            with self._busy_lock:
                self._busy = False
                self._current_job_id = ""
            shutil.rmtree(jdir, ignore_errors=True)
            if self.engines_ready:
                self.status_banner = "🟢 ИИ готов · Дежурство в сети Panda AI"
            self._emit_ui()

    def run_once(self) -> bool:
        """Run one heartbeat + claim + execute cycle (ideal for tests and health verification)."""
        if not self.capabilities:
            self.startup_self_check()
        self.send_heartbeat()
        job = self.claim_next_job()
        if not job:
            return False
        return self.process_claimed_job(job)

    def stop(self) -> None:
        self._stop_evt.set()

    def serve_forever(self) -> None:
        # 1) If AI engines are not yet downloaded on disk, start silent resumable background download!
        if self.engines_ready:
            self.startup_self_check()
            self.status_banner = "🟢 ИИ готов · Дежурство в сети Panda AI"
        else:
            self.status_banner = "📥 Подготовка тихой фоновой загрузки ИИ-движков..."

            def _bg_dl() -> None:
                try:
                    hb = self.send_heartbeat()
                    eng_url = ((hb.get("volunteer") or {}).get("version_gate") or {}).get("engines_url") or ""
                except Exception:
                    eng_url = ""

                def _on_dl(pct: int, msg: str, rdy: bool) -> None:
                    self.dl_progress_pct = pct
                    self.status_banner = msg
                    if rdy:
                        try:
                            self.startup_self_check()
                        except Exception as exc:
                            log.error("Ошибка инициализации после докачки: %s", exc)
                    self._emit_ui()

                dl = BackgroundEngineInitializer(
                    server_url=self.server_url,
                    worker_token=self.worker_token,
                    engines_url=eng_url,
                    on_status=_on_dl,
                )
                try:
                    dl.ensure_ready()
                except InterruptedError:
                    pass
                except Exception as exc:
                    log.error("Фоновая загрузка прервана: %s", exc)

            threading.Thread(target=_bg_dl, name="panda-bg-downloader", daemon=True).start()

        def _hb_loop() -> None:
            while not self._stop_evt.wait(timeout=wcfg.HEARTBEAT_SEC):
                try:
                    self.send_heartbeat()
                except Exception as exc:
                    log.warning("Heartbeat недоступен: %s", _redact(str(exc), self.worker_token)[:180])

        threading.Thread(target=_hb_loop, name="panda-worker-hb", daemon=True).start()

        backoff = wcfg.IDLE_POLL_MIN_SEC
        while not self._stop_evt.is_set():
            try:
                self.send_heartbeat()
                if not self.engines_ready:
                    self._stop_evt.wait(timeout=4.0)
                    continue
                job = self.claim_next_job()
                backoff = wcfg.IDLE_POLL_MIN_SEC
                if job:
                    self.process_claimed_job(job)
                    if self.inter_job_cooldown_sec > 0.2:
                        self._stop_evt.wait(timeout=self.inter_job_cooldown_sec)
                    continue
                sleep_s = random.uniform(wcfg.IDLE_POLL_MIN_SEC, wcfg.IDLE_POLL_MAX_SEC)
                self._stop_evt.wait(timeout=sleep_s)
            except UpgradeRequiredError as ure:
                self.status_banner = (
                    f"⚠️ Вышло важное обновление v{ure.version_gate.get('min_required_version')} — обновляю..."
                )
                self._emit_ui()
                self._trigger_background_hot_update(ure.version_gate)
                self._stop_evt.wait(timeout=12.0)
            except PermissionError as pe:
                self.status_banner = "⛔ Ключ устройства отозван или неактивен"
                self._emit_ui()
                log.error("%s Ожидание 30с перед повторной проверкой...", pe)
                self._stop_evt.wait(timeout=30.0)
            except KeyboardInterrupt:
                log.info("Остановка воркера по Ctrl+C...")
                self.stop()
                break
            except Exception as exc:
                self.status_banner = "🌐 Ожидание связи с сервером Panda AI..."
                self._emit_ui()
                log.warning(
                    "Связь с сервером временно недоступна (%s). Повтор через %.1fс...",
                    _redact(str(exc), self.worker_token)[:160],
                    backoff,
                )
                self._stop_evt.wait(timeout=backoff + random.uniform(0.0, 1.5))
                backoff = min(wcfg.BACKOFF_MAX_SEC, backoff * 1.8)


def main() -> None:
    agent = PandaWorkerAgent()

    def _sig_handler(_signum: int, _frame: Any) -> None:
        log.info("Получен сигнал завершения — плавная остановка Panda Worker...")
        agent.stop()

    signal.signal(signal.SIGINT, _sig_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig_handler)

    agent.serve_forever()


if __name__ == "__main__":
    main()
