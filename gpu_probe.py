# -*- coding: utf-8 -*-
"""
panda_worker/gpu_probe.py
Hardware & workstation telemetry for Panda Worker v2.0:
- NVIDIA GPU name, VRAM total/free MB, temperature °C, GPU load %
- Safe lazy loading of `engines/config.py` so `PandaWorkerSetup.exe` runs cleanly on a brand-new PC
  BEFORE `panda_engines_pack.zip` has finished downloading in the background!
- Windows battery/AC power status (GetSystemPowerStatus) & gaming auto-pause check.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import worker_config as wcfg

_SUBPROCESS_FLAGS = 0x08000000 if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# v2.5.7: лёгкая телеметрия. Раньше каждые 2.5–6 с запускались nvidia-smi (~180 мс) и
# tasklist (~520 мс на ноутбуке) — это давало микрофризы в играх даже без задач.
# Теперь: NVML напрямую через nvml.dll (доли миллисекунды), кэш результатов,
# список процессов — через Toolhelp32 без запуска внешних программ.
# ---------------------------------------------------------------------------
import os
import threading
import time

_TEL_LOCK = threading.Lock()
_TEL_CACHE: Dict[str, Any] = {}
_TEL_TS = 0.0
_NVML: Any = None
_NVML_HANDLE: Any = None
_NVML_FAILED_AT = 0.0


class _NvmlMemory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


def _nvml_handle() -> Any:
    """Инициализирует NVML один раз. None — NVML недоступна (повтор не чаще раза в 10 минут)."""
    global _NVML, _NVML_HANDLE, _NVML_FAILED_AT
    if _NVML_HANDLE is not None:
        return _NVML_HANDLE
    if sys.platform != "win32" or (_NVML_FAILED_AT and time.monotonic() - _NVML_FAILED_AT < 600):
        return None
    try:
        lib = None
        for cand in (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll"),
                     r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll", "nvml.dll"):
            try:
                lib = ctypes.WinDLL(cand)
                break
            except OSError:
                continue
        if lib is None:
            raise OSError("nvml.dll not found")
        init = getattr(lib, "nvmlInit_v2", None) or lib.nvmlInit
        if init() != 0:
            raise OSError("nvmlInit failed")
        h = ctypes.c_void_p()
        get_h = getattr(lib, "nvmlDeviceGetHandleByIndex_v2", None) or lib.nvmlDeviceGetHandleByIndex
        if get_h(0, ctypes.byref(h)) != 0:
            raise OSError("nvmlDeviceGetHandleByIndex failed")
        _NVML, _NVML_HANDLE = lib, h
        return h
    except Exception:  # noqa: BLE001
        _NVML_FAILED_AT = time.monotonic()
        return None


def _telemetry_nvml() -> Dict[str, Any]:
    h = _nvml_handle()
    if h is None:
        return {}
    lib = _NVML
    out: Dict[str, Any] = {}
    try:
        buf = ctypes.create_string_buffer(96)
        if lib.nvmlDeviceGetName(h, buf, 96) == 0:
            out["gpu_name"] = buf.value.decode("utf-8", "replace")[:120]
        mem = _NvmlMemory()
        if lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(mem)) == 0:
            out["vram_total_mb"] = int(mem.total // (1024 * 1024))
            out["vram_free_mb"] = int(mem.free // (1024 * 1024))
        t = ctypes.c_uint()
        if lib.nvmlDeviceGetTemperature(h, 0, ctypes.byref(t)) == 0:
            out["temp_c"] = int(t.value)
        u = _NvmlUtil()
        if lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == 0:
            out["gpu_util"] = int(u.gpu)
    except Exception:  # noqa: BLE001
        return {}
    return out if out.get("gpu_name") else {}


def _telemetry_smi() -> Dict[str, Any]:
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,temperature.gpu,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            creationflags=_SUBPROCESS_FLAGS,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
            if len(parts) >= 5:
                return {
                    "gpu_name": parts[0][:120],
                    "vram_total_mb": int(float(parts[1])),
                    "vram_free_mb": int(float(parts[2])),
                    "temp_c": int(float(parts[3])),
                    "gpu_util": int(float(parts[4])),
                }
    except Exception:  # noqa: BLE001
        pass
    return {}


def query_nvidia_telemetry(max_age: float = 10.0) -> Dict[str, Any]:
    """GPU telemetry (name, VRAM, °C, load %). Кэш `max_age` секунд; NVML, запасной путь — nvidia-smi
    (не чаще раза в 30 с)."""
    global _TEL_TS, _TEL_CACHE
    now = time.monotonic()
    with _TEL_LOCK:
        if _TEL_CACHE and now - _TEL_TS < max(0.0, float(max_age)):
            return dict(_TEL_CACHE)
        info: Dict[str, Any] = {"gpu_name": "", "vram_total_mb": 0, "vram_free_mb": 0, "temp_c": 0, "gpu_util": 0}
        got = _telemetry_nvml()
        if not got:
            if _TEL_CACHE.get("_src") == "smi" and now - _TEL_TS < 30.0:
                return dict(_TEL_CACHE)
            got = _telemetry_smi()
            info["_src"] = "smi"
        else:
            info["_src"] = "nvml"
        info.update(got)
        info["_ts"] = now
        if not info["gpu_name"]:
            try:
                from hw_gate import detect_best_discrete_gpu
                gn, vm = detect_best_discrete_gpu()
                info["gpu_name"] = gn[:120]
                info["vram_total_mb"] = vm
            except Exception:  # noqa: BLE001
                pass
        _TEL_CACHE, _TEL_TS = info, now
        return dict(info)


def is_running_on_battery() -> bool:
    """Return True if a Windows laptop is unplugged on battery power."""
    if sys.platform != "win32":
        try:
            ps_root = Path("/sys/class/power_supply")
            if ps_root.is_dir():
                for item in ps_root.iterdir():
                    status_file = item / "status"
                    if status_file.is_file():
                        st = status_file.read_text(encoding="utf-8", errors="ignore").strip().lower()
                        if st == "discharging":
                            return True
        except Exception:
            pass
        return False

    class _SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_byte),
            ("BatteryFlag", ctypes.c_byte),
            ("BatteryLifePercent", ctypes.c_byte),
            ("SystemStatusFlag", ctypes.c_byte),
            ("BatteryLifeTime", ctypes.c_ulong),
            ("BatteryFullLifeTime", ctypes.c_ulong),
        ]

    try:
        sps = _SYSTEM_POWER_STATUS()
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps))
        if ok and sps.ACLineStatus == 0:
            return True
    except Exception:
        pass
    return False


# Игры и тяжёлые программы, при которых воркер встаёт на паузу. Дополняет GAMING_PROCESS_NAMES
# из worker_config (тот не обновляется автообновлением). Плюс универсальные признаки ниже:
# полноэкранное окно на переднем плане и загрузка видеокарты чужой программой.
BUILTIN_GAME_PROCESSES = frozenset({
    # лаунчеры (flarial.launcher.exe, minecraftlauncher.exe) висят в фоне — их не считаем, только сами игры
    "minecraft.windows.exe", "javaw.exe",
    "robloxplayerbeta.exe", "cs2.exe", "csgo.exe", "dota2.exe", "valorant-win64-shipping.exe",
    "leagueclient.exe", "league of legends.exe", "gta5.exe", "gta5_enhanced.exe", "rdr2.exe",
    "cyberpunk2077.exe", "eldenring.exe", "fortniteclient-win64-shipping.exe", "r5apex.exe",
    "r5apex_dx12.exe", "tslgame.exe", "overwatch.exe", "genshinimpact.exe", "zenlesszonezero.exe",
    "starrail.exe", "wutheringwaves.exe", "rocketleague.exe", "terraria.exe", "osu!.exe",
    "rainbowsix.exe", "eft.exe", "escapefromtarkov.exe", "deadlock.exe", "marvel-win64-shipping.exe",
    "helldivers2.exe", "bg3.exe", "bg3_dx11.exe", "witcher3.exe", "forzahorizon5.exe", "minecraft.exe",
    "afterfx.exe", "premiere pro.exe", "resolve.exe", "blender.exe", "obs64.exe",
})
_IGNORE_FULLSCREEN = frozenset({
    "explorer.exe", "searchhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "lockapp.exe", "applicationframehost.exe", "pandagpuhelper.exe",
    "pandaworkersetup.exe", "python.exe", "pythonw.exe", "windowsterminal.exe",
})


def iter_processes() -> List[tuple]:
    """[(pid, parent_pid, exe_lower)] через Toolhelp32 — без запуска tasklist."""
    out: List[tuple] = []
    if sys.platform != "win32":
        return out
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return out
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile.lower()))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def _exe_of_pid(pid: int) -> str:
    try:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return os.path.basename(buf.value).lower()
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        pass
    return ""


def foreground_fullscreen_exe() -> str:
    """Имя exe полноэкранного окна на переднем плане (игра, фильм на весь экран) или ''."""
    if sys.platform != "win32":
        return ""
    try:
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = u32.GetForegroundWindow()
        if not hwnd or hwnd in (u32.GetShellWindow(), u32.GetDesktopWindow()):
            return ""
        cls = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(hwnd, cls, 64)
        if cls.value in ("WorkerW", "Progman", "Shell_TrayWnd"):
            return ""
        rect = wintypes.RECT()
        if not u32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return ""

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        u32.MonitorFromWindow.restype = wintypes.HANDLE
        mon = u32.MonitorFromWindow(hwnd, 2)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not mon or not u32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return ""
        m = mi.rcMonitor
        if not (rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom):
            return ""
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _exe_of_pid(int(pid.value))
        if not exe or exe in _IGNORE_FULLSCREEN:
            return ""
        return exe
    except Exception:  # noqa: BLE001
        return ""


_GAME_LOCK = threading.Lock()
_GAME_CACHE = ("", 0.0)


def has_active_gaming_process(watch_names: List[str], max_age: float = 4.0) -> str:
    """Игра/тяжёлая программа запущена -> её имя, иначе ''. Кэш `max_age` секунд.
    Признаки: известный exe из списка или полноэкранное окно на переднем плане."""
    global _GAME_CACHE
    now = time.monotonic()
    with _GAME_LOCK:
        if now - _GAME_CACHE[1] < max(0.0, float(max_age)):
            return _GAME_CACHE[0]
        found = ""
        try:
            watch = {w.lower() for w in (watch_names or []) if w} | BUILTIN_GAME_PROCESSES
            if sys.platform == "win32":
                for _pid, _ppid, exe in iter_processes():
                    if exe in watch:
                        found = exe
                        break
                if not found:
                    fs = foreground_fullscreen_exe()
                    if fs:
                        found = fs + " (полный экран)"
            else:
                r = subprocess.run(["ps", "-eo", "comm"], capture_output=True, text=True, timeout=4)
                if r.returncode == 0:
                    for line in r.stdout.splitlines():
                        if line.strip().lower() in watch:
                            found = line.strip().lower()
                            break
        except Exception:  # noqa: BLE001
            found = ""
        _GAME_CACHE = (found, now)
        return found


def child_process_ids(root_pid: int) -> List[int]:
    """Все потомки процесса (ffmpeg, realesrgan, rife, запущенные воркером)."""
    procs = iter_processes()
    kids: Dict[int, List[int]] = {}
    for pid, ppid, _exe in procs:
        kids.setdefault(ppid, []).append(pid)
    out: List[int] = []
    stack = list(kids.get(int(root_pid), []))
    while stack:
        p = stack.pop()
        if p in out or p == root_pid:
            continue
        out.append(p)
        stack.extend(kids.get(p, []))
    return out


def set_processes_suspended(pids: List[int], suspended: bool) -> int:
    """Заморозить/разморозить процессы (NtSuspendProcess/NtResumeProcess). Возвращает число успешных."""
    if sys.platform != "win32" or not pids:
        return 0
    from ctypes import wintypes
    ntdll = ctypes.WinDLL("ntdll")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    fn = ntdll.NtSuspendProcess if suspended else ntdll.NtResumeProcess
    fn.argtypes = [wintypes.HANDLE]
    ok = 0
    for pid in pids:
        h = k32.OpenProcess(0x0800, False, int(pid))  # PROCESS_SUSPEND_RESUME
        if not h:
            continue
        try:
            if fn(h) == 0:
                ok += 1
        finally:
            k32.CloseHandle(h)
    return ok


def subprocess_can_exec(cmd: str) -> bool:
    try:
        r = subprocess.run(
            [cmd, "-version"],
            capture_output=True,
            timeout=5,
            creationflags=_SUBPROCESS_FLAGS,
        )
        return r.returncode == 0
    except Exception:
        return False


def verify_worker_binaries() -> Dict[str, Any]:
    """Validate availability of FFmpeg/FFprobe and GPU upscaler/interpolator binaries."""
    if str(wcfg.ENGINES_DIR) not in sys.path:
        # FIX-2026-11-19: в КОНЕЦ sys.path — файлы движков не могут подменить модули воркера/stdlib.
        sys.path.append(str(wcfg.ENGINES_DIR))
    import config as engine_cfg  # type: ignore

    missing: List[str] = []
    for label, path_str in (
        ("ffmpeg", str(engine_cfg.FFMPEG_EXE)),
        ("ffprobe", str(engine_cfg.FFPROBE_EXE)),
    ):
        p = Path(path_str)
        if not p.is_file() and not subprocess_can_exec(path_str):
            missing.append(label)

    def _isf(p) -> bool:
        # (2.4.7) Path(...).is_file() может кинуть OSError на Windows — раньше это валило self-check.
        try:
            return Path(p).is_file()
        except Exception:  # noqa: BLE001
            return False

    gpu_bins = {
        "realesrgan": _isf(engine_cfg.REALESRGAN_EXE),
        "realcugan": _isf(engine_cfg.REALCUGAN_EXE),
        "rife": _isf(engine_cfg.RIFE_EXE),
    }
    capabilities = ["ffmpeg", "photo_tools"]
    if gpu_bins["realesrgan"] or gpu_bins["realcugan"]:
        capabilities.extend(["photo_esrgan", "video_esrgan"])
        if gpu_bins["rife"]:
            capabilities.append("video_esrgan_rife")
            # v2.5.6: 4K (3840) — замер на RTX 5050 Laptop: пик RIFE v4.6 в 4K ~3.4 ГБ видеопамяти.
            # Берём с запасом: от 6 ГБ. Старые воркеры этой роли не объявляют и 4K-заказ не получат.
            try:
                vram_mb = int(query_nvidia_telemetry().get("vram_total_mb") or 0)
            except Exception:  # noqa: BLE001
                vram_mb = 0
            if vram_mb >= 6000:
                capabilities.append("video_4k")
    elif str(wcfg.os.environ.get("PANDA_ALLOW_FALLBACK_CAPS", "0")).strip() == "1":
        missing = []
        capabilities = ["ffmpeg", "photo_tools", "photo_esrgan", "video_esrgan", "video_esrgan_rife"]

    vk_gpu_id = engine_cfg.resolve_vulkan_gpu_id(force_refresh=True)
    return {
        "ok": len(missing) == 0,
        "missing": missing,
        "gpu_bins": gpu_bins,
        "vulkan_gpu_id": vk_gpu_id,
        "vulkan_devices": engine_cfg.list_vulkan_devices(),
        "capabilities": capabilities,
    }
