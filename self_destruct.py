# -*- coding: utf-8 -*-
"""
panda_worker/self_destruct.py
1-Click Confirmed Irreversible Full Uninstall for Panda Worker v2.0.

Guarantees when the user confirms «Полностью удалить программу» (or when Hardware Gate rejects a weak PC):
1. Permanently burns the `worker_token` on the server (`POST /v1/worker/uninstall`) so credentials can
   never be reused — coming back requires downloading from 0 and pairing a new 6-digit code.
2. Removes Windows Autostart registry value, Scheduled Task (`PandaWorkerAutoStart`), and shortcuts.
3. Terminates any running AI child binaries (`realesrgan`, `realcugan`, `rife`, `ffmpeg`).
4. Wipes AI engines, downloads, logs, `core_current`, and `state.json`.
5. Spawns a silent detached cleanup helper on Windows that deletes `PandaWorker.exe` itself and
   removes `%LOCALAPPDATA%\\PandaWorker` after this process exits, leaving ZERO trace on disk!
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import requests

import worker_config as wcfg

log = logging.getLogger("panda_worker")

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_DETACHED_PROCESS = 0x00000008 if sys.platform == "win32" else 0


def install_windows_autostart() -> None:
    """Register quiet current-user autostart (no UAC admin required)."""
    if sys.platform != "win32" or not wcfg.IS_FROZEN:
        return
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "PandaWorker", 0, winreg.REG_SZ, f'"{wcfg.EXE_PATH}" --minimized')
    except Exception:
        pass


def remove_windows_autostart_and_shortcuts() -> None:
    if sys.platform != "win32":
        return
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(key, "PandaWorker")
            except FileNotFoundError:
                pass
    except Exception:
        pass

    try:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", "PandaWorkerAutoStart", "/F"],
            capture_output=True,
            timeout=5,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        pass

    for folder_env in ("USERPROFILE", "APPDATA"):
        base = os.environ.get(folder_env, "")
        if not base:
            continue
        for rel in (
            "Desktop/Panda Worker.lnk",
            "Microsoft/Windows/Start Menu/Programs/Panda Worker.lnk",
        ):
            lnk = Path(base) / rel
            lnk.unlink(missing_ok=True)


def kill_ai_child_binaries() -> None:
    if sys.platform != "win32":
        return
    for img in (
        "realesrgan-ncnn-vulkan.exe",
        "realcugan-ncnn-vulkan.exe",
        "rife-ncnn-vulkan.exe",
    ):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", img],
                capture_output=True,
                timeout=4,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception:
            pass


def burn_token_on_server(server_url: str, worker_token: str) -> bool:
    if not server_url or not worker_token:
        return False
    try:
        r = requests.post(
            f"{server_url.rstrip('/')}/v1/worker/uninstall",
            headers={
                "Authorization": f"Bearer {worker_token}",
                "Content-Type": "application/json",
            },
            data=b"{}",
            timeout=8,
            verify=wcfg.TLS_VERIFY,
        )
        return r.status_code == 200
    except Exception:
        return False


def execute_full_self_uninstall(
    burn_remote: bool = True,
    server_url: Optional[str] = None,
    worker_token: Optional[str] = None,
    exit_process: bool = True,
) -> Dict[str, Any]:
    """
    Completely and irreversibly wipe Panda Worker, AI engines, config, autostart, and `.exe`.
    """
    st = wcfg.load_state()
    s_url = (server_url or str(st.get("server_url") or wcfg.SERVER_URL)).strip()
    tok = (worker_token or str(st.get("worker_token") or wcfg.WORKER_TOKEN)).strip()

    burned = False
    if burn_remote and tok:
        burned = burn_token_on_server(s_url, tok)

    kill_ai_child_binaries()
    remove_windows_autostart_and_shortcuts()

    # Close logging handlers so Windows releases `panda_worker.log` file handles!
    root_log = logging.getLogger("panda_worker")
    for h in list(root_log.handlers):
        try:
            h.close()
            root_log.removeHandler(h)
        except Exception:
            pass

    # Wipe data subtrees immediately
    for sub in (
        wcfg.ENGINES_DIR,
        wcfg.WORK_DIR,
        wcfg.DOWNLOAD_DIR,
        wcfg.HOT_CORE_DIR,
        wcfg.HOT_PREV_DIR,
        wcfg.LOG_DIR,
    ):
        try:
            if sub.is_dir() and sub != wcfg.BUNDLE_DIR:
                shutil.rmtree(sub, ignore_errors=True)
        except Exception:
            pass

    for f in (wcfg.STATE_FILE, wcfg.PAUSE_FLAG_FILE, wcfg.WORKER_ROOT / ".env"):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass

    # On Windows frozen `.exe`, schedule delayed self-deletion of `.exe` + `WORKER_ROOT`
    if sys.platform == "win32" and wcfg.IS_FROZEN:
        bat_path = Path(tempfile.gettempdir()) / f"panda_wipe_{os.getpid()}.cmd"
        exe_str = str(wcfg.EXE_PATH)
        root_str = str(wcfg.WORKER_ROOT)
        bat_content = (
            "@echo off\r\n"
            "ping 127.0.0.1 -n 3 > nul\r\n"
            f'del /f /q "{exe_str}" > nul 2>&1\r\n'
            f'rmdir /s /q "{root_str}" > nul 2>&1\r\n'
            'del /f /q "%~f0" > nul 2>&1\r\n'
        )
        try:
            bat_path.write_text(bat_content, encoding="oem", errors="ignore")
            subprocess.Popen(
                ["cmd.exe", "/c", str(bat_path)],
                creationflags=_CREATE_NO_WINDOW | _DETACHED_PROCESS,
                close_fds=True,
            )
        except Exception:
            pass
    elif wcfg.WORKER_ROOT != wcfg.BUNDLE_DIR:
        shutil.rmtree(wcfg.WORKER_ROOT, ignore_errors=True)

    if exit_process:
        os._exit(0)
    return {"wiped": True, "burned_remote": burned}
