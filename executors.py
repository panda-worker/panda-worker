# -*- coding: utf-8 -*-
"""
panda_worker/executors.py
Strict allowlist task executors for Panda Worker v2.0.
Lazy-loads `engines/` modules only when executing a task, so `PandaWorkerSetup.exe`
starts cleanly on a brand-new PC before `panda_engines_pack.zip` is downloaded!
"""
from __future__ import annotations

import dataclasses  # noqa: F401 — bundled for dynamic engines/*.py inside PyInstaller
import importlib
import inspect
import math  # noqa: F401
import sys
import uuid  # noqa: F401
import zipfile  # noqa: F401
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

try:
    from PIL import (  # noqa: F401 — ensure PyInstaller bundles all PIL submodules used by engines/
        Image,
        ImageChops,
        ImageDraw,
        ImageEnhance,
        ImageFilter,
        ImageFont,
        ImageOps,
        ImageStat,
    )
except Exception:
    pass

import worker_config as wcfg
from panda_protocol import (
    TASK_PHOTO_ENHANCE,
    TASK_PHOTO_TOOL,
    TASK_SQUEEZE_MEDIA,
    TASK_VIDEO_ENHANCE,
    TASK_VIDEO_TOOL,
    pack_result_bundle,
    validate_job_payload,
)


class NonRetryableMediaError(RuntimeError):
    """Raised when input media itself is corrupt/unsupported so server refunds immediately."""


def _ensure_engine_imports() -> None:
    if str(wcfg.ENGINES_DIR) not in sys.path:
        sys.path.insert(0, str(wcfg.ENGINES_DIR))


