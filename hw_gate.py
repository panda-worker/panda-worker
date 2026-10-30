# -*- coding: utf-8 -*-
"""
panda_worker/hw_gate.py
Pre-Install Hardware Eligibility Gate, Anonymized HWID & User Idle Detector.

Rejects PCs weaker than NVIDIA RTX 5050 / RTX 4060 / RTX 3060 8 GB (< 7800 MB discrete VRAM)
with a polite human explanation in Russian before downloading AI engines, enabling clean
zero-trace self-removal on weak laptops/office PCs.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

MIN_VRAM_MB = 7800
MIN_FREE_DISK_BYTES = int(2.5 * (1024 ** 3))

_WEAK_GPU_MARKERS = (
    "intel(r) uhd",
    "intel uhd",
    "iris(r) xe",
    "iris xe",
    "raptorlake",
    "alderlake",
    "hd graphics",
    "microsoft basic",
    "llvmpipe",
    "swiftshader",
    "radeon(tm) graphics",
    "vega 8",
    "vega 7",
    "gtx 1050",
    "gtx 1650",
    "gt 1030",
    "mx150",
    "mx250",
    "mx330",
    "mx450",
)

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def detect_best_discrete_gpu() -> Tuple[str, int]:
    """
    Detect best discrete GPU name & VRAM (MB) even before AI engines are downloaded!
    Checks `nvidia-smi` first (accurate MB for NVIDIA), then Windows WMI/CIM / DXGI.
    """
    # 1) NVIDIA SMI (most accurate for RTX 30/40/50 series)
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_CREATE_NO_WINDOW,
        )
        if r.returncode == 0 and r.stdout.strip():
            best_name = ""
            best_vram = 0
            for ln in r.stdout.strip().splitlines():
                parts = [p.strip() for p in ln.split(",")]
                if len(parts) >= 2:
                    vram = int(float(parts[1]))
                    if vram > best_vram:
                        best_vram = vram
                        best_name = parts[0]
            if best_name:
                return best_name, best_vram
    except Exception:
        pass

    # 2) Windows PowerShell CIM Win32_VideoController + Registry qwMemorySize (handles 8GB/12GB/16GB AMD & NVIDIA)
    if sys.platform == "win32":
        ps_cmd = (
            "$bestName=''; $bestMB=0; "
            "Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\0*' -ErrorAction SilentlyContinue | ForEach-Object { "
            "  $n = $_.'DriverDesc'; $q = $_.'HardwareInformation.qwMemorySize'; "
            "  if ($n -and $q) { $mb = [int]([uint64]$q / 1MB); if ($mb -gt $bestMB) { $bestMB = $mb; $bestName = $n } } "
            "}; "
            "if (-not $bestName) { "
            "  Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | ForEach-Object { "
            "    $mb = [int]([uint64]$_.AdapterRAM / 1MB); "
            "    if ($mb -gt $bestMB -or -not $bestName) { $bestMB = $mb; $bestName = $_.Name } "
            "  } "
            "}; "
            "Write-Output \"$bestName|$bestMB\""
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=_CREATE_NO_WINDOW,
            )
            if r.returncode == 0 and "|" in r.stdout:
                name_part, mb_part = r.stdout.strip().splitlines()[-1].split("|", 1)
                return name_part.strip(), int(mb_part.strip() or "0")
        except Exception:
            pass

    return "", 0


def check_pc_hardware_eligibility(target_dir: Path) -> Dict[str, Any]:
    """
    Full pre-install eligibility check:
    - 64-bit OS
    - >= 2.5 GB free disk space
    - Discrete GPU >= 7800 MB VRAM (RTX 5050 / 4060 / 3060 8GB class or higher)
    """
    bits = struct.calcsize("P") * 8
    if bits < 64:
        return {
            "eligible": False,
            "code": "os_32bit",
            "gpu_name": "",
            "vram_mb": 0,
            "gpu_tier": "rejected",
            "reason_ru": (
                "Спасибо за желание помочь Panda AI!\n\n"
                "На вашем компьютере установлена 32-разрядная система, а современные ИИ-движки "
                "работают только на 64-разрядной Windows 10/11.\n\n"
                "Сейчас установщик аккуратно удалит временные файлы и сам себя, чтобы не занимать место."
            ),
        }

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        free_b = shutil.disk_usage(target_dir).free
    except Exception:
        free_b = MIN_FREE_DISK_BYTES

    if free_b < MIN_FREE_DISK_BYTES:
        free_gb = round(free_b / (1024 ** 3), 1)
        return {
            "eligible": False,
            "code": "low_disk",
            "gpu_name": "",
            "vram_mb": 0,
            "gpu_tier": "rejected",
            "reason_ru": (
                f"На системном диске свободно всего {free_gb} ГБ.\n"
                "Для безопасной фоновой загрузки нейросетей и буфера обработки требуется минимум 2.5 ГБ свободного места."
            ),
        }

    gpu_name, vram_mb = detect_best_discrete_gpu()
    low = gpu_name.lower()

    if not gpu_name:
        return {
            "eligible": False,
            "code": "no_discrete_gpu",
            "gpu_name": "Не обнаружена",
            "vram_mb": 0,
            "gpu_tier": "rejected",
            "reason_ru": (
                "Спасибо за отклик! 🐼\n\n"
                "К сожалению, на этом компьютере не обнаружена дискретная видеокарта NVIDIA RTX / AMD Radeon.\n"
                "Для нейросетей улучшения 4K и 60 FPS требуется игровая видеокарта от 8 ГБ видеопамяти "
                "(уровень RTX 3060 / RTX 4060 / RTX 5050 и выше).\n\n"
                "Программа сейчас автоматически удалит себя и все временные файлы без следа."
            ),
        }

    if any(m in low for m in _WEAK_GPU_MARKERS) or vram_mb < MIN_VRAM_MB:
        gb_s = f"{round(vram_mb / 1024.0, 1)} ГБ" if vram_mb > 0 else "менее 8 ГБ"
        return {
            "eligible": False,
            "code": "weak_gpu",
            "gpu_name": gpu_name,
            "vram_mb": vram_mb,
            "gpu_tier": "rejected",
            "reason_ru": (
                "Спасибо за желание помочь проекту Panda 4K AI! 🐼\n\n"
                f"На вашем ПК обнаружена видеокарта:\n«{gpu_name}» ({gb_s}).\n\n"
                "Нейросети 4K / 60 FPS требуют дискретную видеокарту минимум на 8 ГБ видеопамяти "
                "(уровень NVIDIA RTX 3060 / 4060 / 5050 или AMD RX 6600 и выше), иначе компьютер "
                "будет зависать при рендере.\n\n"
                "Чтобы не нагружать ваш ПК и не оставлять лишних файлов, программа сейчас полностью удалит себя."
            ),
        }

    if vram_mb >= 15500:
        tier = "ultra_16gb"
        tier_label = "🟣 Ультра (16+ ГБ)"
    elif vram_mb >= 9900:
        tier = "pro_12gb"
        tier_label = "🔵 Мощный (10–12 ГБ)"
    else:
        tier = "standard_8gb"
        tier_label = "🟢 Стандарт (8 ГБ)"

    return {
        "eligible": True,
        "code": "ok",
        "gpu_name": gpu_name,
        "vram_mb": vram_mb,
        "gpu_tier": tier,
        "gpu_tier_label": tier_label,
        "reason_ru": f"Видеокарта подходит: {gpu_name} ({tier_label})",
    }


def compute_hwid_hash() -> str:
    """
    Generate an irreversible privacy-safe SHA-256 fingerprint of this machine
    (used solely to prevent 1 PC farming daily packs across 20 fake accounts).
    """
    raw_parts = [platform.node(), platform.machine(), platform.processor()]
    if sys.platform == "win32":
        try:
            import winreg  # type: ignore
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                raw_parts.append(str(guid))
        except Exception:
            pass
    gpu_n, vram = detect_best_discrete_gpu()
    raw_parts.append(f"{gpu_n}:{vram}")
    material = "panda_hwid_v2|" + "|".join(raw_parts)
    return hashlib.sha256(material.encode("utf-8", errors="ignore")).hexdigest()


def get_user_idle_seconds() -> float:
    """Return seconds since last mouse/keyboard input on Windows (for `🌙 Умный Авто` mode)."""
    if sys.platform != "win32":
        return 0.0

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return max(0.0, float(millis) / 1000.0)
    except Exception:
        pass
    return 0.0
