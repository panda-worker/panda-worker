# -*- coding: utf-8 -*-
"""
panda_worker/hot_updater.py
Zero-Reinstall Signed Hot-Updater for Panda Worker (v2.5.0).

1. `PandaWorker.exe` loads hot-updated `.py` modules from `%LOCALAPPDATA%\\PandaWorker\\core_current`.
2. When the server's `version_gate` announces a newer `latest_version`, `apply_hot_update()`
   downloads `update.zip` (resumable, TLS-pinned for our server), verifies SHA-256 AND the
   mandatory Ed25519 signature (release_verify), then installs it atomically.
3. `rollback_last_hot_update()` restores the previous core AND the previous version label.

FIX-2026-11-19:
* P0-3: подпись обязательна (fail-closed), HMAC-фолбэк удалён, даунгрейд запрещён.
* P1-6: дельта движков ставится атомарно (старые файлы уходят в бэкап и возвращаются
  при любой ошибке), откат возвращает ПРЕДЫДУЩУЮ версию, а не BASE.
* Загрузка идёт через сессию с пином сертификата (раньше — новая Session с verify=True,
  которая не знала про самоподписанный сертификат сервера).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import worker_config as wcfg
from engine_downloader import download_file_resumable, safe_extract_zip
from release_verify import verify_release_file

log = logging.getLogger("panda_worker")

HOT_UPDATE_MAX_BYTES = 64 * 1024 * 1024


def verify_update_signature(archive_path: Path, sha256_hex: str, sig_b64: str) -> bool:
    """Backward-compatible name: mandatory SHA-256 + Ed25519 (fail-closed)."""
    return verify_release_file(archive_path, sha256_hex, sig_b64, what="Hot-update")


def _install_engines_delta(eng_delta: Path) -> None:
    """Move files from `eng_delta` into ENGINES_DIR atomically (all-or-nothing)."""
    items = list(eng_delta.iterdir())
    if not items:
        return
    backup = wcfg.WORKER_ROOT / f"engines_delta_backup_{int(time.time())}"
    backup.mkdir(parents=True, exist_ok=True)
    moved_old: List[Tuple[Path, Path]] = []
    placed_new: List[Path] = []
    try:
        wcfg.ENGINES_DIR.mkdir(parents=True, exist_ok=True)
        for item in items:
            dst = wcfg.ENGINES_DIR / item.name
            if dst.exists():
                bak = backup / item.name
                shutil.move(str(dst), str(bak))
                moved_old.append((bak, dst))
            shutil.move(str(item), str(dst))
            placed_new.append(dst)
    except Exception:
        log.error("Установка дельты движков прервана — возвращаю прежние файлы.")
        for dst in placed_new:
            try:
                if dst.is_dir():
                    shutil.rmtree(dst, ignore_errors=True)
                else:
                    dst.unlink(missing_ok=True)
            except OSError:
                pass
        for bak, dst in moved_old:
            try:
                shutil.move(str(bak), str(dst))
            except OSError:
                log.error("Не удалось вернуть %s из бэкапа %s", dst.name, backup)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def apply_hot_update(
    version_gate: Dict[str, Any],
    stop_evt: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> bool:
    """Download, verify (SHA-256 + Ed25519), and atomically activate a hot-update."""
    target_ver = str(version_gate.get("latest_version") or "").strip()
    upd_url = str(version_gate.get("update_url") or "").strip()
    upd_sha = str(version_gate.get("update_sha256") or "").strip()
    upd_sig = str(version_gate.get("update_sig_b64") or "").strip()

    if not target_ver or not upd_url or not upd_sha:
        return False
    current = wcfg.get_runtime_version()
    if wcfg._ver_tuple(target_ver) <= wcfg._ver_tuple(current):
        log.info("Hot-update v%s пропущен: установлена v%s (даунгрейд/повтор запрещён).", target_ver, current)
        return False
    if not upd_sig:
        log.error("Hot-update v%s отклонён: сервер не опубликовал подпись.", target_ver)
        return False
    if not upd_url.startswith("https://"):
        upd_url = wcfg.normalize_server_url(upd_url)

    if on_progress:
        on_progress(f"🔄 Скачиваю автообновление Panda Worker v{target_ver}...")

    dest_zip = wcfg.DOWNLOAD_DIR / f"hot_update_{target_ver}.zip"
    download_file_resumable(
        url=upd_url,
        dest_file=dest_zip,
        expected_sha256=upd_sha,
        stop_evt=stop_evt,
        max_bytes=HOT_UPDATE_MAX_BYTES,
    )

    if not verify_update_signature(dest_zip, upd_sha, upd_sig):
        dest_zip.unlink(missing_ok=True)
        return False

    if on_progress:
        on_progress(f"⚡ Устанавливаю обновление v{target_ver} без переустановки .exe...")

    stage_dir = wcfg.WORKER_ROOT / "core_staging_tmp"
    shutil.rmtree(stage_dir, ignore_errors=True)
    try:
        safe_extract_zip(dest_zip, stage_dir, max_total_bytes=256 * 1024 * 1024, max_files=2000)
    finally:
        dest_zip.unlink(missing_ok=True)

    eng_delta = stage_dir / "engines"
    if eng_delta.is_dir():
        _install_engines_delta(eng_delta)
        shutil.rmtree(eng_delta, ignore_errors=True)

    # Rotate `core_current` -> `core_prev` and promote `stage_dir` -> `core_current`
    shutil.rmtree(wcfg.HOT_PREV_DIR, ignore_errors=True)
    had_core = wcfg.HOT_CORE_DIR.is_dir()
    if had_core:
        wcfg.HOT_CORE_DIR.replace(wcfg.HOT_PREV_DIR)
    try:
        stage_dir.replace(wcfg.HOT_CORE_DIR)
    except OSError:
        if had_core:
            wcfg.HOT_PREV_DIR.replace(wcfg.HOT_CORE_DIR)
        raise

    try:
        wcfg.save_state({
            "installed_version": target_ver,
            "previous_version": current,
            "last_update_ok": True,
        })
    except OSError:
        # Метка версии не записалась — откатываем ядро, чтобы версия на диске и в state совпадали.
        shutil.rmtree(wcfg.HOT_CORE_DIR, ignore_errors=True)
        if had_core:
            wcfg.HOT_PREV_DIR.replace(wcfg.HOT_CORE_DIR)
        raise
    wcfg.WORKER_VERSION = target_ver
    log.info("Hot-update v%s успешно установлен в %s (было v%s).", target_ver, wcfg.HOT_CORE_DIR, current)
    return True


def rollback_last_hot_update() -> bool:
    """Restore `core_prev` (and its version label) if a hot-update fails the startup self-check."""
    st = wcfg.load_state()
    prev_ver = str(st.get("previous_version") or "")
    if wcfg.HOT_PREV_DIR.is_dir():
        shutil.rmtree(wcfg.HOT_CORE_DIR, ignore_errors=True)
        wcfg.HOT_PREV_DIR.replace(wcfg.HOT_CORE_DIR)
    elif wcfg.HOT_CORE_DIR.is_dir() and prev_ver:
        # Предыдущей hot-версии не было — возвращаемся к модулям, вшитым в .exe.
        shutil.rmtree(wcfg.HOT_CORE_DIR, ignore_errors=True)
    else:
        return False
    prev_ver = prev_ver or wcfg.BASE_WORKER_VERSION
    try:
        wcfg.save_state({"installed_version": prev_ver, "previous_version": "", "last_update_ok": False})
    except OSError:
        pass
    log.warning("Выполнен автоматический откат hot-update на v%s.", prev_ver)
    return True


def relaunch_self_silently() -> None:
    """Seamlessly restart the `.exe` / Python process so freshly hot-updated modules load cleanly."""
    try:
        env = dict(os.environ)
        # Critical for PyInstaller --onefile: force child process to unpack its own fresh _MEI directory!
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        env.pop("_MEIPASS2", None)
        # v2.5.2: новый процесс получает те же аргументы (+ --after-update): он дождётся, пока
        # этот процесс освободит mutex единственного экземпляра, а не закроется молча.
        args = [a for a in sys.argv[1:] if a not in ("--after-update", "--self-test")] + ["--after-update"]
        # v2.5.3: окно было спрятано в трей -> после обновления тоже стартуем скрыто (и наоборот)
        hidden = getattr(sys, "_panda_window_hidden", None)
        if hidden is True and "--minimized" not in args:
            args.insert(0, "--minimized")
        elif hidden is False:
            args = [a for a in args if a != "--minimized"]
        if wcfg.IS_FROZEN:
            subprocess.Popen([str(wcfg.EXE_PATH)] + args, env=env, close_fds=True)
        else:
            subprocess.Popen([sys.executable, sys.argv[0]] + args, env=env, close_fds=True)
    except Exception as exc:
        log.warning("Не удалось перезапустить процесс после обновления: %s", exc)
        return
    try:  # убрать значок из трея, иначе до наведения мыши висит «мёртвый» дубль
        _tray = sys.modules.get("panda_tray")
        if _tray is not None:
            _tray.remove_all()
    except Exception:
        pass
    os._exit(0)