def execute_whitelisted_task(
    task_type: str,
    raw_params: Dict[str, Any],
    input_path: Path,
    work_dir: Path,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Tuple[Path, bool, Dict[str, Any]]:
    """
    Validate and run a whitelisted task inside `work_dir`.
    Returns `(bundle_zip_path, True, result_meta_dict)`.
    """
    _ensure_engine_imports()
    from compressor import compress_photo, compress_video  # type: ignore
    from photo_engine import (  # type: ignore
        colorize_bw_photo,
        cutout_background_png,
        enhance_photo_pipeline,
        portrait_blur_bg,
    )
    from video_engine import (  # type: ignore
        enhance_video_pipeline,
        make_hd_gif,
        make_vertical_9_16,
        make_video_circle,
    )

    params = validate_job_payload(task_type, raw_params)
    stage_dir = work_dir / "stage"
    stage_dir.mkdir(parents=True, exist_ok=True)
    bundle_zip = work_dir / "result.zip"

    if task_type == TASK_VIDEO_ENHANCE:
        if "mastering_mode" not in inspect.signature(enhance_video_pipeline).parameters:
            import video_engine as _ve_mod  # type: ignore
            importlib.reload(_ve_mod)
            enhance_video_pipeline = _ve_mod.enhance_video_pipeline
        v_kw: Dict[str, Any] = {
            "video_model": params["video_model"],
            "enable_60fps": params["enable_60fps"],
            "color_enhance": params["color_enhance"],
            "max_seconds": params["max_seconds"],
            "target_long_edge": params["target_long_edge"],
            "is_paid_tier": params["is_paid_tier"],
            "smart_hevc": params["smart_hevc"],
            "lang": params["lang"],
            "sharpness_mode": params["sharpness_mode"],
            "mastering_mode": params["mastering_mode"],
            "on_progress": on_progress,
        }
        v_sig = inspect.signature(enhance_video_pipeline).parameters
        res = enhance_video_pipeline(
            input_path,
            stage_dir,
            **{k: v for k, v in v_kw.items() if k in v_sig},
        )
        out_mp4 = Path(res["out_path"])
        meta = {
            "out_name": out_mp4.name,
            "orig_size": list(res["orig_size"]),
            "final_size": list(res["final_size"]),
            "orig_fps": float(res["orig_fps"]),
            "final_fps": float(res["final_fps"]),
            "duration": float(res["duration"]),
            "ai_category": str(res.get("ai_category") or "irl"),
            "engine_tag": str(res.get("engine_tag") or ""),
            "rife_applied": bool(res.get("rife_applied")),
            "elapsed_sec": float(res.get("elapsed_sec") or 0.0),
        }
        pack_result_bundle(bundle_zip, meta, {out_mp4.name: out_mp4})
        return bundle_zip, True, meta

    if task_type == TASK_PHOTO_ENHANCE:
        if "mastering_mode" not in inspect.signature(enhance_photo_pipeline).parameters:
            import photo_engine as _pe_mod  # type: ignore
            importlib.reload(_pe_mod)
            enhance_photo_pipeline = _pe_mod.enhance_photo_pipeline
        p_kw: Dict[str, Any] = {
            "photo_model": params["photo_model"],
            "sharpness_mode": params["sharpness_mode"],
            "color_enhance": params["color_enhance"],
            "anti_blur": params["anti_blur"],
            "scale_factor": params["scale_factor"],
            "max_target_px": params["max_target_px"],
            "build_split": params["build_split"],
            "mastering_mode": params["mastering_mode"],
            "on_progress": on_progress,
        }
        p_sig = inspect.signature(enhance_photo_pipeline).parameters
        if "mastering_mode" not in p_sig and p_kw["sharpness_mode"] not in ("raw", "balanced", "vivid"):
            p_kw["sharpness_mode"] = "balanced" if p_kw["sharpness_mode"] == "soft" else "vivid"
        res = enhance_photo_pipeline(
            input_path,
            stage_dir,
            **{k: v for k, v in p_kw.items() if k in p_sig},
        )
        doc_p = Path(res["doc_path"])
        prev_p = Path(res["preview_path"])
        files_map: Dict[str, Path] = {doc_p.name: doc_p, prev_p.name: prev_p}
        comp_name = ""
        if res.get("comp_path") and Path(res["comp_path"]).is_file():
            cp = Path(res["comp_path"])
            comp_name = cp.name
            files_map[comp_name] = cp
        meta = {
            "doc_name": doc_p.name,
            "preview_name": prev_p.name,
            "comp_name": comp_name,
            "orig_size": list(res["orig_size"]),
            "final_size": list(res["final_size"]),
            "ai_category": str(res.get("ai_category") or "irl"),
            "engine_used": str(res.get("engine_used") or ""),
            "elapsed_sec": float(res.get("elapsed_sec") or 0.0),
        }
        pack_result_bundle(bundle_zip, meta, files_map)
        return bundle_zip, True, meta

    if task_type == TASK_PHOTO_TOOL:
        tool = params["tool"]
        if tool == "cutout_bg":
            res = cutout_background_png(input_path, stage_dir)
        elif tool == "blur_bg":
            res = portrait_blur_bg(input_path, stage_dir)
        else:
            res = colorize_bw_photo(input_path, stage_dir)
        doc_p = Path(res["doc_path"])
        prev_p = Path(res.get("preview_path") or doc_p)
        files_map = {doc_p.name: doc_p, prev_p.name: prev_p}
        meta = {
            "doc_name": doc_p.name,
            "preview_name": prev_p.name,
            "final_size": list(res.get("final_size") or [0, 0]),
            "elapsed_sec": float(res.get("elapsed_sec") or 0.0),
        }
        pack_result_bundle(bundle_zip, meta, files_map)
        return bundle_zip, True, meta

    if task_type == TASK_VIDEO_TOOL:
        tool = params["tool"]
        if tool == "video_9_16":
            out_v = make_vertical_9_16(input_path, stage_dir)
        elif tool == "video_gif":
            out_v = make_hd_gif(input_path, stage_dir)
        else:
            out_v = make_video_circle(input_path, stage_dir)
        meta = {"out_name": out_v.name}
        pack_result_bundle(bundle_zip, meta, {out_v.name: out_v})
        return bundle_zip, True, meta

    if task_type == TASK_SQUEEZE_MEDIA:
        kind = params["kind"]
        sq = compress_video(input_path, stage_dir) if kind == "video" else compress_photo(input_path, stage_dir)
        if sq is None:
            marker = stage_dir / "optimal.txt"
            marker.write_bytes(b"OPTIMAL")
            meta = {"out_name": "optimal.txt", "already_optimal": True}
            pack_result_bundle(bundle_zip, meta, {"optimal.txt": marker})
            return bundle_zip, True, meta
        out_f = Path(sq.path)
        meta = {
            "out_name": out_f.name,
            "already_optimal": False,
            "original_bytes": int(sq.original_bytes),
            "result_bytes": int(sq.result_bytes),
            "saved_fraction": float(sq.saved_fraction),
            "mode": str(sq.mode),
            "psnr_db": float(getattr(sq, "psnr_db", 0.0) or 0.0),
        }
        pack_result_bundle(bundle_zip, meta, {out_f.name: out_f})
        return bundle_zip, True, meta

    raise ValueError(f"Unsupported task_type: {task_type}")


def is_error_retryable(exc: BaseException) -> bool:
    """Distinguish transient/node errors (requeue to another GPU) from bad input files."""
    if isinstance(exc, NonRetryableMediaError):
        return False
    if type(exc).__name__ == "VulkanOutOfMemoryError":
        return True
    msg = str(exc).lower()
    non_retry_markers = (
        "повреждён",
        "не содержит видеопотока",
        "cannot identify image file",
        "truncated",
        "decompressionbomb",
        "unsupported format",
    )
    if any(m in msg for m in non_retry_markers):
        return False
    return True
