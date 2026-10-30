# -*- coding: utf-8 -*-
"""
panda_worker/worker_config.py
Configuration, Persistent State & Hot-Overlay MetaPath Finder for Panda Worker v2.0.
Contains ZERO server secrets (no BOT_TOKEN, no SQLite DB, no payment keys).
"""
from __future__ import annotations

import importlib.abc
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

_STATE_LOCK = threading.RLock()
_LAST_KNOWN_GOOD_STATE: Dict[str, Any] = {}

IS_FROZEN: bool = bool(getattr(sys, "frozen", False))

if IS_FROZEN:
    BUNDLE_DIR: Path = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    EXE_PATH: Path = Path(sys.executable).resolve()
    _default_root = Path(os.environ.get("LOCALAPPDATA", str(EXE_PATH.parent))) / "PandaWorker"
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    EXE_PATH = Path(__file__).resolve()
    _default_root = BUNDLE_DIR

WORKER_ROOT: Path = Path(os.environ.get("PANDA_WORKER_HOME", str(_default_root))).resolve()
WORKER_ROOT.mkdir(parents=True, exist_ok=True)

HOT_CORE_DIR: Path = WORKER_ROOT / "core_current"
HOT_PREV_DIR: Path = WORKER_ROOT / "core_prev"
ENGINES_DIR: Path = Path(os.environ.get("PANDA_ENGINES_DIR", str(WORKER_ROOT / "engines"))).resolve()


class _HotOverlayFinder(importlib.abc.MetaPathFinder):
    """
    Placed BEFORE PyInstaller's `PyiFrozenImporter` in `sys.meta_path` so hot-updated
    `.py` files in `%LOCALAPPDATA%\\PandaWorker\\core_current` ALWAYS override frozen code
    without ever reinstalling `.exe`!
    """

    def find_spec(self, fullname: str, path: Any = None, target: Any = None):
        if "." in fullname or fullname == "worker_config":
            return None
        for cand_dir in (HOT_CORE_DIR, HOT_CORE_DIR / "engines", ENGINES_DIR):
            candidate = cand_dir / f"{fullname}.py"
            if candidate.is_file():
                return importlib.util.spec_from_file_location(fullname, str(candidate))
        return None


if not any(type(f).__name__ == "_HotOverlayFinder" for f in sys.meta_path):
    sys.meta_path.insert(0, _HotOverlayFinder())

