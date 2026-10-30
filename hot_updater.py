# -*- coding: utf-8 -*-
"""
panda_worker/hot_updater.py
Zero-Reinstall Signed Hot-Updater for Panda Worker v2.0.

How it works without ever asking the user to reinstall `.exe`:
1. `PandaWorker.exe` prepends `%LOCALAPPDATA%\\PandaWorker\\core_current` to `sys.path`.
2. When the server's `version_gate` announces a newer `latest_version` (or returns HTTP `426`
   because `min_required_version` increased), `apply_hot_update()` downloads `update.zip`
   using resumable HTTP Range, verifies `SHA-256` (+ optional cryptographic signature),
   rotates `core_current` -> `core_prev`, extracts the new modules into `core_current`,
   updates `installed_version` in `state.json`, and cleanly restarts the worker loop!
3. If an update ever crashes on startup, `rollback_last_hot_update()` restores `core_prev`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import worker_config as wcfg
from engine_downloader import download_file_resumable, safe_extract_zip

log = logging.getLogger("panda_worker")


def verify_update_signature(archive_path: Path, sha256_hex: str, sig_b64: str) -> bool:
    """
    Verify mandatory SHA-256 integrity, plus Ed25519 / HMAC signature when `RELEASE_PUBKEY_HEX` is set.
    """
    if not sha256_hex:
        log.error("Hot-update отклонён: сервер не передал обязательный SHA-256 пакета.")
        return False

    h = hashlib.sha256()
    with archive_path.open("rb") as fh:
        while True:
            chunk = fh.read(524288)
            if not chunk:
                break
            h.update(chunk)
    actual_sha = h.hexdigest().lower()
    if actual_sha != sha256_hex.strip().lower():
        log.error("Hot-update отклонён: SHA-256 не совпал (%s != %s).", actual_sha[:12], sha256_hex[:12])
        return False

    pubkey_hex = wcfg.RELEASE_PUBKEY_HEX.strip()
    if not pubkey_hex:
        return True

    if not sig_b64:
        log.error("Hot-update отклонён: требуется подпись пакета (RELEASE_PUBKEY_HEX задан).")
        return False

    try:
        sig_raw = base64.b64decode(sig_b64)
        key_raw = bytes.fromhex(pubkey_hex)
        # Support pure-stdlib HMAC-SHA256 verification or Ed25519 if cryptography is installed
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # type: ignore
            pub = Ed25519PublicKey.from_public_bytes(key_raw)
            pub.verify(sig_raw, actual_sha.encode("ascii"))
            return True
        except ImportError:
            expected_mac = hmac.new(key_raw, actual_sha.encode("ascii"), hashlib.sha256).digest()
            return hmac.compare_digest(sig_raw, expected_mac)
    except Exception as exc:
        log.error("Проверка подписи hot-update не прошла: %s", exc)
        return False


def apply_hot_update(
    version_gate: Dict[str, Any],
    stop_evt: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Download, verify, and atomically activate a hot-update in `core_current/` (and/or `engines/`)
    without reinstalling `PandaWorker.exe`.
    """
    target_ver = str(version_gate.get("latest_version") or "").strip()
    upd_url = str(version_gate.get("update_url") or "").strip()
    upd_sha = str(version_gate.get("update_sha256") or "").strip()
    upd_sig = str(version_gate.get("update_sig_b64") or "").strip()

    if not target_ver or not upd_url or not upd_sha:
        return False

    if on_progress:
        on_progress(f"🔄 Скачиваю автообновление Panda Worker v{target_ver}...")

    dest_zip = wcfg.DOWNLOAD_DIR / f"hot_update_{target_ver}.zip"
    download_file_resumable(
        url=upd_url,
        dest_file=dest_zip,
        expected_sha256=upd_sha,
        stop_evt=stop_evt,
    )

    if not verify_update_signature(dest_zip, upd_sha, upd_sig):
        dest_zip.unlink(missing_ok=True)
        return False

    if on_progress:
        on_progress(f"⚡ Устанавливаю обновление v{target_ver} без переустановки .exe...")

    stage_dir = wcfg.WORKER_ROOT / "core_staging_tmp"
    shutil.rmtree(stage_dir, ignore_errors=True)
    safe_extract_zip(dest_zip, stage_dir)
    dest_zip.unlink(missing_ok=True)

    # Optional `engines/` delta inside the hot-update zip
    eng_delta = stage_dir / "engines"
    if eng_delta.is_dir():
        for item in eng_delta.iterdir():
            dst = wcfg.ENGINES_DIR / item.name
            if dst.is_dir():
                shutil.rmtree(dst, ignore_errors=True)
            elif dst.exists():
                dst.unlink(missing_ok=True)
            shutil.move(str(item), str(dst))
        shutil.rmtree(eng_delta, ignore_errors=True)

    # Rotate `core_current` -> `core_prev` and promote `stage_dir` -> `core_current`
    shutil.rmtree(wcfg.HOT_PREV_DIR, ignore_errors=True)
    if wcfg.HOT_CORE_DIR.is_dir():
        wcfg.HOT_CORE_DIR.replace(wcfg.HOT_PREV_DIR)
    stage_dir.replace(wcfg.HOT_CORE_DIR)

    wcfg.save_state({"installed_version": target_ver, "last_update_ok": True})
    wcfg.WORKER_VERSION = target_ver
    log.info("Hot-update v%s успешно установлен в %s!", target_ver, wcfg.HOT_CORE_DIR)
    return True


def rollback_last_hot_update() -> bool:
    """Restore `core_prev` if a hot-update ever fails startup self-check."""
    if not wcfg.HOT_PREV_DIR.is_dir():
        return False
    shutil.rmtree(wcfg.HOT_CORE_DIR, ignore_errors=True)
    wcfg.HOT_PREV_DIR.replace(wcfg.HOT_CORE_DIR)
    wcfg.save_state({"installed_version": wcfg.BASE_WORKER_VERSION, "last_update_ok": False})
    log.warning("Выполнен автоматический откат hot-update на стабильную версию.")
    return True


def relaunch_self_silently() -> None:
    """Seamlessly restart the `.exe` / Python process so freshly hot-updated modules load cleanly."""
    try:
        env = dict(os.environ)
        # Critical for PyInstaller --onefile: force child process to unpack its own fresh _MEI directory!
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        env.pop("_MEIPASS2", None)
        if wcfg.IS_FROZEN:
            subprocess.Popen([str(wcfg.EXE_PATH)], env=env, close_fds=True)
        else:
            subprocess.Popen([sys.executable] + sys.argv, env=env, close_fds=True)
    except Exception as exc:
        log.warning("Не удалось перезапустить процесс после обновления: %s", exc)
        return
    os._exit(0)
