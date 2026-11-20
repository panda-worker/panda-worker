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


def query_nvidia_telemetry() -> Dict[str, Any]:
    """Read GPU telemetry via nvidia-smi (or fallback registry/Vulkan) without requiring `engines/`."""
    info: Dict[str, Any] = {
        "gpu_name": "",
        "vram_total_mb": 0,
        "vram_free_mb": 0,
        "temp_c": 0,
        "gpu_util": 0,
    }
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
            first_line = r.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first_line.split(",")]
            if len(parts) >= 5:
                info["gpu_name"] = parts[0][:120]
                info["vram_total_mb"] = int(float(parts[1]))
                info["vram_free_mb"] = int(float(parts[2]))
                info["temp_c"] = int(float(parts[3]))
                info["gpu_util"] = int(float(parts[4]))
    except Exception:
        pass

    if not info["gpu_name"]:
        try:
            from hw_gate import detect_best_discrete_gpu
            gn, vm = detect_best_discrete_gpu()
            info["gpu_name"] = gn[:120]
            info["vram_total_mb"] = vm
        except Exception:
            pass
    return info


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


def has_active_gaming_process(watch_names: List[str]) -> str:
    """Return matched process name if any configured game/app is running, else ''."""
    if not watch_names:
        return ""
    watch_set = {w.lower() for w in watch_names if w}
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
                creationflags=_SUBPROCESS_FLAGS,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    if not line.strip():
                        continue
                    proc = line.split(",", 1)[0].strip().strip('"').lower()
                    if proc in watch_set:
                        return proc
        else:
            r = subprocess.run(["ps", "-eo", "comm"], capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    proc = line.strip().lower()
                    if proc in watch_set:
                        return proc
    except Exception:
        pass
    return ""


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
