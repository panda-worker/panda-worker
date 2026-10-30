# -*- coding: utf-8 -*-
"""
panda_worker/self_destruct.py
AV-Clean Windows Autostart (`IShellLinkW` Startup Shortcut in `%LOCALAPPDATA%\\Programs\\PandaGPUHelper`)
and 1-Click Full Uninstall for Panda GPU Helper v2.0.

Zero heuristic triggers:
- Uses Microsoft's standard per-user app folder (`%LOCALAPPDATA%\\Programs\\PandaGPUHelper`).
- Creates a native Win32 Shell `.lnk` shortcut in the user's `Startup` folder via `ole32.dll` COM
  (`IShellLinkW` / `IPersistFile`) — spawns ZERO child processes (`no schtasks`, `no cmd.exe`,
  `no CurrentVersion\\Run` registry edits).
- Full uninstall cleanly revokes the server token, removes Startup/Start Menu shortcuts, wipes data,
  and schedules clean file cleanup via Win32 `MoveFileExW`.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import requests

import worker_config as wcfg

log = logging.getLogger("panda_worker")


def _installed_app_exe() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
    return base / "Programs" / "PandaGPUHelper" / "PandaGPUHelper.exe"


def _startup_shortcut_path() -> Optional[Path]:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Panda GPU Helper.lnk"


def _create_win32_shortcut(lnk_path: Path, target_exe: Path, args: str = "", work_dir: str = "") -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        ole32 = ctypes.windll.ole32

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

            def __init__(self, s: str) -> None:
                super().__init__()
                ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(self))

        clsid_sl = GUID("{00021401-0000-0000-C000-000000000046}")
        iid_slw = GUID("{000214F9-0000-0000-C000-000000000046}")
        iid_pf = GUID("{0000010b-0000-0000-C000-000000000046}")

        ole32.CoInitialize(None)
        psl = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid_sl), None, 1, ctypes.byref(iid_slw), ctypes.byref(psl)
        )
        if hr != 0 or not psl.value:
            return False

        vtbl = ctypes.cast(
            ctypes.cast(psl, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )
        qi_fn = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
        )(vtbl[0])
        rel_fn = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        set_wd = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl[9])
        set_args = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl[11])
        set_path = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl[20])

        set_path(psl, str(target_exe))
        if args:
            set_args(psl, args)
        if work_dir:
            set_wd(psl, work_dir)

        ppf = ctypes.c_void_p()
        if qi_fn(psl, ctypes.byref(iid_pf), ctypes.byref(ppf)) == 0 and ppf.value:
            pf_vtbl = ctypes.cast(
                ctypes.cast(ppf, ctypes.POINTER(ctypes.c_void_p))[0],
                ctypes.POINTER(ctypes.c_void_p),
            )
            save_fn = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, wintypes.BOOL
            )(pf_vtbl[6])
            rel_pf = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(pf_vtbl[2])
            lnk_path.parent.mkdir(parents=True, exist_ok=True)
            hr_save = save_fn(ppf, str(lnk_path), True)
            rel_pf(ppf)
            rel_fn(psl)
            return hr_save == 0
        rel_fn(psl)
    except Exception:
        pass
    return False


def install_windows_autostart() -> None:
    """
    Ensure mandatory Windows startup via native Startup folder `.lnk` pointing to
    `%LOCALAPPDATA%\\Programs\\PandaGPUHelper\\PandaGPUHelper.exe --minimized`.
    """
    if sys.platform != "win32" or not wcfg.IS_FROZEN:
        return
    app_exe = _installed_app_exe()
    try:
        app_exe.parent.mkdir(parents=True, exist_ok=True)
        if wcfg.EXE_PATH.is_file() and wcfg.EXE_PATH.resolve() != app_exe.resolve():
            if not app_exe.is_file() or app_exe.stat().st_size != wcfg.EXE_PATH.stat().st_size:
                shutil.copy2(wcfg.EXE_PATH, app_exe)
    except Exception:
        if not app_exe.is_file():
            app_exe = wcfg.EXE_PATH

    target_exe = app_exe if app_exe.is_file() else wcfg.EXE_PATH
    lnk = _startup_shortcut_path()
    if lnk is not None:
        _create_win32_shortcut(lnk, target_exe, "--minimized", str(target_exe.parent))


def remove_windows_autostart_and_shortcuts() -> None:
    if sys.platform != "win32":
        return
    lnk = _startup_shortcut_path()
    if lnk is not None:
        lnk.unlink(missing_ok=True)
    for folder_env in ("USERPROFILE", "APPDATA"):
        base = os.environ.get(folder_env, "")
        if not base:
            continue
        for rel in (
            "Desktop/Panda Worker.lnk",
            "Desktop/Panda GPU Helper.lnk",
            "Microsoft/Windows/Start Menu/Programs/Panda Worker.lnk",
            "Microsoft/Windows/Start Menu/Programs/Panda GPU Helper.lnk",
        ):
            (Path(base) / rel).unlink(missing_ok=True)


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
    Completely and irreversibly wipe Panda Worker data, token, and Startup shortcut.
    """
    st = wcfg.load_state()
    s_url = (server_url or str(st.get("server_url") or wcfg.SERVER_URL)).strip()
    tok = (worker_token or str(st.get("worker_token") or wcfg.WORKER_TOKEN)).strip()

    burned = False
    if burn_remote and tok:
        burned = burn_token_on_server(s_url, tok)

    remove_windows_autostart_and_shortcuts()

    root_log = logging.getLogger("panda_worker")
    for h in list(root_log.handlers):
        try:
            h.close()
            root_log.removeHandler(h)
        except Exception:
            pass

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

    if sys.platform == "win32" and wcfg.IS_FROZEN:
        app_exe = _installed_app_exe()
        for p in (app_exe, app_exe.parent, wcfg.EXE_PATH, wcfg.WORKER_ROOT):
            try:
                if p.is_file() and p.resolve() != wcfg.EXE_PATH.resolve():
                    p.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            import ctypes
            # MOVEFILE_DELAY_UNTIL_REBOOT = 0x4 (clean Win32 API, zero batch scripts)
            ctypes.windll.kernel32.MoveFileExW(str(wcfg.EXE_PATH), None, 4)
        except Exception:
            pass
    elif wcfg.WORKER_ROOT != wcfg.BUNDLE_DIR:
        shutil.rmtree(wcfg.WORKER_ROOT, ignore_errors=True)

    if exit_process:
        os._exit(0)
    return {"wiped": True, "burned_remote": burned}
