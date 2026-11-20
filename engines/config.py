# -*- coding: utf-8 -*-
"""
Panda 4K AI — Commercial Configuration & Tariff Matrix (v3.0).
- 100% Dedicated GPU saturation (-j 4:6:6, -threads 0)
- Tiered resolution & FPS caps (Free: 720p @ 30fps, Paid/VIP: 1080p/2K/4K @ 60fps)
- 4-Category Storefront: Combo Bundles (-32%..-36% cheaper), Subscriptions, Separate Photo, Separate Video
- Multi-PC Tailscale GPU Cluster support
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BRAND_NAME = "Panda 4K AI"
LOCAL_TZ_OFFSET_HOURS = 5  # Asia/Almaty (UTC+5) for daily limit midnight reset

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
JOBS_DIR = BASE_DIR / "jobs"
LOGS_DIR = BASE_DIR / "logs"

for _d in (DATA_DIR, JOBS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def load_dotenv_file(env_path: Path) -> Dict[str, str]:
    loaded: Dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
            v = v[1:-1]
        if k:
            loaded[k] = v
            os.environ.setdefault(k, v)
    return loaded


load_dotenv_file(BASE_DIR / ".env")


def _find_first(
    candidates: list[Optional[Path]],
    fallback_glob_root: Optional[Path] = None,
    pattern: str = "",
) -> Path:
    for p in candidates:
        if p is not None and str(p) not in ("", ".") and p.is_file():
            return p
    if fallback_glob_root and fallback_glob_root.is_dir() and pattern:
        for found in fallback_glob_root.rglob(pattern):
            if found.is_file():
                return found
    for p in candidates:
        if p is not None and str(p) not in ("", "."):
            return p
    return Path("__missing_binary__")


# Бинарники лежат в engines/bin (так их кладёт panda_engines_pack.zip), а на
# машине разработчика — в WAIFU_ROOT. Проверяем оба места, чтобы новый ПК
# находил движки сразу после распаковки, без ручной настройки путей.
_LOCAL_ENGINES_BIN = BASE_DIR / "bin"
# v2.5.6: если engines/bin пуст (ПК разработчика с waifu2x-extension) — прежний путь по умолчанию.
_DEFAULT_WAIFU_ROOT = (
    str(_LOCAL_ENGINES_BIN)
    if (_LOCAL_ENGINES_BIN.is_dir() and any(_LOCAL_ENGINES_BIN.iterdir()))
    else r"C:\Users\user\Desktop\Real-ESRGAN\waifu2x-extension-gui"
)
WAIFU_ROOT = Path(os.environ.get("WAIFU_ROOT", _DEFAULT_WAIFU_ROOT))

REALESRGAN_EXE = _find_first(
    [
        Path(os.environ["REALESRGAN_EXE"]) if os.environ.get("REALESRGAN_EXE") else None,
        WAIFU_ROOT / "realesrgan-ncnn-vulkan" / "realesrgan-ncnn-vulkan_waifu2xEX.exe",
    ],
    WAIFU_ROOT,
    "realesrgan-ncnn-vulkan*.exe",
)
REALESRGAN_MODELS = REALESRGAN_EXE.parent / "models"

REALCUGAN_EXE = _find_first(
    [
        Path(os.environ["REALCUGAN_EXE"]) if os.environ.get("REALCUGAN_EXE") else None,
        WAIFU_ROOT / "realcugan-ncnn-vulkan" / "realcugan-ncnn-vulkan_W2xEX.exe",
    ],
    WAIFU_ROOT,
    "realcugan-ncnn-vulkan*.exe",
)
REALCUGAN_MODELS_PRO = REALCUGAN_EXE.parent / "models-pro"

RIFE_EXE = _find_first(
    [
        Path(os.environ["RIFE_EXE"]) if os.environ.get("RIFE_EXE") else None,
        WAIFU_ROOT / "rife-ncnn-vulkan" / "rife-ncnn-vulkan_waifu2xEX.exe",
    ],
    WAIFU_ROOT,
    "rife-ncnn-vulkan*.exe",
)


def is_real_rife_v4_dir(d: Path) -> bool:
    """Настоящая RIFE v4.x: только flownet.* (у старых моделей есть contextnet/fusionnet)."""
    try:
        return ((d / "flownet.bin").is_file() and (d / "flownet.param").is_file()
                and not (d / "fusionnet.bin").exists() and not (d / "contextnet.bin").exists())
    except OSError:
        return False


def _rife_dir_usable(d: Path) -> bool:
    """rife-ncnn-vulkan выбирает тип модели по ИМЕНИ папки (find("rife-v4")). Папка с именем v4,
    в которой лежит старая модель (так в пакете движков 2.4.7), даёт брак/падение — пропускаем."""
    if not d.is_dir():
        return False
    if "rife-v4" in d.name:
        return is_real_rife_v4_dir(d)
    return True


def _pick_fast_rife_dir(rife_parent: Path, anime: bool = False) -> Path:
    order = (
        ("rife-anime", "rife-v4.22-lite", "rife-UHD", "rife-v4.17-lite", "rife-v4.26")
        if anime
        else ("rife-v4.22-lite", "rife-UHD", "rife-v4.17-lite", "rife-v4.26")
    )
    for name in order + ("rife-anime", "rife-UHD", "rife-HD", "rife"):
        cand = rife_parent / name
        if _rife_dir_usable(cand):
            return cand
    return rife_parent / "rife-v4.26"


RIFE_MODEL_DIR = _pick_fast_rife_dir(RIFE_EXE.parent, anime=False)
RIFE_ANIME_DIR = _pick_fast_rife_dir(RIFE_EXE.parent, anime=True)

# v2.5.6: настоящая RIFE v4.6 (официальный rife-ncnn-vulkan 20221029, MIT). В пакете движков 2.4.7
# папки rife-v4.26 / rife-v4.22-lite / rife-UHD — копии старой rife-anime, поэтому проверяем
# устройство папки, а не имя. Замер RTX 5050 Laptop, 2K: rife-anime 7.3 кадр/с, v4.6 15–20 кадр/с;
# плюс v4 умеет любой шаг -> ровно 60 fps из 24/25/30.
RIFE_V4_ADDON_DIR = BASE_DIR / "addons" / "rife-v4.6"


def find_rife_v4_dir() -> Optional[Path]:
    """Папка настоящей RIFE v4.6 или None (тогда прежний путь x2). Выключатель: PANDA_RIFE_V4=0."""
    if os.environ.get("PANDA_RIFE_V4", "1").strip().lower() in ("0", "false", "off", "no"):
        return None
    for d in (RIFE_EXE.parent / "rife-v4.6", RIFE_V4_ADDON_DIR):
        if is_real_rife_v4_dir(d):
            return d
    return None

# ffmpeg кладём прямо в bin/ (см. panda_engines_pack.zip). На Windows это ffmpeg.exe,
# на сервере — просто ffmpeg. Проверяем оба имени, иначе на чистом ПК движок
# «не находился» и видео падало в запасной путь.
FFMPEG_EXE = _find_first(
    [
        Path(os.environ["FFMPEG_EXE"]) if os.environ.get("FFMPEG_EXE") else None,
        BASE_DIR / "bin" / "ffmpeg.exe",
        BASE_DIR / "bin" / "ffmpeg",
        WAIFU_ROOT / "ffmpeg.exe",
        WAIFU_ROOT / "ffmpeg",
        WAIFU_ROOT / "ffmpeg_waifu2xEX.exe",
        Path(shutil.which("ffmpeg")) if shutil.which("ffmpeg") else None,
    ],
    WAIFU_ROOT,
    "ffmpeg*",
)

FFPROBE_EXE = _find_first(
    [
        Path(os.environ["FFPROBE_EXE"]) if os.environ.get("FFPROBE_EXE") else None,
        BASE_DIR / "bin" / "ffprobe.exe",
        BASE_DIR / "bin" / "ffprobe",
        WAIFU_ROOT / "ffprobe.exe",
        WAIFU_ROOT / "ffprobe",
        WAIFU_ROOT / "ffprobe_waifu2xEX.exe",
        Path(shutil.which("ffprobe")) if shutil.which("ffprobe") else None,
    ],
    WAIFU_ROOT,
    "ffprobe*",
)

IMAGEMAGICK_EXE = _find_first(
    [
        Path(os.environ["IMAGEMAGICK_EXE"]) if os.environ.get("IMAGEMAGICK_EXE") else None,
        WAIFU_ROOT / "convert_waifu2xEX.exe",
        WAIFU_ROOT / "ImageMagick" / "convert_waifu2xEX.exe",
        Path(shutil.which("magick")) if shutil.which("magick") else None,
        Path(shutil.which("convert")) if shutil.which("convert") else None,
    ],
    WAIFU_ROOT,
    "convert*waifu2xEX.exe",
)

BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "").strip()
GPU_ID: int = int(os.environ.get("GPU_ID", "0"))
GPU_PIN_STRICT: bool = os.environ.get("GPU_PIN_STRICT", "0").strip().lower() in ("1", "true", "yes")
GPU_THREADS: str = os.environ.get("GPU_THREADS", "4:6:6").strip()
# Benchmark-verified thread profile for the Turbo 2x model only (same pixels, ~3.9% faster).
REALESRGAN_TURBO_THREADS: str = os.environ.get("REALESRGAN_TURBO_THREADS", "2:6:3").strip()
# Full-frame optical flow in RIFE does NOT tile: -j 4:6:6 exhausts 8 GB VRAM on 1080p/2K
# (vkAllocateMemory failed -2 -> 0xC0000005 / 3221225477). 1:2:2 is both fastest and crash-free.
RIFE_THREADS: str = os.environ.get("RIFE_THREADS", "1:2:2").strip()

# Tiered Caps: Free (720p, 30 FPS, 15s) vs Paid/VIP (1080p/2K/4K, 60 FPS, up to 120s)
FREE_VIDEO_MAX_SEC: int = int(os.environ.get("MAX_VIDEO_SECONDS_FREE", "15"))
PAID_PACK_VIDEO_MAX_SEC: int = 60
MAX_VIDEO_SECONDS_VIP: int = int(os.environ.get("MAX_VIDEO_SECONDS_VIP", "120"))

FREE_VIDEO_LONG_EDGE: int = 1280   # 720p HD (1280x720 or 720x1280)
PAID_VIDEO_LONG_EDGE: int = 1920   # 1080p Full HD (1920x1080 or 1080x1920)
PRO_VIDEO_LONG_EDGE: int = 2560    # 2K QHD Studio
# Нижняя планка размера кадра для ИИ-прохода: даже маленький исходник доводим хотя бы досюда,
# иначе видео 368px выходило 736px при тарифе 1080p (замер: конкурент на том же файле даёт 1024px).
VIDEO_AI_MIN_EDGE: int = int(os.environ.get("VIDEO_AI_MIN_EDGE", "1024"))
# Потолок кадров для удвоения через RIFE. Почему: удвоение считается на каждый кадр и почти
# удваивает время задачи — в логах были клипы на 394–421 с против 116 с без него. Держим
# удвоение только там, где оно укладывается в бюджет (примерно 16 с клипа при 60 к/с).
RIFE_MAX_OUT_FRAMES: int = int(os.environ.get("RIFE_MAX_OUT_FRAMES", "2000"))

FREE_PHOTO_TARGET_PX: int = 1280   # 720p HD class on Free tier
MAX_PHOTO_TARGET_PX: int = int(os.environ.get("MAX_PHOTO_TARGET_PX", "3840"))  # Full 4K Ultra on Paid/VIP

JOB_RETENTION_HOURS: float = float(os.environ.get("JOB_RETENTION_HOURS", "4"))

# Immutable Super-Owner ID (cannot be removed by co-admins)
SUPER_ADMIN_ID: int = 8715694435
_raw_admins = os.environ.get("ADMIN_IDS", str(SUPER_ADMIN_ID)).strip()
ADMIN_IDS: set[int] = {SUPER_ADMIN_ID} | {
    int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()
}

# Legacy internal LAN cluster settings (kept for compatibility; do NOT expose publicly)
_raw_workers = os.environ.get("REMOTE_WORKERS", "").strip()
REMOTE_WORKERS: List[str] = [w.strip().rstrip("/") for w in _raw_workers.split(",") if w.strip()]
CLUSTER_SECRET: str = os.environ.get("CLUSTER_SECRET", "colibri-cluster-local-key").strip()
WORKER_PORT: int = int(os.environ.get("WORKER_PORT", "8787"))

# Panda Worker / ARM Server Dispatcher Configuration
# - "local": default standalone mode (runs GPU pipelines locally on PC)
# - "server": headless ARM/VPS dispatcher mode (bot + queue + worker API; NO AI runs on server)
DISPATCH_MODE: str = os.environ.get("DISPATCH_MODE", "local").strip().lower()
DISPATCH_HOST: str = os.environ.get("DISPATCH_HOST", "127.0.0.1").strip()
DISPATCH_PORT: int = int(os.environ.get("DISPATCH_PORT", "8790"))
DISPATCH_LEASE_SEC: int = int(os.environ.get("DISPATCH_LEASE_SEC", "45"))
DISPATCH_MAX_ATTEMPTS: int = int(os.environ.get("DISPATCH_MAX_ATTEMPTS", "2"))
ZERO_RETENTION_SERVER: bool = os.environ.get(
    "ZERO_RETENTION_SERVER", "1" if DISPATCH_MODE == "server" else "0"
).strip().lower() in ("1", "true", "yes")
ALLOW_VOLUNTEER_WORKERS: bool = os.environ.get("ALLOW_VOLUNTEER_WORKERS", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)
MONTHLY_EGRESS_BUDGET_TB: float = float(os.environ.get("MONTHLY_EGRESS_BUDGET_TB", "10.0"))

# Telegram Bot API endpoint & Local Bot API (--local up to 2000 MB) support
TG_API_BASE_URL: str = os.environ.get("TG_API_BASE_URL", "https://api.telegram.org").strip().rstrip("/")
TG_FILE_BASE_URL: str = os.environ.get("TG_FILE_BASE_URL", TG_API_BASE_URL).strip().rstrip("/")
TG_LOCAL_MODE: bool = os.environ.get("TG_LOCAL_MODE", "0").strip().lower() in ("1", "true", "yes")
INBOUND_MAX_MB: int = int(os.environ.get("INBOUND_MAX_MB", "500" if TG_LOCAL_MODE else "20"))
OUTBOUND_MAX_MB: int = int(os.environ.get("OUTBOUND_MAX_MB", "1950" if TG_LOCAL_MODE else "50"))

IS_WINDOWS = sys.platform.startswith("win")
# CREATE_NO_WINDOW + BELOW_NORMAL_PRIORITY_CLASS (v2.5.7): движки ИИ не отнимают процессор у игр
# и программ пользователя. Выключатель: PANDA_LOW_PRIORITY=0.
SUBPROCESS_FLAGS = (0x08000000 | (0x00004000 if os.environ.get("PANDA_LOW_PRIORITY", "1") != "0" else 0)) if IS_WINDOWS else 0

_VULKAN_GPU_LOCK = threading.Lock()
_VULKAN_GPU_CACHE: Tuple[float, int, str] = (0.0, GPU_ID, "")
_DISCRETE_GPU_TOKENS = (
    "nvidia",
    "geforce",
    "rtx",
    "gtx",
    "quadro",
    "tesla",
    "radeon rx",
    "radeon pro",
    "arc a",
    "arc b",
)
_INTEGRATED_GPU_TOKENS = (
    "intel(r) uhd",
    "intel(r) iris",
    "raptorlake",
    "alderlake",
    "meteorlake",
    "hd graphics",
    "radeon(tm) graphics",
    "llvmpipe",
    "lavapipe",
    "microsoft basic",
)


def list_vulkan_devices() -> List[Tuple[int, str]]:
    """Enumerate Vulkan physical devices reported by ncnn binaries (`[idx Name]`)."""
    probe_exe: Optional[Path] = None
    for cand in (RIFE_EXE, REALESRGAN_EXE, REALCUGAN_EXE):
        if cand.is_file():
            probe_exe = cand
            break
    if probe_exe is None:
        return []
    args = [str(probe_exe), "-l"] if probe_exe == RIFE_EXE else [str(probe_exe), "-h"]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=SUBPROCESS_FLAGS,
        )
        raw = f"{proc.stdout}\n{proc.stderr}"
    except Exception:
        return []
    seen: Dict[int, str] = {}
    for m in re.finditer(r"\[(\d+)\s+([^\]]+)\]", raw):
        idx = int(m.group(1))
        name = m.group(2).strip()
        # Keep only device-header lines (ignore capability continuation lines if already recorded)
        if idx not in seen and name:
            seen[idx] = name
    return sorted(seen.items(), key=lambda x: x[0])


def resolve_vulkan_gpu_id(force_refresh: bool = False, ttl_sec: float = 45.0) -> int:
    """
    Dynamically select the discrete Vulkan GPU index on Optimus/hybrid laptops.
    Windows WDDM can swap Vulkan device [0] <-> [1] between Intel iGPU and NVIDIA dGPU
    live on lid-close/power transitions without restarting the bot.
    """
    global _VULKAN_GPU_CACHE
    if GPU_ID < 0 or GPU_PIN_STRICT:
        return GPU_ID
    now = time.monotonic()
    with _VULKAN_GPU_LOCK:
        cached_ts, cached_id, _ = _VULKAN_GPU_CACHE
        if not force_refresh and cached_ts > 0.0 and (now - cached_ts) < ttl_sec:
            return cached_id
        devices = list_vulkan_devices()
        if not devices:
            _VULKAN_GPU_CACHE = (now, GPU_ID, "")
            return GPU_ID
        dev_map = dict(devices)
        # If configured GPU_ID is already a discrete GPU, honor it
        if GPU_ID in dev_map:
            low_cfg = dev_map[GPU_ID].lower()
            if any(t in low_cfg for t in _DISCRETE_GPU_TOKENS) and not any(
                t in low_cfg for t in _INTEGRATED_GPU_TOKENS
            ):
                _VULKAN_GPU_CACHE = (now, GPU_ID, dev_map[GPU_ID])
                return GPU_ID
        # Otherwise pick the first discrete GPU
        for idx, name in devices:
            low = name.lower()
            if any(t in low for t in _DISCRETE_GPU_TOKENS) and not any(
                t in low for t in _INTEGRATED_GPU_TOKENS
            ):
                _VULKAN_GPU_CACHE = (now, idx, name)
                return idx
        # Fallback: first non-integrated device, else configured GPU_ID if present
        for idx, name in devices:
            low = name.lower()
            if not any(t in low for t in _INTEGRATED_GPU_TOKENS):
                _VULKAN_GPU_CACHE = (now, idx, name)
                return idx
        chosen = GPU_ID if GPU_ID in dev_map else devices[0][0]
        _VULKAN_GPU_CACHE = (now, chosen, dev_map.get(chosen, ""))
        return chosen

# Subscription Tier Definitions (daily caps, max video length, resolution, priority)
SUB_TIER_SPECS: Dict[str, Dict[str, Any]] = {
    "lite": {
        "daily_photos": 30,
        "daily_videos": 10,
        "max_video_sec": 45,
        "video_long_edge": PAID_VIDEO_LONG_EDGE,
        "priority": 3,
        "badge": "Panda Lite",
    },
    "vip": {
        "daily_photos": 100,
        "daily_videos": 35,
        "max_video_sec": 90,
        "video_long_edge": PAID_VIDEO_LONG_EDGE,
        "priority": 2,
        "badge": "Panda VIP",
    },
    "pro": {
        "daily_photos": 250,
        "daily_videos": 80,
        "max_video_sec": 120,
        "video_long_edge": PRO_VIDEO_LONG_EDGE,
        "priority": 1,
        "badge": "Panda PRO",
    },
}

# ---------------------------------------------------------------------------
# 14 Commercial Plans across 4 Categories:
#   1) 'combo' — Photo + Video together (32%-36% CHEAPER than buying separately!)
#   2) 'sub'   — Subscriptions (7d Lite, 7d VIP, 30d VIP, 30d PRO, 90d VIP)
#   3) 'photo' — Separate Photo-Only 4K packs (never expire)
#   4) 'video' — Separate Video-Only 1080p 60FPS packs (never expire)
# ---------------------------------------------------------------------------
TARIFF_PLANS: Dict[str, Dict[str, Any]] = {
    # === COMBO PACKS (32% - 36% cheaper than separate photo + video!) ===
    "cb_start": {
        "category": "combo",
        "photos": 30,
        "videos": 5,
        "vip_days": 0,
        "sub_tier": "",
        "max_video_sec": 60,
        "stars": 75,
        "old_stars": 110,     # 45 (ph_30) + 65 (vd_5) = 110 Stars separately!
        "kzt": 750,
        "old_kzt": 1100,
        "discount_pct": 32,
        "name_ru": "Комбо S: 30 Фото 4K + 5 Видео 60FPS",
        "name_kk": "Комбо S: 30 Фото 4K + 5 Видео 60FPS",
        "name_en": "Combo S: 30 Photos 4K + 5 Videos 60FPS",
    },
    "cb_optima": {
        "category": "combo",
        "photos": 100,
        "videos": 15,
        "vip_days": 0,
        "sub_tier": "",
        "max_video_sec": 60,
        "stars": 195,
        "old_stars": 290,     # 120 (ph_100) + 170 (vd_15) = 290 Stars separately!
        "kzt": 1950,
        "old_kzt": 2880,
        "discount_pct": 33,
        "popular": True,
        "name_ru": "Комбо M (ХИТ): 100 Фото 4K + 15 Видео 60FPS",
        "name_kk": "Комбо M (ХИТ): 100 Фото 4K + 15 Видео 60FPS",
        "name_en": "Combo M (BEST): 100 Photos 4K + 15 Videos 60FPS",
    },
    "cb_creator": {
        "category": "combo",
        "photos": 300,
        "videos": 50,
        "vip_days": 0,
        "sub_tier": "",
        "max_video_sec": 90,
        "stars": 499,
        "old_stars": 780,     # 290 (ph_300) + 490 (vd_50) = 780 Stars separately!
        "kzt": 4990,
        "old_kzt": 7780,
        "discount_pct": 36,
        "name_ru": "Комбо XL: 300 Фото 4K + 50 Видео 60FPS",
        "name_kk": "Комбо XL: 300 Фото 4K + 50 Видео 60FPS",
        "name_en": "Combo XL: 300 Photos 4K + 50 Videos 60FPS",
    },

    # === SUBSCRIPTIONS (Priority Queue + 1080p/2K + 60 FPS + High Daily Caps) ===
    "sub_lite_7": {
        "category": "sub",
        "photos": 0,
        "videos": 0,
        "vip_days": 7,
        "sub_tier": "lite",
        "max_video_sec": 45,
        "stars": 160,
        "old_stars": 0,
        "kzt": 1590,
        "old_kzt": 0,
        "discount_pct": 0,
        "name_ru": "Panda Lite — 7 дней (30 фото + 10 видео/день, 60FPS)",
        "name_kk": "Panda Lite — 7 күн (күніне 30 фото + 10 видео, 60FPS)",
        "name_en": "Panda Lite — 7 Days (30 photos + 10 videos/day, 60FPS)",
    },
    "sub_vip_7": {
        "category": "sub",
        "photos": 0,
        "videos": 0,
        "vip_days": 7,
        "sub_tier": "vip",
        "max_video_sec": 90,
        "stars": 250,
        "old_stars": 0,
        "kzt": 2490,
        "old_kzt": 0,
        "discount_pct": 0,
        "name_ru": "Panda VIP — 7 дней (Вне очереди, 1080p 60FPS)",
        "name_kk": "Panda VIP — 7 күн (Кезексіз, 1080p 60FPS)",
        "name_en": "Panda VIP — 7 Days (Skip Queue, 1080p 60FPS)",
    },
    "sub_vip_30": {
        "category": "sub",
        "photos": 0,
        "videos": 0,
        "vip_days": 30,
        "sub_tier": "vip",
        "max_video_sec": 90,
        "stars": 590,
        "old_stars": 1000,
        "kzt": 5890,
        "old_kzt": 9960,
        "discount_pct": 41,
        "popular": True,
        "name_ru": "Panda VIP — 30 дней (100 фото + 35 видео/день, Вне очереди)",
        "name_kk": "Panda VIP — 30 күн (күніне 100 фото + 35 видео, Кезексіз)",
        "name_en": "Panda VIP — 30 Days (100 photos + 35 videos/day, Priority)",
    },
    "sub_pro_30": {
        "category": "sub",
        "photos": 0,
        "videos": 0,
        "vip_days": 30,
        "sub_tier": "pro",
        "max_video_sec": 120,
        "stars": 1190,
        "old_stars": 0,
        "kzt": 11890,
        "old_kzt": 0,
        "discount_pct": 0,
        "name_ru": "Panda PRO Studio — 30 дней (2K 60FPS, до 120 сек, Приоритет №1)",
        "name_kk": "Panda PRO Studio — 30 күн (2K 60FPS, 120 сек дейін, №1 Кезек)",
        "name_en": "Panda PRO Studio — 30 Days (2K 60FPS, up to 120s, #1 Priority)",
    },
    "sub_vip_90": {
        "category": "sub",
        "photos": 0,
        "videos": 0,
        "vip_days": 90,
        "sub_tier": "vip",
        "max_video_sec": 90,
        "stars": 1390,
        "old_stars": 1770,
        "kzt": 13890,
        "old_kzt": 17670,
        "discount_pct": 22,
        "name_ru": "Panda VIP — 90 дней (Квартал -22%)",
        "name_kk": "Panda VIP — 90 күн (Тоқсан -22%)",
        "name_en": "Panda VIP — 90 Days (Quarter -22%)",
    },

    # === SEPARATE PHOTO-ONLY PACKS (4K Ultra, never expire) ===
    "ph_30": {
        "category": "photo",
        "photos": 30,
        "videos": 0,
        "vip_days": 0,
        "sub_tier": "",
        "max_video_sec": 15,
        "stars": 45,
        "old_stars": 0,
        "kzt": 450,
        "old_kzt": 0,
        "discount_pct": 0,
        "name_ru": "30 Фото в 4K Ultra (без срока)",
        "name_kk": "4K Ultra сапада 30 Фото (мерзімсіз)",
        "name_en": "30 Photos in 4K Ultra (never expire)",
    },
    "ph_100": {
        "category": "photo",
        "photos": 100,
        "videos": 0,
        "vip_days": 0,
        "sub_tier": "",
        "max_video_sec": 15,
        "stars": 120,
        "old_stars": 0,
        "kzt": 1190,
        "old_kzt": 0,
        "discount_pct": 0,
        "name_ru": "100 Фото в 4K Ultra (без срока)",
        "name_kk": "4K Ultra сапада 100 Фото (мерзімсіз)",
        "name_en": "100 Photos in 4K Ultra (never expire)",
    },
    "ph_300": {
        "category": "photo",
        "photos": 300,
        "videos": 0,
        "vip_days": 0,
        "sub_tier": "",
        "max_video_sec": 15,
        "stars": 290,
        "old_stars": 0,
        "kzt": 2890,
        "old_kzt": 0,
        "discount_pct": 0,
        "name_ru": "300 Фото в 4K Ultra (без срока)",
        "name_kk": "4K Ultra сапада 300 Фото (мерзімсіз)",
        "name_en": "300 Photos in 4K Ultra (never expire)",
    },

    # === SEPARATE VIDEO-ONLY PACKS (1080p Full HD + 60 FPS, never expire) ===
    "vd_5": {
        "category": "video",
        "photos": 0,
        "videos": 5,
        "vip_days": 0,
        "sub_tier": "",
        "max_video_sec": 60,
        "stars": 65,
        "old_stars": 0,
        "kzt": 650,
        "old_kzt": 0,
        "discount_pct": 0,
        "name_ru": "5 Видео 1080p + 60 FPS (до 60 сек)",
        "name_kk": "5 Видео 1080p + 60 FPS (60 сек дейін)",
        "name_en": "5 Videos 1080p + 60 FPS (up to 60s)",
    },
    "vd_15": {
        "category": "video",
        "photos": 0,
        "videos": 15,
        "vip_days": 0,
        "sub_tier": "",
        "max_video_sec": 60,
        "stars": 170,
        "old_stars": 0,
        "kzt": 1690,
        "old_kzt": 0,
        "discount_pct": 0,
        "name_ru": "15 Видео 1080p + 60 FPS (до 60 сек)",
        "name_kk": "15 Видео 1080p + 60 FPS (60 сек дейін)",
        "name_en": "15 Videos 1080p + 60 FPS (up to 60s)",
    },
    "vd_50": {
        "category": "video",
        "photos": 0,
        "videos": 50,
        "vip_days": 0,
        "sub_tier": "",
        "max_video_sec": 90,
        "stars": 490,
        "old_stars": 0,
        "kzt": 4890,
        "old_kzt": 0,
        "discount_pct": 0,
        "name_ru": "50 Видео 1080p + 60 FPS (до 90 сек)",
        "name_kk": "50 Видео 1080p + 60 FPS (90 сек дейін)",
        "name_en": "50 Videos 1080p + 60 FPS (up to 90s)",
    },
}
