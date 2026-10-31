# -*- coding: utf-8 -*-
"""
Panda 4K AI — Commercial Video Engine (v3.0):
- Automatic AI Content Detection (Anime / 3D Cartoon / Movie / Live Video)
- Tiered Quality & FPS Gating:
    * Free Tier: 720p HD (1280px max), max 30 FPS, up to 15s (ultra-fast ~12-16s GPU pass!)
    * Paid / VIP: 1080p Full HD / 2K QHD + Smooth 60 FPS RIFE + up to 120s
- 100% Dedicated GPU saturation (-j 4:6:6, -threads 0) + disk-backed stderr logs
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import requests
from PIL import Image, ImageStat

from config import (
    DISPATCH_MODE,
    FFMPEG_EXE,
    FFPROBE_EXE,
    GPU_ID,
    GPU_THREADS,
    REALESRGAN_TURBO_THREADS,
    RIFE_THREADS,
    MAX_VIDEO_SECONDS_VIP,
    RIFE_MAX_OUT_FRAMES,
    PAID_VIDEO_LONG_EDGE,
    VIDEO_AI_MIN_EDGE,
    REALESRGAN_EXE,
    REALESRGAN_MODELS,
    RIFE_ANIME_DIR,
    RIFE_EXE,
    RIFE_MODEL_DIR,
    JOBS_DIR,
    SUBPROCESS_FLAGS,
    ZERO_RETENTION_SERVER,
    resolve_vulkan_gpu_id,
)
from content_ai import classify_video_content
from i18n import ai_cat_name

log = logging.getLogger("panda.video")

# Windows STATUS_ACCESS_VIOLATION (0xC0000005) unsigned & signed exit codes
_ACCESS_VIOLATION_CODES = (3221225477, -1073741819)
_VULKAN_FATAL_MARKERS = (
    "vkallocatememory failed",
    "vkqueuesubmit failed",
    "vkwaitforfences failed",
    "vkcreatecomputepipelines failed",
    "out of device memory",
    "device lost",
)


class VulkanOutOfMemoryError(RuntimeError):
    """Raised when Vulkan exhausts GPU VRAM or loses device even after conservative retry."""


def _video_model_candidates(effective_model: str, requested_model: str, ai_category: str,
                            is_paid_tier: bool, model_dir: Path) -> list[str]:
    """Fast free auto-anime canary, with a strict fallback and a kill switch.

    Manual model selection and every paid tier retain the original models. Never
    select a model if its weights OR its ncnn parameter file are missing.
    """
    model_map = {
        "turbo": ["Omni-TurboV1.5-W2xEX", "Universal-FastV2-W2xEX"],
        "real": ["Universal-FastV2-W2xEX", "Omni-TurboV1.5-W2xEX"],
        "anime": ["realesr-animevideov3-x2", "Omni-TurboV1.5-W2xEX"],
    }
    preferred = list(model_map.get(effective_model, model_map["turbo"]))
    fast_on = os.environ.get("PANDA_FAST_FREE_ANIME", "1").strip().lower() not in ("0", "false", "off", "no")
    mini = "AnimeVideo-MiniV1.8-W2xEX"
    if (fast_on and not is_paid_tier and (requested_model or "auto") == "auto"
            and ai_category == "anime" and effective_model == "anime"
            and (model_dir / f"{mini}.bin").is_file()
            and (model_dir / f"{mini}.param").is_file()):
        preferred.insert(0, mini)
    return preferred


def _plan_ai_output(orig_long: int, target_long_edge: int, is_paid_tier: bool) -> tuple[int, int, int]:
    """Решает, до какого размера доводить кадр и каким коэффициентом.

    Почему не «всегда 2x», как было: нейропроход был жёстко `-s 2`, а `target_long_edge` работал
    только как потолок. Видео 368x368 выходило 736x736 при тарифе 1080p — вдвое меньше, чем даёт
    конкурент на том же исходнике, и это не лечилось настройками.

    Правило: цель = min(потолок тарифа, max(2 * исходник, VIDEO_AI_MIN_EDGE)), коэффициент —
    наименьший целый 2/3/4, который её достигает. Замеры на RTX 5050: на входе 368 коэффициент
    3 и 4 стоят столько же, сколько 2 (~2.6 с на 120 кадров), поэтому более крупный кадр почти
    бесплатен; дорожает только финальная сборка, её и ограничивает потолок тарифа.
    """
    orig_long = max(2, int(orig_long))
    cap = int(target_long_edge)
    min_edge = int(VIDEO_AI_MIN_EDGE) if is_paid_tier else min(int(VIDEO_AI_MIN_EDGE), 1280)
    want = min(cap, max(orig_long * 2, min_edge))

    scale = 4
    for cand in (2, 3, 4):
        if orig_long * cand >= want * 0.98:
            scale = cand
            break
    out_edge = orig_long * scale
    if out_edge > cap * 1.05:
        # Не раздуваем сверх тарифа: готовим вход так, чтобы 2x попал ровно в потолок.
        scale = 2
        out_edge = min(cap, orig_long * 2)
    pre_edge = max(2, int(round(out_edge / float(scale))))
    return scale, out_edge, pre_edge


def _model_for_scale(preferred: list, scale: int, model_dir: Path) -> tuple:
    """Подбирает модель под нужный коэффициент. Возвращает (имя, фактический коэффициент).

    Если модели на 3x/4x нет — честно отдаём 2x, а не молча обещаем больше.
    """
    if int(scale) <= 2:
        return preferred[0], 2
    for name in preferred:
        cand = name.replace("-x2", f"-x{int(scale)}")
        if cand != name and (model_dir / f"{cand}.bin").is_file() and (model_dir / f"{cand}.param").is_file():
            return cand, int(scale)
    return preferred[0], 2


def _realesrgan_threads_for_model(model: str) -> str:
    """Use the measured no-pixel-change speed profile only for the tested Turbo model."""
    if model == "Omni-TurboV1.5-W2xEX":
        return REALESRGAN_TURBO_THREADS
    return GPU_THREADS


def _rife_threads_for_edge(target_long_edge: int) -> str:
    """
    RIFE runs full-frame optical flow without tiling.
    -j 4:6:6 allocates 6 full-frame pipelines (~9+ GB on 2K) -> vkAllocateMemory failed -2 -> 3221225477.
    1:2:2 is benchmarked fastest and 100% VRAM-safe on 8 GB RTX 5050 up to 2560px.
    """
    if int(target_long_edge) > 2560:
        return "1:1:2"
    return RIFE_THREADS or "1:2:2"


def _has_vulkan_fatal_error(stderr_text: str, returncode: int) -> bool:
    if int(returncode) in _ACCESS_VIOLATION_CODES:
        return True
    low = (stderr_text or "").lower()
    return any(tok in low for tok in _VULKAN_FATAL_MARKERS)


def _rebuild_conservative_vulkan_cmd(
    cmd: list[str],
    is_rife: bool = False,
    target_long_edge: int = 1920,
) -> list[str]:
    """Build a minimal-VRAM recovery command on the refreshed discrete GPU."""
    safe_gpu = str(resolve_vulkan_gpu_id(force_refresh=True))
    out: list[str] = []
    i = 0
    saw_g = False
    saw_j = False
    saw_t = False
    while i < len(cmd):
        arg = cmd[i]
        if arg == "-g" and i + 1 < len(cmd):
            out.extend(["-g", safe_gpu])
            saw_g = True
            i += 2
            continue
        if arg == "-j" and i + 1 < len(cmd):
            out.extend(["-j", "1:1:1"])
            saw_j = True
            i += 2
            continue
        if arg == "-t" and i + 1 < len(cmd):
            out.extend(["-t", "200"])
            saw_t = True
            i += 2
            continue
        out.append(arg)
        i += 1
    if not saw_g:
        out.extend(["-g", safe_gpu])
    if not saw_j:
        out.extend(["-j", "1:1:1"])
    if is_rife:
        if target_long_edge >= 1920 and "-u" not in out:
            out.append("-u")
    else:
        if not saw_t:
            out.extend(["-t", "200"])
    return out


def _ffmpeg_bin() -> str:
    if FFMPEG_EXE.is_file():
        return str(FFMPEG_EXE)
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe_bin() -> str:
    if FFPROBE_EXE.is_file():
        return str(FFPROBE_EXE)
    return shutil.which("ffprobe") or "ffprobe"


def _progress_bar(pct: int, width: int = 10) -> str:
    pct = max(0, min(100, pct))
    filled = int(round(width * pct / 100.0))
    return "▰" * filled + "▱" * (width - filled)


_PROBE_CACHE: Dict[str, tuple] = {}


def _probe_cache_key(video_path: Path) -> str:
    """Ключ кэша метаданных: путь + размер + время правки.

    Почему: на одну задачу `probe_video` вызывается 2–3 раза (вход, результат, проверки),
    каждый вызов — это отдельный ffprobe на Windows, ~1.2 с. Файл за время задачи не меняется,
    поэтому повторный запуск процесса бессмысленен.
    """
    try:
        st = video_path.stat()
        return f"{video_path}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return ""


def probe_video(video_path: Path) -> Dict[str, Any]:
    key = _probe_cache_key(video_path)
    if key:
        cached = _PROBE_CACHE.get(key)
        if cached is not None:
            return dict(cached)
    cmd = [
        _ffprobe_bin(),
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    res = _run_retry(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=SUBPROCESS_FLAGS,
    )
    info = {
        "width": 640,
        "height": 480,
        "fps": 30.0,
        "duration": 5.0,
        "has_audio": False,
    }
    if res.returncode != 0 or not res.stdout.strip():
        return info

    try:
        data = json.loads(res.stdout)
    except Exception:
        return info

    fmt = data.get("format") or {}
    if fmt.get("duration"):
        try:
            info["duration"] = float(fmt["duration"])
        except Exception:
            pass

    for st in data.get("streams") or []:
        ctype = st.get("codec_type")
        if ctype == "video" and info["width"] == 640 and st.get("width"):
            w = int(st.get("width", 640))
            h = int(st.get("height", 480))
            rot = 0
            tags = st.get("tags") or {}
            if "rotate" in tags:
                try:
                    rot = abs(int(tags["rotate"]))
                except Exception:
                    pass
            for sd in st.get("side_data_list") or []:
                if "rotation" in sd:
                    try:
                        rot = abs(int(float(sd["rotation"])))
                    except Exception:
                        pass
            if rot in (90, 270):
                w, h = h, w
            info["width"] = w
            info["height"] = h

            r_fps = st.get("avg_frame_rate") or st.get("r_frame_rate") or "30/1"
            if "/" in str(r_fps):
                num, den = str(r_fps).split("/", 1)
                try:
                    fps_val = float(num) / max(1.0, float(den))
                    if 1.0 <= fps_val <= 240.0:
                        info["fps"] = round(fps_val, 3)
                except Exception:
                    pass
        elif ctype == "audio":
            info["has_audio"] = True

    if key:
        _PROBE_CACHE[key] = dict(info)
    return info


def _kill_and_wait(proc: "subprocess.Popen", timeout: float = 15.0) -> None:
    """Kill a child tool and wait until Windows really released its handles."""
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=timeout)
    except Exception:
        pass


class RenderStalled(RuntimeError):
    """The GPU tool stopped producing frames for too long (hung driver / dead child)."""

def _open_retry(path: Path, mode: str = "rb", attempts: int = 12, delay: float = 0.4, **kwargs):
    """open() that waits out a momentary Windows lock on a freshly written file."""
    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return open(path, mode, **kwargs)
        except PermissionError as exc:
            last = exc
            time.sleep(delay * min(attempt + 1, 4))
    raise last  # type: ignore[misc]


def _mkdir_retry(path: Path) -> None:
    """mkdir that ignores a momentary lock on a Windows directory."""
    last: Optional[BaseException] = None
    for attempt in range(6):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except (PermissionError, BlockingIOError) as exc:
            last = exc
            time.sleep(0.25 * (attempt + 1))
    raise last  # type: ignore[misc]


def _unlink_retry(path: Path) -> None:
    """Best-effort delete: a locked temp file must never fail a finished order."""
    last: Optional[BaseException] = None
    for attempt in range(6):
        try:
            path.unlink(missing_ok=True)
            return
        except (PermissionError, BlockingIOError) as exc:
            last = exc
            time.sleep(0.25 * (attempt + 1))
    if last is not None:
        log.debug("Temp file stays on disk: %s (%s)", path, type(last).__name__)


def _run_retry(cmd: list, **kwargs: Any) -> Any:
    """subprocess.run that survives a transient Windows sharing violation.

    Antivirus can briefly hold a freshly written exe / media file, and the launch
    fails with PermissionError [WinError 5] even though the tool itself is fine.
    A short retry turns that into a normal run instead of a failed order.
    """
    last: Optional[BaseException] = None
    for attempt in range(5):
        try:
            return subprocess.run(cmd, **kwargs)
        except (PermissionError, BlockingIOError) as exc:
            last = exc
            time.sleep(0.4 * (attempt + 1))
    raise last  # type: ignore[misc]


def _popen_retry(cmd: list, **kwargs: Any) -> "subprocess.Popen":
    """Same protection for long-running GPU tools that take minutes per batch."""
    last: Optional[BaseException] = None
    for attempt in range(5):
        try:
            return subprocess.Popen(cmd, **kwargs)
        except (PermissionError, BlockingIOError) as exc:
            last = exc
            time.sleep(0.4 * (attempt + 1))
    raise last  # type: ignore[misc]




def _run_single_monitored_pass(
    cmd: list[str],
    out_dir: Path,
    expected_total: int,
    stage_title: str,
    pct_start: int,
    pct_span: int,
    on_progress: Optional[Callable[[str], None]],
    timeout_sec: int,
    stall_sec: int,
    extra_tail_sec: float = 0.0,
) -> tuple[int, str]:
    err_log_path = out_dir.parent / f"gpu_{out_dir.name}.log"
    err_fh = _open_retry(err_log_path, "w", encoding="utf-8", errors="replace")
    proc = _popen_retry(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=err_fh,
        creationflags=SUBPROCESS_FLAGS,
    )
    t_start = time.monotonic()
    last_emit = 0.0
    last_count = 0.0
    last_done = 0
    last_change = t_start

    # Warmup filter: Vulkan shader compilation & weight upload on the first few frames
    # artificially inflates per-frame time (e.g. 4/599 frames showing 20 min ETA).
    warmup_target = min(24, max(6, int(expected_total * 0.04)))
    t_warm: Optional[float] = None
    done_warm: int = 0
    ema_rate: float = 0.0
    prev_rate_t: float = 0.0
    prev_rate_done: int = 0

    try:
        while True:
            ret = proc.poll()
            now = time.monotonic()
            if now - last_count >= 1.0:
                last_count = now
                try:
                    done_cnt = sum(
                        1 for name in os.listdir(out_dir) if name.endswith((".jpg", ".png"))
                    )
                except Exception:
                    done_cnt = 0
                if done_cnt > last_done:
                    last_done = done_cnt
                    last_change = now
                    if t_warm is None and (
                        done_cnt >= warmup_target or (done_cnt >= 3 and (now - t_start) >= 10.0)
                    ):
                        t_warm = now
                        done_warm = done_cnt
                        prev_rate_t = now
                        prev_rate_done = done_cnt
                    elif t_warm is not None and (now - prev_rate_t) >= 1.5 and done_cnt > prev_rate_done:
                        inst_rate = (done_cnt - prev_rate_done) / max(0.001, now - prev_rate_t)
                        ema_rate = inst_rate if ema_rate <= 0 else (0.35 * inst_rate + 0.65 * ema_rate)
                        prev_rate_t = now
                        prev_rate_done = done_cnt

            if on_progress and (now - last_emit >= 2.2):
                last_emit = now
                done_cnt = last_done
                frac = min(1.0, done_cnt / float(max(1, expected_total)))
                overall_pct = pct_start + int(round(pct_span * frac))
                bar = _progress_bar(overall_pct)
                eta_txt = ""
                if expected_total > done_cnt:
                    eff_rate = 0.0
                    if t_warm is not None and (now - t_warm) >= 1.5 and done_cnt > done_warm:
                        post_warm_rate = (done_cnt - done_warm) / max(0.001, now - t_warm)
                        eff_rate = (0.5 * ema_rate + 0.5 * post_warm_rate) if ema_rate > 0 else post_warm_rate
                    if eff_rate > 0:
                        eta_sec = max(1, int((expected_total - done_cnt) / eff_rate + max(0.0, extra_tail_sec)))
                        eta_txt = f" · ~{eta_sec // 60}м {eta_sec % 60:02d}с" if eta_sec >= 60 else f" · ~{eta_sec}с"
                on_progress(
                    f"{stage_title}\n"
                    f"{bar} <b>{overall_pct}%</b>  ({done_cnt}/{expected_total}{eta_txt})"
                )
            if ret is not None:
                err_fh.flush()
                err_fh.close()
                err_text = ""
                try:
                    err_text = err_log_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
                return int(ret), err_text
            if now - last_change > stall_sec and last_done < expected_total:
                _kill_and_wait(proc)
                raise RenderStalled(
                    f"GPU остановился на {last_done}/{expected_total} кадрах"
                )
            if now - t_start > timeout_sec:
                _kill_and_wait(proc)
                raise TimeoutError("Превышено максимальное время рендера видео.")
            time.sleep(0.35)
    finally:
        if not err_fh.closed:
            try:
                err_fh.close()
            except OSError:
                pass
        _kill_and_wait(proc)
        try:
            _unlink_retry(err_log_path)
        except OSError:
            pass


def _run_monitored_batch(
    cmd: list[str],
    out_dir: Path,
    expected_total: int,
    stage_title: str,
    pct_start: int,
    pct_span: int,
    on_progress: Optional[Callable[[str], None]],
    timeout_sec: Optional[int] = None,
    stall_sec: int = 600,
    is_rife: bool = False,
    target_long_edge: int = 1920,
    extra_tail_sec: float = 0.0,
) -> None:
    """Run one neural CLI batch with warmup-filtered ETA, stall watchdog, and Vulkan OOM recovery."""
    if timeout_sec is None:
        timeout_sec = max(1800, int(expected_total * 8) + 300)

    ret, err_text = _run_single_monitored_pass(
        cmd=cmd,
        out_dir=out_dir,
        expected_total=expected_total,
        stage_title=stage_title,
        pct_start=pct_start,
        pct_span=pct_span,
        on_progress=on_progress,
        timeout_sec=timeout_sec,
        stall_sec=stall_sec,
        extra_tail_sec=extra_tail_sec,
    )
    vulkan_fatal = _has_vulkan_fatal_error(err_text, ret)
    if ret == 0 and not vulkan_fatal:
        return

    # Bounded 1-pass recovery on Vulkan OOM / 0xC0000005 (3221225477) / Device Lost:
    # clear any corrupted partial output, refresh discrete NVIDIA GPU index, and run with -j 1:1:1.
    if vulkan_fatal:
        safe_cmd = _rebuild_conservative_vulkan_cmd(
            cmd, is_rife=is_rife, target_long_edge=target_long_edge
        )
        log.warning(
            "Vulkan memory/device fault (rc=%s); retrying once with conservative profile cmd=%s",
            ret,
            safe_cmd,
        )
        shutil.rmtree(out_dir, ignore_errors=True)
        _mkdir_retry(out_dir)
        ret2, err_text2 = _run_single_monitored_pass(
            cmd=safe_cmd,
            out_dir=out_dir,
            expected_total=expected_total,
            stage_title=stage_title,
            pct_start=pct_start,
            pct_span=pct_span,
            on_progress=on_progress,
            timeout_sec=timeout_sec,
            stall_sec=stall_sec,
            extra_tail_sec=extra_tail_sec,
        )
        if ret2 == 0 and not _has_vulkan_fatal_error(err_text2, ret2):
            log.info("Conservative Vulkan recovery succeeded on %s", out_dir.name)
            return
        raise VulkanOutOfMemoryError(
            "Недостаточно видеопамяти GPU (VRAM) для этого кадра даже в щадящем режиме."
        )

    err_tail = (err_text or "")[-400:]
    raise RuntimeError(f"GPU render exited with code {ret}: {err_tail}")


def _count_frames(frames_dir: Path) -> int:
    try:
        return sum(1 for name in os.listdir(frames_dir) if name.endswith((".jpg", ".png")))
    except OSError:
        return 0


def _frame_luma_mean(path: Path) -> float:
    """Read a cheap, resolution-independent luma sample from one generated frame."""
    with Image.open(path) as frame:
        gray = frame.convert("L")
        gray.thumbnail((48, 48), Image.Resampling.BILINEAR)
        return float(ImageStat.Stat(gray).mean[0])


def _validate_rife_output(
    source_dir: Path,
    rife_dir: Path,
    expected_total: int,
    sample_pairs: int = 12,
) -> Dict[str, Any]:
    """Reject incomplete RIFE output and severe isolated dark/bright midpoint flashes.

    It checks evenly distributed source pairs only, skips dark source scenes and hard
    cuts, and compares each generated midpoint with its two neighboring output frames.
    This is intentionally a conservative last-resort guard, not a visual-quality score.
    """
    source_files = sorted(
        p for p in source_dir.iterdir() if p.suffix.lower() in (".jpg", ".png")
    ) if source_dir.is_dir() else []
    rife_files = sorted(
        p for p in rife_dir.iterdir() if p.suffix.lower() in (".jpg", ".png")
    ) if rife_dir.is_dir() else []
    result: Dict[str, Any] = {
        "ok": len(rife_files) == expected_total and len(source_files) >= 2,
        "source_frames": len(source_files),
        "rife_frames": len(rife_files),
        "expected_frames": expected_total,
        "checked_pairs": 0,
        "dark_midpoints": 0,
        "severe_luma_outliers": 0,
    }
    if not result["ok"]:
        return result

    last_start = len(source_files) - 2
    n_samples = max(1, min(int(sample_pairs), last_start + 1))
    if n_samples == 1:
        starts = [last_start // 2]
    else:
        starts = sorted(
            {round(i * last_start / float(n_samples - 1)) for i in range(n_samples)}
        )

    for idx in starts:
        src_a = _frame_luma_mean(source_files[idx])
        src_b = _frame_luma_mean(source_files[idx + 1])
        # Ignore near-black input scenes and scene cuts; they are not meaningful
        # references for temporal interpolation checks.
        if min(src_a, src_b) < 22.0 or abs(src_b - src_a) > 24.0:
            continue

        left = _frame_luma_mean(rife_files[2 * idx])
        middle = _frame_luma_mean(rife_files[2 * idx + 1])
        right = _frame_luma_mean(rife_files[2 * idx + 2])
        residual = abs(middle - ((left + right) / 2.0))
        result["checked_pairs"] += 1
        if middle < 18.0 and min(left, right) > 36.0:
            result["dark_midpoints"] += 1
        elif residual >= 55.0:
            result["severe_luma_outliers"] += 1

    # An all-dark clip can yield zero eligible samples; frame-count integrity still
    # passes, but any sampled catastrophic midpoint mismatch blocks delivery.
    result["ok"] = (
        result["rife_frames"] == expected_total
        and result["dark_midpoints"] == 0
        and result["severe_luma_outliers"] == 0
    )
    return result


def _encode_frames_to_mp4(
    frames_dir: Path,
    src_video_for_audio: Optional[Path],
    out_mp4: Path,
    fps: float,
    max_duration: float,
    vf_filter: str,
    max_kbps: int = 3500,
    smart_hevc: bool = True,
) -> str:
    ext = "jpg" if any(frames_dir.glob("*.jpg")) else "png"
    start_num = 0 if (frames_dir / f"00000000.{ext}").exists() else 1
    pattern = str(frames_dir / f"%08d.{ext}")

    # Build codec attempts:
    # 1) Hardware HEVC (H.265 hvc1) + Spatial/Temporal AQ -> ~45-55% smaller file at ZERO quality loss!
    # 2) Hardware H.264 + Spatial/Temporal AQ
    # 3) CPU libx264 fallback
    attempts = []
    if smart_hevc:
        hevc_kbps = max(900, int(round(max_kbps * 0.65)))
        attempts.append(
            (
                "HEVC Smart-AQ",
                [
                    "-c:v", "hevc_nvenc",
                    "-preset", "p4",
                    "-tune", "hq",
                    "-rc", "vbr",
                    "-cq", "26",
                    "-b:v", "0",
                    "-maxrate", f"{hevc_kbps}k",
                    "-bufsize", f"{hevc_kbps * 2}k",
                    "-spatial-aq", "1",
                    "-temporal-aq", "1",
                    "-rc-lookahead", "8",
                    "-tag:v", "hvc1",
                ],
            )
        )
    attempts.append(
        (
            "H.264 Smart-AQ",
            [
                "-c:v", "h264_nvenc",
                "-preset", "p4",
                "-tune", "hq",
                "-rc", "vbr",
                "-cq", "25",
                "-b:v", "0",
                "-maxrate", f"{max_kbps}k",
                "-bufsize", f"{max_kbps * 2}k",
                "-spatial-aq", "1",
                "-temporal-aq", "1",
                "-rc-lookahead", "8",
            ],
        )
    )
    attempts.append(
        (
            "H.264 CPU",
            [
                "-c:v", "libx264",
                "-preset", "faster",
                "-crf", "24",
                "-maxrate", f"{max_kbps}k",
                "-bufsize", f"{max_kbps * 2}k",
            ],
        )
    )

    for codec_label, v_args in attempts:
        cmd = [
            _ffmpeg_bin(),
            "-y",
            "-threads",
            "0",
            "-framerate",
            f"{fps:.3f}",
            "-start_number",
            str(start_num),
            "-i",
            pattern,
        ]
        if src_video_for_audio is not None:
            cmd += ["-t", f"{max_duration:.2f}", "-i", str(src_video_for_audio)]
            cmd += ["-map", "0:v:0", "-map", "1:a:0?"]
        else:
            cmd += ["-map", "0:v:0"]

        cmd += ["-vf", vf_filter, *v_args]
        cmd += [
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(out_mp4),
        ]
        res = _run_retry(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(300, int(min(4 * 60 * 60, 60 + 0.35 * max(1, _count_frames(frames_dir))))),
            creationflags=SUBPROCESS_FLAGS,
        )
        if res.returncode == 0 and out_mp4.exists() and out_mp4.stat().st_size > 1024:
            return codec_label
    raise RuntimeError("Не удалось собрать итоговый MP4.")


def enhance_video_pipeline(
    src_video: Path,
    work_dir: Path,
    video_model: str = "auto",
    enable_60fps: bool = True,
    color_enhance: bool = True,
    max_seconds: int = 30,
    target_long_edge: int = PAID_VIDEO_LONG_EDGE,
    is_paid_tier: bool = True,
    smart_hevc: bool = True,
    lang: str = "ru",
    sharpness_mode: str = "auto",
    mastering_mode: str = "auto",
    on_progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    if DISPATCH_MODE == "server":
        raise RuntimeError("AI-рендер на диспетчер-сервере отключён: задание должен выполнять Panda Worker.")
    _mkdir_retry(work_dir)
    meta = probe_video(src_video)

    # AI Content Classifier: Anime / 3D Cartoon / Movie / Live Video
    ai_info = classify_video_content(src_video, meta, work_dir)
    ai_cat = ai_info["category"]
    cat_label = ai_cat_name(ai_cat, lang)

    effective_model = ai_info["video_model"] if video_model in ("auto", "") else video_model
    cinema_tone = bool(ai_info.get("cinema_tone"))

    dur = min(float(meta["duration"] or 5.0), float(max_seconds))
    trimmed = float(meta["duration"] or 0.0) > float(max_seconds) + 0.6
    orig_w, orig_h = int(meta["width"]), int(meta["height"])
    raw_fps = min(60.0, max(8.0, float(meta["fps"] or 30.0)))

    # Free tier strictly caps framerate at 30 FPS & resolution at 720p (1280px)
    if not is_paid_tier:
        enable_60fps = False
        target_long_edge = min(1280, int(target_long_edge))
        in_fps = min(30.0, raw_fps)
    else:
        in_fps = raw_fps

    frames_in = work_dir / "frames_in"
    frames_up = work_dir / "frames_up"
    _mkdir_retry(frames_in)
    _mkdir_retry(frames_up)

    ai_hdr = {
        "kk": f"🧠 ИИ түрі: <b>{cat_label}</b>",
        "ru": f"🧠 ИИ определил: <b>{cat_label}</b>",
        "en": f"🧠 AI detected: <b>{cat_label}</b>",
    }.get(lang, f"🧠 ИИ определил: <b>{cat_label}</b>")

    stage_unpack = {
        "kk": f"{ai_hdr}\n🎬 Кадрларды дайындау...",
        "ru": f"{ai_hdr}\n🎬 Распаковка кадров...",
        "en": f"{ai_hdr}\n🎬 Extracting frames...",
    }.get(lang, f"{ai_hdr}\n🎬 Распаковка кадров...")

    if on_progress:
        on_progress(f"{stage_unpack}\n▰▱▱▱▱▱▱▱▱▱ <b>8%</b>")

    # Планируем нейропроход: коэффициент 2/3/4 и точный размер на выходе (см. _plan_ai_output).
    ai_scale, ai_out_edge, pre_cap = _plan_ai_output(max(orig_w, orig_h), int(target_long_edge), is_paid_tier)
    long_edge = max(orig_w, orig_h)
    vf_extract = []
    if not is_paid_tier and raw_fps > 30.5:
        vf_extract.append("fps=30")
    if long_edge != pre_cap:
        # Раньше здесь было только уменьшение, поэтому маленький исходник никогда не доходил
        # до разрешения тарифа. Теперь приводим к плану в обе стороны (lanczos — мягче, чем bilinear).
        if orig_w >= orig_h:
            vf_extract.append(f"scale={pre_cap}:-2:flags=lanczos")
        else:
            vf_extract.append(f"scale=-2:{pre_cap}:flags=lanczos")
    else:
        vf_extract.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    # Pre-AI chroma + temporal flicker clean-up (kills night phone purple/green speckle before GPU upscale)
    if color_enhance and (mastering_mode or "auto").lower() != "raw":
        vf_extract.append("hqdn3d=1.0:2.2:1.8:2.8")

    extract_cmd = [
        _ffmpeg_bin(),
        "-y",
        "-threads",
        "0",
        "-t",
        f"{dur:.2f}",
        "-i",
        str(src_video),
        "-vf",
        ",".join(vf_extract),
        "-qscale:v",
        "3",
        str(frames_in / "%08d.jpg"),
    ]
    res = _run_retry(
        extract_cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        creationflags=SUBPROCESS_FLAGS,
    )
    frame_files = sorted(frames_in.glob("*.jpg"))
    if res.returncode != 0 or not frame_files:
        raise RuntimeError("Не удалось прочитать кадры видео. Проверь формат файла.")

    # Fast 3ms optical diagnostic on middle frame for Smart Auto Sharpness & HDR Mastering
    v_diag: Dict[str, Any] = {}
    try:
        from PIL import Image as _VImage
        from photo_engine import analyze_photo as _analyze_vframe

        mid_fp = frame_files[len(frame_files) // 2]
        with _VImage.open(mid_fp) as _vim:
            v_diag = _analyze_vframe(_vim)
    except Exception:
        v_diag = {}

    n_frames = len(frame_files)
    rife_dir = RIFE_ANIME_DIR if ai_info.get("anime_rife") else RIFE_MODEL_DIR
    # Бюджет удвоения кадров: на длинных клипах RIFE почти удваивает время задачи (в логах были
    # 394–421 с против 116 с без него), а выигрыш по плавности на длинном видео не заметен.
    rife_within_budget = (n_frames * 2) <= int(RIFE_MAX_OUT_FRAMES)
    will_run_rife = bool(
        is_paid_tier and enable_60fps and in_fps < 47.0 and RIFE_EXE.is_file() and rife_dir.is_dir()
        and rife_within_budget
    )
    if is_paid_tier and enable_60fps and in_fps < 47.0 and not rife_within_budget:
        log.info(
            "RIFE skipped: %s кадров сверх бюджета %s — отдаём исходные %.1f fps честно",
            n_frames * 2, RIFE_MAX_OUT_FRAMES, in_fps,
        )

    stage_up = {
        "kk": f"{ai_hdr}\n🚀 Әр кадрды ИИ арқылы жақсарту",
        "ru": f"{ai_hdr}\n🚀 ИИ-улучшение чёткости каждого кадра",
        "en": f"{ai_hdr}\n🚀 AI frame-by-frame clarity enhancement",
    }.get(lang, f"{ai_hdr}\n🚀 ИИ-улучшение чёткости каждого кадра")

    if REALESRGAN_EXE.is_file():
        preferred = _video_model_candidates(effective_model, video_model, ai_cat,
                                            is_paid_tier, REALESRGAN_MODELS)
        chosen_model = preferred[0]
        for m in preferred:
            if (REALESRGAN_MODELS / f"{m}.bin").exists():
                chosen_model = m
                break
        # Коэффициент 3/4 — если модель под него установлена; иначе падаем на 2x и пересчитываем план.
        scaled_model, actual_scale = _model_for_scale([chosen_model] + [m for m in preferred if m != chosen_model],
                                                     ai_scale, REALESRGAN_MODELS)
        if actual_scale != ai_scale:
            log.warning("VIDEO_AI_SCALE plan=%sx недоступен (модели нет) — работаю на %sx", ai_scale, actual_scale)
            ai_scale = actual_scale
            ai_out_edge = max(orig_w, orig_h) * actual_scale
        chosen_model = scaled_model

        active_gpu_id = resolve_vulkan_gpu_id()
        # Estimate tail time (RIFE + mux) so ESRGAN stage does not under-promise end-to-end ETA
        est_tail = (max(4.0, n_frames * 0.14) if will_run_rife else 0.0) + max(2.0, n_frames * 0.02)
        up_cmd = [
            str(REALESRGAN_EXE),
            "-i",
            str(frames_in),
            "-o",
            str(frames_up),
            "-m",
            str(REALESRGAN_MODELS),
            "-n",
            chosen_model,
            "-s",
            str(int(ai_scale)),
            "-g",
            str(active_gpu_id),
            "-j",
            _realesrgan_threads_for_model(chosen_model),
            "-f",
            "jpg",
        ]
        _run_monitored_batch(
            up_cmd,
            frames_up,
            expected_total=n_frames,
            stage_title=stage_up,
            pct_start=12,
            pct_span=48 if will_run_rife else 76,
            on_progress=on_progress,
            is_rife=False,
            target_long_edge=int(target_long_edge),
            extra_tail_sec=est_tail,
        )
    else:
        for f in frame_files:
            shutil.copy2(f, frames_up / f.name)

    shutil.rmtree(frames_in, ignore_errors=True)
    active_frames_dir = frames_up
    out_fps = in_fps

    if will_run_rife:
        stage_rife = {
            "kk": f"{ai_hdr}\n⚡ Тегіс 60 FPS генерациясы",
            "ru": f"{ai_hdr}\n⚡ Генерация плавных 60 FPS",
            "en": f"{ai_hdr}\n⚡ Generating smooth 60 FPS",
        }.get(lang, f"{ai_hdr}\n⚡ Генерация плавных 60 FPS")

        frames_rife = work_dir / "frames_rife"
        _mkdir_retry(frames_rife)
        active_rife_gpu = resolve_vulkan_gpu_id()
        rife_cmd = [
            str(RIFE_EXE),
            "-i",
            str(frames_up),
            "-o",
            str(frames_rife),
            "-m",
            str(rife_dir),
            "-g",
            str(active_rife_gpu),
            "-j",
            _rife_threads_for_edge(int(target_long_edge)),
            "-f",
            "%08d.jpg",
        ]
        if int(target_long_edge) > 2560:
            rife_cmd.append("-u")
        _run_monitored_batch(
            rife_cmd,
            frames_rife,
            expected_total=n_frames * 2,
            stage_title=stage_rife,
            pct_start=60,
            pct_span=28,
            on_progress=on_progress,
            is_rife=True,
            target_long_edge=int(target_long_edge),
            extra_tail_sec=max(2.0, n_frames * 0.02),
        )
        rife_qc = _validate_rife_output(frames_up, frames_rife, n_frames * 2)
        log.info("RIFE_TEMPORAL_QC model=%s result=%s", rife_dir.name, rife_qc)

        if not rife_qc["ok"]:
            # A known-good general model is the single bounded retry. Never encode or
            # send a sequence that still fails the temporal sanity check.
            fallback_model = RIFE_EXE.parent / "rife-v4.26"
            if not fallback_model.is_dir() or fallback_model.resolve() == rife_dir.resolve():
                shutil.rmtree(frames_up, ignore_errors=True)
                shutil.rmtree(frames_rife, ignore_errors=True)
                raise RuntimeError(
                    "RIFE output failed temporal validation; refusing to deliver a flickering video."
                )

            log.warning(
                "RIFE_TEMPORAL_QC retrying once with fallback model=%s", fallback_model.name
            )
            frames_rife_fallback = work_dir / "frames_rife_fallback"
            shutil.rmtree(frames_rife_fallback, ignore_errors=True)
            _mkdir_retry(frames_rife_fallback)
            fallback_cmd = list(rife_cmd)
            fallback_cmd[fallback_cmd.index("-m") + 1] = str(fallback_model)
            fallback_cmd[fallback_cmd.index("-o") + 1] = str(frames_rife_fallback)
            stage_rife_retry = {
                "kk": f"{ai_hdr}\n🔁 Қосымша қауіпсіз модельмен қайталау",
                "ru": f"{ai_hdr}\n🔁 Повтор с резервной безопасной моделью",
                "en": f"{ai_hdr}\n🔁 Retrying with the safe fallback model",
            }.get(lang, f"{ai_hdr}\n🔁 Повтор с резервной безопасной моделью")
            _run_monitored_batch(
                fallback_cmd,
                frames_rife_fallback,
                expected_total=n_frames * 2,
                stage_title=stage_rife_retry,
                pct_start=88,
                pct_span=4,
                on_progress=on_progress,
                is_rife=True,
                target_long_edge=int(target_long_edge),
            )
            fallback_qc = _validate_rife_output(
                frames_up, frames_rife_fallback, n_frames * 2
            )
            log.info(
                "RIFE_TEMPORAL_QC model=%s result=%s",
                fallback_model.name,
                fallback_qc,
            )
            if not fallback_qc["ok"]:
                shutil.rmtree(frames_up, ignore_errors=True)
                shutil.rmtree(frames_rife, ignore_errors=True)
                shutil.rmtree(frames_rife_fallback, ignore_errors=True)
                raise RuntimeError(
                    "RIFE fallback failed temporal validation; refusing to deliver a flickering video."
                )
            shutil.rmtree(frames_rife, ignore_errors=True)
            frames_rife = frames_rife_fallback
            rife_dir = fallback_model

        shutil.rmtree(frames_up, ignore_errors=True)
        active_frames_dir = frames_rife
        out_fps = round(in_fps * 2.0, 3)

    stage_mux = {
        "kk": f"{ai_hdr}\n💎 Финалдық жинау және дыбыс...",
        "ru": f"{ai_hdr}\n💎 Финальная сборка видео и звука...",
        "en": f"{ai_hdr}\n💎 Mastering final MP4 & audio...",
    }.get(lang, f"{ai_hdr}\n💎 Финальная сборка видео и звука...")

    if on_progress:
        on_progress(f"{stage_mux}\n▰▰▰▰▰▰▰▰▰▱ <b>93%</b>")

    vf_master = []
    # Enforce hard cap on output resolution (e.g. <= 1280px 720p on Free, <= 1920px 1080p on VIP, <= 2560px 2K on PRO)
    vf_master.append(
        f"scale='if(gt(max(iw,ih),{int(target_long_edge)}),if(gte(iw,ih),{int(target_long_edge)},-2),trunc(iw/2)*2)':"
        f"'if(gt(max(iw,ih),{int(target_long_edge)}),if(gte(iw,ih),-2,{int(target_long_edge)}),trunc(ih/2)*2)':flags=lanczos"
    )

    s_mode = (sharpness_mode or "auto").lower()
    if s_mode == "vivid":
        s_mode = "hd"
    elif s_mode == "balanced":
        s_mode = "soft"
    v_edge = float(v_diag.get("edge_score", 20.0))

    if s_mode == "auto":
        if ai_cat in ("anime", "cartoon3d") or v_edge > 27.0:
            vf_master.append("cas=strength=0.28")
        elif v_edge < 15.5:
            vf_master.append("cas=strength=0.62")
        else:
            vf_master.append("cas=strength=0.46")
    elif s_mode == "soft":
        vf_master.append("cas=strength=0.18")
    elif s_mode == "hd":
        vf_master.append("cas=strength=0.50")
    elif s_mode == "ultra":
        vf_master.append("cas=strength=0.76,unsharp=3:3:0.32:3:3:0.0")

    eff_vmaster = "raw" if (not color_enhance or bool(v_diag.get("is_bw"))) else (mastering_mode or "auto").lower()
    if eff_vmaster == "hdr":
        vf_master.append("eq=gamma=1.12:contrast=1.05:saturation=1.11")
    elif eff_vmaster == "cinema":
        vf_master.append("eq=brightness=-0.012:contrast=1.08:gamma=0.97:saturation=1.04")
    elif eff_vmaster == "auto":
        v_luma = float(v_diag.get("mean_luma", 122.0))
        v_bfloor = int(v_diag.get("black_floor", 8))
        v_cb_shift = float(v_diag.get("cb_shift", 0.0))
        g_val = 1.13 if v_luma < 96.0 else (1.06 if v_luma < 112.0 else 1.00)
        br_val = -0.015 if v_bfloor > 20 else 0.000
        ct_val = 1.07 if v_bfloor > 20 else (1.02 if cinema_tone else 1.04)
        sat_val = 1.03 if cinema_tone else 1.08
        vf_master.append(
            f"eq=brightness={br_val:.3f}:contrast={ct_val:.2f}:gamma={g_val:.2f}:saturation={sat_val:.2f}"
        )
        if v_cb_shift > 1.2:
            vf_master.append("colorbalance=bm=0.03:rm=-0.025")

    out_mp4 = work_dir / ("Panda_HD_Enhanced.mp4" if is_paid_tier else "Panda_720p.mp4")
    max_kbps = 1900 if not is_paid_tier else (4800 if target_long_edge > 1920 else 3500)

    codec_used = _encode_frames_to_mp4(
        active_frames_dir,
        src_video if meta["has_audio"] else None,
        out_mp4,
        fps=out_fps,
        max_duration=dur,
        vf_filter=",".join(vf_master),
        max_kbps=max_kbps,
        smart_hevc=smart_hevc,
    )

    shutil.rmtree(active_frames_dir, ignore_errors=True)
    out_meta = probe_video(out_mp4)
    elapsed = round(time.perf_counter() - t0, 2)

    return {
        "out_path": out_mp4,
        "orig_size": (orig_w, orig_h),
        "final_size": (out_meta["width"], out_meta["height"]),
        "ai_scale": int(ai_scale),
        "ai_out_edge": int(ai_out_edge),
        "orig_fps": round(raw_fps, 1),
        "final_fps": round(out_fps, 1),
        "rife_applied": will_run_rife,
        "rife_model": rife_dir.name if will_run_rife else None,
        "trimmed": trimmed,
        "max_sec_used": max_seconds,
        "duration": round(dur, 1),
        "elapsed_sec": elapsed,
        "ai_category": ai_cat,
        "is_paid_tier": is_paid_tier,
        "codec_used": codec_used,
    }


def make_vertical_9_16(src_video: Path, work_dir: Path) -> Path:
    _mkdir_retry(work_dir)
    out_mp4 = work_dir / "Panda_Reels_9x16.mp4"
    fc = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=26:4,eq=brightness=-0.08:saturation=1.1[bg2];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease,cas=strength=0.38[fg2];"
        "[bg2][fg2]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )
    for codec_args in (
        ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22", "-maxrate", "6000k", "-bufsize", "12M"],
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "6000k", "-bufsize", "12M"],
    ):
        cmd = [
            _ffmpeg_bin(),
            "-y",
            "-threads",
            "0",
            "-t",
            str(MAX_VIDEO_SECONDS_VIP),
            "-i",
            str(src_video),
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "0:a:0?",
            *codec_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(out_mp4),
        ]
        res = _run_retry(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            creationflags=SUBPROCESS_FLAGS,
        )
        if res.returncode == 0 and out_mp4.exists() and out_mp4.stat().st_size > 1024:
            return out_mp4
    raise RuntimeError("Не удалось создать вертикальную версию 9:16.")


def make_hd_gif(src_video: Path, work_dir: Path, max_sec: float = 10.0) -> Path:
    _mkdir_retry(work_dir)
    out_gif = work_dir / "Panda_HD.gif"
    fc = (
        "fps=15,scale=540:-1:flags=lanczos,cas=strength=0.35,"
        "split[s0][s1];[s0]palettegen=stats_mode=diff[p];"
        "[s1][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    )
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-threads",
        "0",
        "-t",
        f"{max_sec:.1f}",
        "-i",
        str(src_video),
        "-lavfi",
        fc,
        str(out_gif),
    ]
    res = _run_retry(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        creationflags=SUBPROCESS_FLAGS,
    )
    if res.returncode != 0 or not out_gif.exists():
        raise RuntimeError("Не удалось создать HD GIF.")
    return out_gif


def make_video_circle(src_video: Path, work_dir: Path) -> Path:
    _mkdir_retry(work_dir)
    out_mp4 = work_dir / "Panda_Circle_640.mp4"
    vf = (
        "crop='min(iw,ih)':'min(iw,ih)',"
        "scale=640:640:flags=lanczos,"
        "cas=strength=0.42,eq=contrast=1.03:saturation=1.06,"
        "format=yuv420p"
    )
    for codec_args in (
        ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22", "-maxrate", "3500k", "-bufsize", "7M"],
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "3500k", "-bufsize", "7M"],
    ):
        cmd = [
            _ffmpeg_bin(),
            "-y",
            "-threads",
            "0",
            "-t",
            "59.5",
            "-i",
            str(src_video),
            "-vf",
            vf,
            *codec_args,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(out_mp4),
        ]
        res = _run_retry(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=SUBPROCESS_FLAGS,
        )
        if res.returncode == 0 and out_mp4.exists() and out_mp4.stat().st_size > 1024:
            return out_mp4
    raise RuntimeError("Не удалось подготовить ТГ-кружок.")


_SOCIAL_CACHE_TTL = 1800.0


def _curl_bin() -> Optional[str]:
    """Native curl is far faster than Python TLS on this PC (happy-eyeballs, no DNS stalls)."""
    candidates = [
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe",
        Path(shutil.which("curl") or ""),
    ]
    for cand in candidates:
        try:
            if str(cand) and cand.is_file():
                return str(cand)
        except OSError:
            continue
    return None


def _curl_json_post(url: str, form: Dict[str, str], timeout: float = 25.0) -> Optional[Dict[str, Any]]:
    curl = _curl_bin()
    if not curl:
        return None
    args = [curl, "-sS", "-L", "--http1.1", "--connect-timeout", "10", "--max-time", str(int(timeout))]
    for key, value in form.items():
        args += ["--data-urlencode", f"{key}={value}"]
    args.append(url)
    try:
        res = _run_retry(args, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout + 5, creationflags=SUBPROCESS_FLAGS)
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout)
    except Exception:
        return None
    return None


def _curl_download(url: str, dest: Path, timeout: float = 180.0,
                   on_progress: Optional[Callable[[int, int], None]] = None) -> bool:
    """Stream to disk with curl; returns True when a non-empty file was written."""
    curl = _curl_bin()
    if not curl:
        return False
    part = dest.with_suffix(dest.suffix + ".part")
    args = [curl, "-sS", "-L", "--http1.1", "--connect-timeout", "15", "--max-time", str(int(timeout)),
            "-A", "Mozilla/5.0", "-o", str(part)]
    if on_progress:
        args += ["--write-out", "%{size_download} %{speed_download}"]
    args.append(url)
    try:
        res = _run_retry(args, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout + 10, creationflags=SUBPROCESS_FLAGS)
        if res.returncode != 0 or not part.is_file() or part.stat().st_size < 4096:
            _unlink_retry(part)
            return False
        part.replace(dest)
        if on_progress:
            try:
                done = int(part.stat().st_size if part.exists() else dest.stat().st_size)
                on_progress(done, int(timeout))
            except Exception:
                pass
        return True
    except Exception:
        _unlink_retry(part)
        return False


def _requests_download(url: str, dest: Path, on_progress: Optional[Callable[[int, int], None]] = None) -> bool:
    try:
        with requests.get(url, stream=True, timeout=(10, 90), headers={"User-Agent": "Mozilla/5.0"}) as vr:
            vr.raise_for_status()
            total = int(vr.headers.get("Content-Length") or 0)
            done = 0
            with _open_retry(dest, "wb") as fh:
                for chunk in vr.iter_content(chunk_size=262144):
                    if chunk:
                        fh.write(chunk)
                        done += len(chunk)
                        if on_progress and done % (1024 * 1024) < len(chunk):
                            on_progress(done, total)
        return dest.is_file() and dest.stat().st_size > 4096
    except Exception:
        return False


def _social_cache_path(url: str) -> Optional[Path]:
    if ZERO_RETENTION_SERVER:
        return None
    try:
        digest = hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:16]
        cache_dir = JOBS_DIR / "cache_social"
        _mkdir_retry(cache_dir)
        return cache_dir / f"{digest}.mp4"
    except Exception:
        return None


def download_social_video(
    url: str,
    work_dir: Path,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """TikTok/Reels download: cached, curl-first, with a progress callback."""
    _mkdir_retry(work_dir)
    out_mp4 = work_dir / "panda_tiktok.mp4"
    lower_url = url.lower()

    cached = _social_cache_path(url)
    if cached and cached.is_file() and cached.stat().st_size > 4096 \
            and (time.time() - cached.stat().st_mtime) < _SOCIAL_CACHE_TTL:
        shutil.copy2(cached, out_mp4)
        return {"video_path": out_mp4, "title": "TikTok HD", "author": "", "cached": True}

    def _progress(done: int, total: int) -> None:
        if not on_progress:
            return
        try:
            mb = done / 1048576.0
            if total > 0:
                on_progress(f"🌐 <b>TikTok HD:</b> {mb:.1f} / {total / 1048576.0:.1f} МБ")
            else:
                on_progress(f"🌐 <b>TikTok HD:</b> {mb:.1f} МБ")
        except Exception:
            pass

    if "tiktok.com" in lower_url:
        payload = _curl_json_post("https://www.tikwm.com/api/", {"url": url, "hd": "1"})
        if payload is None:
            try:
                rr = requests.post("https://www.tikwm.com/api/", data={"url": url, "hd": "1"},
                                   headers={"User-Agent": "Mozilla/5.0"}, timeout=(8, 20))
                payload = rr.json()
            except Exception:
                payload = None
        if payload and payload.get("code") == 0 and payload.get("data"):
            d = payload["data"]
            video_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
            if video_url:
                if video_url.startswith("/"):
                    video_url = "https://www.tikwm.com" + video_url
                ok = _curl_download(video_url, out_mp4, on_progress=on_progress and _progress)
                if not ok:
                    ok = _requests_download(video_url, out_mp4, _progress)
                if ok and out_mp4.stat().st_size > 4096:
                    if cached:
                        try:
                            shutil.copy2(out_mp4, cached)
                        except Exception:
                            pass
                    return {
                        "video_path": out_mp4,
                        "title": (d.get("title") or "TikTok HD")[:180],
                        "author": ((d.get("author") or {}).get("nickname") or ""),
                    }

    ytdlp_candidates = [
        work_dir.parent.parent / "yt-dlp.exe",
        Path(shutil.which("yt-dlp") or ""),
    ]
    pyz = work_dir.parent.parent / "yt-dlp.pyz"
    commands: list = []
    for ybin in ytdlp_candidates:
        if str(ybin) and ybin.is_file():
            commands.append([str(ybin)])
    if pyz.is_file():
        commands.append([sys.executable.replace("pythonw.exe", "python.exe"), str(pyz)])
    for prefix in commands:
        cmd = prefix + [
            "--no-playlist",
            "--no-part",
            "--concurrent-fragments", "4",
            "-f",
            "mp4[filesize<45M]/best[ext=mp4]/best",
            "-o",
            str(out_mp4),
            url,
        ]
        try:
            res = _run_retry(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=120, creationflags=SUBPROCESS_FLAGS)
        except Exception:
            continue
        if res.returncode == 0 and out_mp4.exists() and out_mp4.stat().st_size > 4096:
            if cached:
                try:
                    shutil.copy2(out_mp4, cached)
                except Exception:
                    pass
            return {"video_path": out_mp4, "title": "Video", "author": ""}

    raise RuntimeError(
        "Не удалось скачать видео по ссылке. Проверьте, что ссылка открывается без входа в аккаунт, "
        "или пришлите файл в чат (до 20 МБ)."
    )