# Maintain clean priority order: HOT_CORE_DIR -> ENGINES_DIR -> BUNDLE_DIR
for _p in reversed((HOT_CORE_DIR, ENGINES_DIR, BUNDLE_DIR)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

STATE_FILE: Path = WORKER_ROOT / "state.json"


def _load_worker_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# Load user local `.env` first, then clean public defaults bundled inside `.exe`
_load_worker_env(WORKER_ROOT / ".env")
_load_worker_env(BUNDLE_DIR / "public_client.env")
if not IS_FROZEN and BUNDLE_DIR != WORKER_ROOT:
    _load_worker_env(BUNDLE_DIR / ".env")


def load_state() -> Dict[str, Any]:
    with _STATE_LOCK:
        if not STATE_FILE.is_file():
            return {}
        for attempt in range(5):
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _LAST_KNOWN_GOOD_STATE.clear()
                    _LAST_KNOWN_GOOD_STATE.update(data)
                    return dict(data)
            except OSError:
                time.sleep(0.05 * (attempt + 1))
            except Exception:
                break
        return dict(_LAST_KNOWN_GOOD_STATE)


def save_state(updates: Dict[str, Any]) -> Dict[str, Any]:
    with _STATE_LOCK:
        cur = load_state()
        cur.update(updates)
        WORKER_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(f"state.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        last_err: Exception | None = None
        for attempt in range(6):
            try:
                tmp.replace(STATE_FILE)
                last_err = None
                break
            except OSError as exc:
                last_err = exc
                time.sleep(0.06 * (attempt + 1))
        if last_err is not None:
            tmp.unlink(missing_ok=True)
        return cur


_STATE = load_state()

os.environ["DISPATCH_MODE"] = "local"
os.environ.setdefault("GPU_PIN_STRICT", "0")
os.environ.setdefault("GPU_THREADS", "2:2:2")
os.environ.setdefault("RIFE_THREADS", "1:2:2")

SERVER_URL: str = (
    os.environ.get("SERVER_URL")
    or str(_STATE.get("server_url") or "")
    or "http://127.0.0.1:8790"
).strip().rstrip("/")

WORKER_TOKEN: str = (
    os.environ.get("WORKER_TOKEN")
    or str(_STATE.get("worker_token") or "")
).strip()

NODE_LABEL: str = os.environ.get("NODE_LABEL", str(_STATE.get("node_label") or "")).strip()

WORK_DIR: Path = Path(os.environ.get("WORK_DIR", str(WORKER_ROOT / "work_tmp")))
LOG_DIR: Path = Path(os.environ.get("LOG_DIR", str(WORKER_ROOT / "logs")))
DOWNLOAD_DIR: Path = WORKER_ROOT / "downloads"
PAUSE_FLAG_FILE: Path = WORKER_ROOT / "pause.flag"

MAX_TEMP_C: int = int(os.environ.get("MAX_TEMP_C", "84"))
COOLDOWN_TEMP_C: int = int(os.environ.get("COOLDOWN_TEMP_C", "76"))
MAX_CONCURRENT_VIDEO: int = max(1, int(os.environ.get("MAX_CONCURRENT_VIDEO", "1")))
PAUSE_ON_BATTERY: bool = os.environ.get("PAUSE_ON_BATTERY", "1").strip() in ("1", "true", "yes", "on")

GAMING_PROCESS_NAMES: List[str] = [
    p.strip().lower()
    for p in os.environ.get(
        "GAMING_PROCESS_NAMES",
        "cs2.exe,dota2.exe,valorant-win64-shipping.exe,gta5.exe,cyberpunk2077.exe,eldenring.exe,fortniteclient-win64-shipping.exe",
    ).split(",")
    if p.strip()
]

HEARTBEAT_SEC: float = max(5.0, float(os.environ.get("HEARTBEAT_SEC", "15")))
IDLE_POLL_MIN_SEC: float = max(0.5, float(os.environ.get("IDLE_POLL_MIN_SEC", "2.5")))
IDLE_POLL_MAX_SEC: float = max(IDLE_POLL_MIN_SEC, float(os.environ.get("IDLE_POLL_MAX_SEC", "6.0")))
BACKOFF_MAX_SEC: float = max(10.0, float(os.environ.get("BACKOFF_MAX_SEC", "35.0")))

TLS_VERIFY: bool = os.environ.get("TLS_VERIFY", "1").strip() not in ("0", "false", "no", "off")
RELEASE_PUBKEY_HEX: str = os.environ.get("RELEASE_PUBKEY_HEX", "").strip()

BASE_WORKER_VERSION: str = "2.2.0"
WORKER_VERSION: str = str(_STATE.get("installed_version") or BASE_WORKER_VERSION)


def _ver_tuple(v: str) -> tuple[int, int, int]:
    parts = [int("".join(c for c in p if c.isdigit()) or "0") for p in str(v or "0.0.0").lstrip("v").split(".")[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def get_runtime_version() -> str:
    st = load_state()
    inst = str(st.get("installed_version") or "")
    if inst and _ver_tuple(inst) > _ver_tuple(BASE_WORKER_VERSION):
        return inst
    return BASE_WORKER_VERSION


def apply_load_mode_threads(gpu_threads: str, rife_threads: str, max_temp_c: int = 84) -> None:
    """Dynamically adjust Vulkan thread counts & thermal cap without restarting the process."""
    global MAX_TEMP_C
    os.environ["GPU_THREADS"] = str(gpu_threads)
    os.environ["RIFE_THREADS"] = str(rife_threads)
    MAX_TEMP_C = int(max_temp_c)
    if "config" in sys.modules:
        try:
            eng_cfg = sys.modules["config"]
            eng_cfg.GPU_THREADS = str(gpu_threads)
            eng_cfg.RIFE_THREADS = str(rife_threads)
        except Exception:
            pass


for _d in (WORK_DIR, LOG_DIR, DOWNLOAD_DIR, ENGINES_DIR):
    _d.mkdir(parents=True, exist_ok=True)
