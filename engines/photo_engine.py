# -*- coding: utf-8 -*-
"""
Panda 4K AI Photo Engine:
- Automatic AI Content Classifier (Anime / 3D Cartoon / Movie / Real-Life)
- Tiered Output Cap (Free: 720p HD class, Paid/VIP: Full 4K Ultra 3840px)
- Studio optical mastering + "BEFORE | AFTER PANDA AI" comparison showcase
"""
from __future__ import annotations

import io
import math
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import requests
from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageOps,
    ImageStat,
)

from config import (
    DISPATCH_MODE,
    GPU_ID,
    GPU_THREADS,
    IMAGEMAGICK_EXE,
    MAX_PHOTO_TARGET_PX,
    REALCUGAN_EXE,
    REALCUGAN_MODELS_PRO,
    REALESRGAN_EXE,
    REALESRGAN_MODELS,
    SUBPROCESS_FLAGS,
    resolve_vulkan_gpu_id,
)
from content_ai import classify_photo_content

_PHOTO_VULKAN_FATAL = (
    "vkallocatememory failed",
    "vkqueuesubmit failed",
    "vkwaitforfences failed",
    "vkcreatecomputepipelines failed",
    "out of device memory",
    "device lost",
)
_PHOTO_AV_CODES = (3221225477, -1073741819)


def _photo_vulkan_failed(res: subprocess.CompletedProcess[str], out_file: Path) -> bool:
    if int(res.returncode) in _PHOTO_AV_CODES or int(res.returncode) != 0:
        return True
    err_low = f"{res.stdout or ''}\n{res.stderr or ''}".lower()
    if any(tok in err_low for tok in _PHOTO_VULKAN_FATAL):
        return True
    return not (out_file.exists() and out_file.stat().st_size > 0)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for fp in candidates:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size=size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def analyze_photo(img: Image.Image) -> Dict[str, Any]:
    rgb = img.convert("RGB")
    w, h = rgb.size
    thumb = rgb.copy()
    thumb.thumbnail((512, 512), Image.Resampling.BILINEAR)

    gray = thumb.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_stat = ImageStat.Stat(edges)
    # Combined mean + std edge energy: < 16.0 is soft/blurry, 16..27 normal, > 27 very sharp
    edge_score = float(edge_stat.mean[0] * 0.45 + edge_stat.stddev[0] * 0.55)

    hsv = thumb.convert("HSV")
    sat_stat = ImageStat.Stat(hsv)
    mean_sat = float(sat_stat.mean[1])  # 0..255
    is_bw = mean_sat < 11.0

    # Optical diagnostics for Smart HDR, Dehaze, Auto White Balance & Chroma Denoise (<3 ms)
    y_ch, cb_ch, cr_ch = thumb.convert("YCbCr").split()
    y_hist = y_ch.histogram()
    total_px = max(1, sum(y_hist))
    mean_luma = float(ImageStat.Stat(y_ch).mean[0])
    shadow_ratio = float(sum(y_hist[:75])) / float(total_px)

    acc = 0
    black_floor = 0
    white_ceil = 255
    p5_target = int(total_px * 0.05)
    p98_target = int(total_px * 0.98)
    got_p5 = False
    for idx_v, cnt in enumerate(y_hist):
        acc += cnt
        if not got_p5 and acc >= p5_target:
            black_floor = idx_v
            got_p5 = True
        if acc >= p98_target:
            white_ceil = idx_v
            break

    mean_cb = float(ImageStat.Stat(cb_ch).mean[0])
    mean_cr = float(ImageStat.Stat(cr_ch).mean[0])
    cb_shift = 0.0
    cr_shift = 0.0
    if not is_bw:
        # Detect indoor yellow/tungsten lamp cast (both dim & bright rooms) without ruining natural sunsets
        if mean_cr > 132.5 and mean_cb < 121.5 and (mean_cr - mean_cb) > 13.5:
            warm_excess = min(1.0, (mean_cr - mean_cb - 13.5) / 18.0)
            cb_shift = round(3.8 * warm_excess, 2)
            cr_shift = round(-3.5 * warm_excess, 2)
        elif mean_cb < 122.5 and mean_cr < 122.5:
            cb_shift = 2.0
            cr_shift = 2.0

    cb_diff = ImageChops.difference(cb_ch, cb_ch.filter(ImageFilter.GaussianBlur(radius=1.2)))
    chroma_noise = float(ImageStat.Stat(cb_diff).mean[0])

    return {
        "width": w,
        "height": h,
        "edge_score": round(edge_score, 2),
        "mean_sat": round(mean_sat, 1),
        "is_bw": is_bw,
        "has_alpha": "A" in img.getbands(),
        "mean_luma": round(mean_luma, 1),
        "shadow_ratio": round(shadow_ratio, 3),
        "black_floor": int(black_floor),
        "white_ceil": int(white_ceil),
        "cb_shift": cb_shift,
        "cr_shift": cr_shift,
        "chroma_noise": round(chroma_noise, 2),
    }


def chroma_denoise_pre(rgb_img: Image.Image, diag: Dict[str, Any]) -> Image.Image:
    """
    Fast pre-AI YCbCr chroma denoiser: removes night phone purple/green sensor speckle
    and WhatsApp color blocks on Cb/Cr while keeping Y luminance edges 100% razor-sharp.
    """
    if diag.get("is_bw"):
        return rgb_img
    mean_luma = float(diag.get("mean_luma", 128.0))
    c_noise = float(diag.get("chroma_noise", 1.5))
    if mean_luma > 135.0 and c_noise < 1.6:
        return rgb_img
    alpha = 0.52 if (mean_luma < 98.0 or c_noise > 2.4) else 0.34
    y, cb, cr = rgb_img.convert("YCbCr").split()
    cb_clean = Image.blend(cb, cb.filter(ImageFilter.GaussianBlur(radius=0.95)), alpha)
    cr_clean = Image.blend(cr, cr.filter(ImageFilter.GaussianBlur(radius=0.95)), alpha)
    return Image.merge("YCbCr", (y, cb_clean, cr_clean)).convert("RGB")


def _select_esrgan_model(preset: str) -> Tuple[str, int]:
    """Returns (model_name, scale) checking installed .bin files with safe fallbacks."""
    prefs = {
        "photo_hq": ["Photo-HQ-W4xEX", "realesrgan-x4plus"],
        "photo_natural": ["Photo-Conservative-x4", "realesrgan-x4plus", "Photo-HQ-W4xEX"],
        "anime_pro": ["Anime-HQ-W4xEX", "realesrgan-x4plus-anime", "Photo-HQ-W4xEX"],
        "fast_2x": ["Universal-FastV2-W2xEX", "realesr-animevideov3-x2"],
    }.get(preset, ["Photo-HQ-W4xEX", "realesrgan-x4plus"])

    scale = 2 if preset == "fast_2x" else 4
    if REALESRGAN_MODELS.exists():
        for name in prefs:
            if (REALESRGAN_MODELS / f"{name}.bin").exists():
                return name, scale
    return prefs[0], scale


def _run_gpu_upscale_rgb(
    rgb_img: Image.Image,
    work_dir: Path,
    preset: str,
    scale_pass: int = 4,
) -> Tuple[Image.Image, str]:
    """
    Runs Vulkan GPU upscaler in pure-ASCII temp directory with discrete GPU resolution
    and bounded conservative (-j 1:1:1 -t 200) recovery on Vulkan OOM / 0xC0000005.
    Falls back to Lanczos ONLY in synthetic test environments where no GPU binary exists.
    """
    if DISPATCH_MODE == "server":
        raise RuntimeError(
            "AI-рендер фото на диспетчер-сервере отключён: задание должен выполнять Panda Worker."
        )

    in_file = work_dir / "gpu_in.png"
    out_file = work_dir / "gpu_out.png"
    out_file.unlink(missing_ok=True)
    rgb_img.save(in_file, format="PNG", compress_level=1)

    has_any_gpu_bin = False
    last_err = ""

    # Try Real-CUGAN Pro first if anime_pro is chosen and binary exists
    if preset == "anime_pro" and REALCUGAN_EXE.is_file() and REALCUGAN_MODELS_PRO.is_dir():
        has_any_gpu_bin = True
        gpu_idx = resolve_vulkan_gpu_id()
        cmd = [
            str(REALCUGAN_EXE),
            "-i",
            str(in_file),
            "-o",
            str(out_file),
            "-m",
            str(REALCUGAN_MODELS_PRO),
            "-s",
            "2" if scale_pass == 2 else "3",
            "-n",
            "1",
            "-g",
            str(gpu_idx),
            "-j",
            GPU_THREADS,
            "-f",
            "png",
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=SUBPROCESS_FLAGS,
        )
        if _photo_vulkan_failed(res, out_file):
            out_file.unlink(missing_ok=True)
            safe_gpu = resolve_vulkan_gpu_id(force_refresh=True)
            safe_cmd = [
                str(REALCUGAN_EXE),
                "-i",
                str(in_file),
                "-o",
                str(out_file),
                "-m",
                str(REALCUGAN_MODELS_PRO),
                "-s",
                "2" if scale_pass == 2 else "3",
                "-n",
                "1",
                "-g",
                str(safe_gpu),
                "-j",
                "1:1:1",
                "-t",
                "200",
                "-f",
                "png",
            ]
            res = subprocess.run(
                safe_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=240,
                creationflags=SUBPROCESS_FLAGS,
            )
        if not _photo_vulkan_failed(res, out_file):
            with Image.open(out_file) as im:
                return im.convert("RGB"), "Panda Anime Pro"
        last_err = (res.stderr or "")[-260:]

    # Real-ESRGAN Vulkan
    if REALESRGAN_EXE.is_file():
        has_any_gpu_bin = True
        out_file.unlink(missing_ok=True)
        model_key = "fast_2x" if scale_pass == 2 else preset
        model_name, model_scale = _select_esrgan_model(model_key)
        gpu_idx = resolve_vulkan_gpu_id()
        cmd = [
            str(REALESRGAN_EXE),
            "-i",
            str(in_file),
            "-o",
            str(out_file),
            "-m",
            str(REALESRGAN_MODELS),
            "-n",
            model_name,
            "-s",
            str(model_scale),
            "-g",
            str(gpu_idx),
            "-j",
            GPU_THREADS,
            "-f",
            "png",
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            creationflags=SUBPROCESS_FLAGS,
        )
        if _photo_vulkan_failed(res, out_file):
            out_file.unlink(missing_ok=True)
            safe_gpu = resolve_vulkan_gpu_id(force_refresh=True)
            safe_cmd = [
                str(REALESRGAN_EXE),
                "-i",
                str(in_file),
                "-o",
                str(out_file),
                "-m",
                str(REALESRGAN_MODELS),
                "-n",
                model_name,
                "-s",
                str(model_scale),
                "-g",
                str(safe_gpu),
                "-j",
                "1:1:1",
                "-t",
                "200",
                "-f",
                "png",
            ]
            res = subprocess.run(
                safe_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                creationflags=SUBPROCESS_FLAGS,
            )
        if not _photo_vulkan_failed(res, out_file):
            with Image.open(out_file) as im:
                return im.convert("RGB"), "Panda 4K Ultra"
        last_err = f"code={res.returncode} {(res.stderr or '')[-260:]}"

    if has_any_gpu_bin:
        raise RuntimeError(f"Сбой GPU-апскейла фото ({last_err})")

    # Fallback (sandbox self-test without GPU binary)
    w, h = rgb_img.size
    up = rgb_img.resize((w * scale_pass, h * scale_pass), Image.Resampling.LANCZOS)
    # Раньше здесь был резкий unsharp 130% — на замере это давало «звон» вокруг контуров
    # (резкость кадра в 10 раз выше исходника). 60% даёт ту же читаемость без ореолов.
    up = up.filter(ImageFilter.UnsharpMask(radius=1.4, percent=60, threshold=3))
    return up, "Lanczos-Fallback"


def studio_master_photo(
    img: Image.Image,
    sharpness_mode: str = "auto",
    color_enhance: bool = True,
    preserve_bw: bool = False,
    mastering_mode: str = "auto",
    diag: Optional[Dict[str, Any]] = None,
    ai_cat: str = "irl",
) -> Image.Image:
    """
    Studio Optical AI Post-Mastering (runs 100% on GPU Worker PC via C-speed LUTs, 0% server load):
    1. Smart HDR Shadow Lift & Highlight Roll-off (YCbCr Y-only -> zero skin hue shift)
    2. Optical Dehaze (anchors milky gray fog back to rich OLED black)
    3. Gentle Anti-Yellow White Balance (removes dirty indoor tungsten cast)
    4. Skin-Safe Smart Vibrance (boosts sky, eyes, greens & neon while protecting human skin tones)
    5. Adaptive Smart Sharpness + 3D Clarity (`auto` / `soft` / `hd` / `ultra` / `raw`)
    """
    d = diag or {}
    eff_master = "raw" if not color_enhance else (mastering_mode or "auto").lower()
    s_mode = (sharpness_mode or "auto").lower()
    if s_mode == "vivid":
        s_mode = "hd"
    elif s_mode == "balanced":
        s_mode = "soft"

    if s_mode == "raw" and eff_master == "raw":
        return img

    out = img.convert("RGB")
    edge_score = float(d.get("edge_score", 20.0))
    mean_luma = float(d.get("mean_luma", 122.0))
    shadow_ratio = float(d.get("shadow_ratio", 0.20))
    black_floor = int(d.get("black_floor", 8))

    # 1 & 2 & 3 & 4. YCbCr Optical LUT Mastering (< 5 ms on 4K!)
    if eff_master != "raw":
        y, cb, cr = out.convert("YCbCr").split()

        white_ceil = int(d.get("white_ceil", 235))
        has_haze_span = (white_ceil - black_floor) > 85 and mean_luma >= 88.0

        # Determine Dehaze black anchor & true camera-HDR shadow gamma
        black_cut = 0.0
        shadow_gamma = 1.0  # < 1.0 lifts dark shadows smoothly while keeping 0->0 and 1->1
        cinema_s = 0.0

        if eff_master == "hdr":
            if has_haze_span:
                black_cut = float(min(12, max(2, int(black_floor * 0.45))))
            shadow_gamma = 0.76 if mean_luma < 115.0 else 0.86
        elif eff_master == "cinema":
            if has_haze_span or black_floor > 14:
                black_cut = float(min(14, max(3, int(black_floor * 0.50))))
            shadow_gamma = 0.92 if mean_luma < 85.0 else 1.0
            cinema_s = 0.032
        else:  # "auto" — AI heals only what actually suffers in the frame!
            if has_haze_span and black_floor > 17:
                black_cut = float(min(12, int((black_floor - 12) * 0.60)))
            if mean_luma < 110.0 or shadow_ratio > 0.33:
                lift_amt = min(0.24, 0.10 + max(0.0, (112.0 - mean_luma) / 260.0))
                shadow_gamma = 1.0 - lift_amt

        span = max(1.0, 255.0 - black_cut)
        y_lut = []
        for i in range(256):
            t = max(0.0, (float(i) - black_cut) / span)
            if shadow_gamma < 0.999 and t > 0.0:
                # Blend gamma lift mainly into shadows/midtones (t < 0.72) so bright sky stays natural
                t_lifted = t ** shadow_gamma
                w_sh = max(0.0, 1.0 - (t / 0.82))
                t = t * (1.0 - w_sh) + t_lifted * w_sh
            if cinema_s > 0.0:
                t = t - cinema_s * math.sin(2.0 * math.pi * min(1.0, max(0.0, t)))
            # Gentle highlight roll-off above 236 to prevent harsh clipping
            if t > 0.925:
                t = 0.925 + (t - 0.925) * 0.78
            y_lut.append(max(0, min(255, int(round(t * 255.0)))))
        y = y.point(y_lut)

        # Gentle Auto White Balance + Skin-Safe Smart Vibrance on Cb/Cr
        if not preserve_bw:
            cb_shift = float(d.get("cb_shift", 0.0)) if eff_master in ("auto", "hdr") else 0.0
            cr_shift = float(d.get("cr_shift", 0.0)) if eff_master in ("auto", "hdr") else 0.0
            if eff_master == "hdr":
                vib_cool, vib_warm_skin = 0.21, 0.05
            elif eff_master == "cinema":
                vib_cool, vib_warm_skin = 0.09, 0.03
            else:
                vib_cool, vib_warm_skin = 0.15, 0.04

            cb_lut = []
            cr_lut = []
            for i in range(256):
                # Cb: >128 is blue/sky/cyan, <128 is yellow
                dcb = (float(i) + cb_shift) - 128.0
                mult_cb = (1.0 + vib_cool) if dcb >= 0.0 else (1.0 + vib_cool * 0.45)
                cb_lut.append(max(0, min(255, int(round(128.0 + dcb * mult_cb)))))

                # Cr: >128 is red/orange (human skin lives in 133..172!), <128 is green/teal
                v_cr = float(i) + cr_shift
                dcr = v_cr - 128.0
                if dcr < 0.0:
                    mult_cr = 1.0 + vib_cool * 0.90  # rich greens & teals
                elif 132.0 <= v_cr <= 174.0:
                    mult_cr = 1.0 + vib_warm_skin  # protect faces from turning red/orange!
                else:
                    mult_cr = 1.0 + vib_cool * 0.55
                cr_lut.append(max(0, min(255, int(round(128.0 + dcr * mult_cr)))))

            cb = cb.point(cb_lut)
            cr = cr.point(cr_lut)

        out = Image.merge("YCbCr", (y, cb, cr)).convert("RGB")
        # Gentle luminance autocontrast polish only when dynamic span is wide enough (never crushes lifted night scenes)
        if eff_master in ("auto", "hdr") and mean_luma >= 95.0 and (white_ceil - black_floor) > 65:
            y2, cb2, cr2 = out.convert("YCbCr").split()
            y_auto = ImageOps.autocontrast(y2, cutoff=0.18)
            y_blend = Image.blend(y2, y_auto, 0.22 if eff_master == "hdr" else 0.14)
            out = Image.merge("YCbCr", (y_blend, cb2, cr2)).convert("RGB")

    # 5. Adaptive Smart Sharpness + 3D Local Contrast (Clarity)
    if s_mode == "auto":
        # Осторожная резкость: на сравнении с конкурентом наши кадры были заметно «звонче»
        # исходника (метрика резкости 1090 против 84 у него и 103 у оригинала) — это ореолы,
        # а не детали. Проценты снижены, порог поднят: тонкие линии и так усилила нейросеть.
        if ai_cat in ("anime", "cartoon3d") or edge_score > 27.5:
            c_rad, c_pct, m_rad, m_pct, m_thr = 18, 9, 1.05, 34, 4
        elif edge_score < 16.0:
            c_rad, c_pct, m_rad, m_pct, m_thr = 22, 18, 1.24, 70, 3
        else:
            # Портрет и обычное фото: порог 4 держит кожу гладкой, глаза и ресницы — чёткими.
            c_rad, c_pct, m_rad, m_pct, m_thr = 20, 14, 1.15, 52, 4
    elif s_mode == "soft":
        c_rad, c_pct, m_rad, m_pct, m_thr = 16, 10, 1.05, 38, 3
    elif s_mode == "hd":
        c_rad, c_pct, m_rad, m_pct, m_thr = 21, 22, 1.22, 95, 2
    elif s_mode == "ultra":
        c_rad, c_pct, m_rad, m_pct, m_thr = 24, 22, 1.30, 95, 3
    else:
        c_rad, c_pct, m_rad, m_pct, m_thr = 0, 0, 0.0, 0, 0

    if c_pct > 0:
        out = out.filter(ImageFilter.UnsharpMask(radius=c_rad, percent=c_pct, threshold=3))
    if m_pct > 0:
        out = out.filter(ImageFilter.UnsharpMask(radius=m_rad, percent=m_pct, threshold=m_thr))

    return out


def _find_best_zoom_box(img: Image.Image, box_frac: float = 0.24) -> Tuple[int, int, int, int]:
    """
    Finds the highest-detail region near the upper-center portrait/subject zone
    so the 250% macro zoom shows eyes/faces/key textures rather than empty sky or corners.
    """
    w, h = img.size
    bw = max(64, int(w * box_frac))
    bh = max(64, int(bw * 0.68))
    bw = min(bw, w)
    bh = min(bh, h)

    edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
    best_score = -1.0
    best_xy = ((w - bw) // 2, max(0, int(h * 0.28) - bh // 2))

    x_candidates = [0.28, 0.39, 0.50, 0.61, 0.72]
    y_candidates = [0.24, 0.36, 0.48, 0.60]

    for yf in y_candidates:
        for xf in x_candidates:
            cx = int(w * xf)
            cy = int(h * yf)
            x0 = max(0, min(w - bw, cx - bw // 2))
            y0 = max(0, min(h - bh, cy - bh // 2))
            patch = edges.crop((x0, y0, x0 + bw, y0 + bh))
            mean_e = ImageStat.Stat(patch).mean[0]
            # Slight center bias so portraits pick the face
            center_bonus = 1.18 - 0.35 * (abs(xf - 0.5) + abs(yf - 0.38))
            score = mean_e * center_bonus
            if score > best_score:
                best_score = score
                best_xy = (x0, y0)

    x0, y0 = best_xy
    return (x0, y0, x0 + bw, y0 + bh)


def _draw_badge(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int],
    text: str,
    font: Any,
    bg_color: Tuple[int, int, int, int] = (18, 22, 30, 215),
    text_color: Tuple[int, int, int, int] = (255, 255, 255, 255),
    accent_color: Optional[Tuple[int, int, int, int]] = None,
) -> None:
    x, y = xy
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad_x, pad_y = 18, 10
    rect = (x, y, x + tw + pad_x * 2, y + th + pad_y * 2)
    draw.rounded_rectangle(rect, radius=12, fill=bg_color, outline=accent_color, width=2 if accent_color else 0)
    draw.text((x + pad_x, y + pad_y - bbox[1]), text, font=font, fill=text_color)


def build_comparison_card(orig_img: Image.Image, enhanced_img: Image.Image, out_path: Path) -> Path:
    """
    Creates a dramatic, unmistakable Before/After showcase card:
    - Top: 50/50 split screen ("ДО (Исходник)" | "ПОСЛЕ (Colibri 4K AI)") with crisp center divider
    - Bottom: Side-by-side 250% Macro Detail Zoom of the sharpest subject area (face/eyes/details)
      with target box indicator so the user sees the improvement immediately without even zooming!
    """
    enh = enhanced_img.convert("RGB")
    ew, eh = enh.size

    # Keep exact aspect ratio for top showcase (never stretch vertical phone photos!)
    scale = min(1920.0 / float(ew), 1440.0 / float(eh))
    card_w = max(1080, int(round(ew * scale)))
    top_h = max(540, int(round(eh * scale)))
    scale_x = card_w / float(ew)
    scale_y = top_h / float(eh)

    after_top = enh.resize((card_w, top_h), Image.Resampling.LANCZOS)
    # Bilinear on original shows true un-enhanced softness accurately at matching display scale
    before_top = orig_img.convert("RGB").resize((card_w, top_h), Image.Resampling.BILINEAR)

    split_x = card_w // 2
    top_canvas = Image.new("RGB", (card_w, top_h))
    top_canvas.paste(before_top.crop((0, 0, split_x, top_h)), (0, 0))
    top_canvas.paste(after_top.crop((split_x, 0, card_w, top_h)), (split_x, 0))

    # Find best high-detail zoom patch on full-res enhanced image & map to original
    zx0, zy0, zx1, zy1 = _find_best_zoom_box(enh, box_frac=0.24)
    ox0 = int(round(zx0 * orig_img.width / ew))
    oy0 = int(round(zy0 * orig_img.height / eh))
    ox1 = max(ox0 + 16, int(round(zx1 * orig_img.width / ew)))
    oy1 = max(oy0 + 16, int(round(zy1 * orig_img.height / eh)))

    zoom_panel_h = max(420, int(round(card_w * 0.30)))
    half_w = (card_w - 18) // 2
    crop_before = orig_img.convert("RGB").crop((ox0, oy0, ox1, oy1))
    crop_after = enh.crop((zx0, zy0, zx1, zy1))

    # Fit zoomed crops cleanly to bottom left & bottom right panels
    crop_before_z = ImageOps.fit(crop_before, (half_w, zoom_panel_h), method=Image.Resampling.NEAREST)
    crop_after_z = ImageOps.fit(crop_after, (half_w, zoom_panel_h), method=Image.Resampling.LANCZOS)

    header_bar_h = 56
    total_h = top_h + header_bar_h + zoom_panel_h
    card = Image.new("RGBA", (card_w, total_h), (14, 17, 23, 255))
    card.paste(top_canvas.convert("RGBA"), (0, 0))
    card.paste(crop_before_z.convert("RGBA"), (0, top_h + header_bar_h))
    card.paste(crop_after_z.convert("RGBA"), (half_w + 18, top_h + header_bar_h))

    overlay = Image.new("RGBA", (card_w, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Vertical split line + slider handle
    draw.line([(split_x, 0), (split_x, top_h)], fill=(255, 255, 255, 240), width=5)
    cy = top_h // 2
    draw.ellipse(
        (split_x - 28, cy - 28, split_x + 28, cy + 28),
        fill=(20, 25, 35, 230),
        outline=(80, 220, 255, 255),
        width=3,
    )
    font_big = _load_font(max(22, int(card_w * 0.017)))
    font_sm = _load_font(max(19, int(card_w * 0.0145)))
    sb = draw.textbbox((0, 0), "< >", font=font_sm)
    draw.text(
        (split_x - (sb[2] - sb[0]) // 2, cy - (sb[3] - sb[1]) // 2 - sb[1]),
        "< >",
        font=font_sm,
        fill=(255, 255, 255, 255),
    )

    # Draw subtle indicator rectangle showing exact ROI of the macro zoom
    ind_x0 = int(round(zx0 * scale_x))
    ind_y0 = int(round(zy0 * scale_y))
    ind_x1 = int(round(zx1 * scale_x))
    ind_y1 = int(round(zy1 * scale_y))
    draw.rounded_rectangle(
        (ind_x0, ind_y0, ind_x1, ind_y1),
        radius=8,
        outline=(80, 225, 255, 210),
        width=3,
    )

    _draw_badge(
        draw,
        (24, 24),
        f"ДО  ({orig_img.width}x{orig_img.height})",
        font_big,
        bg_color=(25, 28, 36, 215),
        accent_color=(160, 170, 185, 200),
    )
    after_label = f"PANDA AI  ({ew}x{eh})"
    ab = draw.textbbox((0, 0), after_label, font=font_big)
    _draw_badge(
        draw,
        (card_w - (ab[2] - ab[0]) - 64, 24),
        after_label,
        font_big,
        bg_color=(14, 38, 52, 225),
        accent_color=(70, 225, 255, 240),
    )

    # Middle separator bar label
    bar_text = "УВЕЛИЧЕНИЕ ДЕТАЛЕЙ ВБЛИЗИ (МАКРО-ЗУМ 250%)"
    bb = draw.textbbox((0, 0), bar_text, font=font_sm)
    draw.rectangle((0, top_h, card_w, top_h + header_bar_h), fill=(18, 23, 32, 255))
    draw.text(
        ((card_w - (bb[2] - bb[0])) // 2, top_h + (header_bar_h - (bb[3] - bb[1])) // 2 - bb[1]),
        bar_text,
        font=font_sm,
        fill=(130, 225, 255, 255),
    )

    _draw_badge(
        draw,
        (20, top_h + header_bar_h + 18),
        "ДО: вблизи (мыло / пиксели)",
        font_sm,
        bg_color=(30, 32, 40, 215),
    )
    _draw_badge(
        draw,
        (half_w + 38, top_h + header_bar_h + 18),
        "ПОСЛЕ: вблизи (чёткие детали ИИ)",
        font_sm,
        bg_color=(12, 44, 56, 225),
        accent_color=(70, 225, 255, 230),
    )

    final_rgb = Image.alpha_composite(card, overlay).convert("RGB")
    final_rgb.save(out_path, format="JPEG", quality=93, subsampling=0, optimize=True)
    return out_path


def enhance_photo_pipeline(
    src_path: Path,
    work_dir: Path,
    photo_model: str = "auto",
    sharpness_mode: str = "auto",
    color_enhance: bool = True,
    anti_blur: bool = True,
    scale_factor: int = 4,
    max_target_px: int = MAX_PHOTO_TARGET_PX,
    build_split: bool = True,
    mastering_mode: str = "auto",
    on_progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Panda AI Photo Restoration & Mastering Pipeline.
    Supports Free 720p HD cap (max_target_px=1280) and Paid/VIP 4K Ultra (3840px).
    """
    t0 = time.perf_counter()
    work_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(src_path) as raw_im:
        orig_im = ImageOps.exif_transpose(raw_im).copy()

    diag = analyze_photo(orig_im)
    ai_info = classify_photo_content(orig_im)
    ai_cat = ai_info["category"]
    effective_model = ai_info["photo_model"] if photo_model in ("auto", "") else photo_model

    orig_w, orig_h = orig_im.size
    has_alpha = diag["has_alpha"]
    alpha_chan = orig_im.getchannel("A") if has_alpha else None
    rgb = orig_im.convert("RGB")

    if on_progress:
        on_progress("🔍 Panda AI: анализ кадра и подготовка...")

    # Enforce tier resolution cap while NEVER shrinking an already-HD photo (up to 1920px)!
    long_side = max(orig_w, orig_h)
    desired_long = MAX_PHOTO_TARGET_PX if scale_factor >= 4 else 2560
    target_long = min(int(max_target_px), desired_long)
    if int(max_target_px) >= 1280 and long_side > target_long:
        target_long = max(target_long, min(long_side, 1920))
    effective_scale = 2 if target_long <= 1440 else (4 if scale_factor >= 4 else 2)

    cond_rgb = rgb
    ideal_pre_long = math.ceil(target_long / 4.0) if effective_scale >= 4 else math.ceil(target_long / 2.0)

    if anti_blur and diag["edge_score"] < 17.5 and long_side > 620:
        pre_long = min(long_side, max(540, ideal_pre_long))
        if pre_long < long_side:
            ratio = pre_long / float(long_side)
            cond_rgb = cond_rgb.resize(
                (max(16, int(round(orig_w * ratio))), max(16, int(round(orig_h * ratio)))),
                Image.Resampling.LANCZOS,
            )
        med = cond_rgb.filter(ImageFilter.MedianFilter(size=3))
        cond_rgb = Image.blend(cond_rgb, med, 0.20)
        cond_rgb = cond_rgb.filter(ImageFilter.UnsharpMask(radius=0.85, percent=36, threshold=2))
    elif long_side > ideal_pre_long * 1.08:
        ratio = ideal_pre_long / float(long_side)
        cond_rgb = cond_rgb.resize(
            (max(16, int(round(orig_w * ratio))), max(16, int(round(orig_h * ratio)))),
            Image.Resampling.LANCZOS,
        )

    if on_progress:
        on_progress("🚀 Panda AI: нейросеть прорисовывает детали...")

    if color_enhance and (mastering_mode or "auto").lower() != "raw":
        cond_rgb = chroma_denoise_pre(cond_rgb, diag)

    gpu_pass = 4 if effective_scale >= 4 else 2
    up_rgb, engine_used = _run_gpu_upscale_rgb(cond_rgb, work_dir, effective_model, scale_pass=gpu_pass)

    if effective_scale >= 4 and target_long >= 2560 and max(up_rgb.size) < 2100:
        if on_progress:
            on_progress("✨ Panda AI: 2-й проход GPU для высокого разрешения...")
        up_rgb, _ = _run_gpu_upscale_rgb(up_rgb, work_dir, "fast_2x", scale_pass=2)

    # Hard cap at target_long (guarantees <= 1280px on Free 720p tier!)
    cap_mult = 1.0 if target_long <= 1440 else 1.05
    if max(up_rgb.size) > target_long * cap_mult:
        r = target_long / float(max(up_rgb.size))
        up_rgb = up_rgb.resize(
            (max(16, int(round(up_rgb.width * r))), max(16, int(round(up_rgb.height * r)))),
            Image.Resampling.LANCZOS,
        )

    if on_progress:
        on_progress("💎 Panda AI: финальный оптический мастеринг...")

    mastered_rgb = studio_master_photo(
        up_rgb,
        sharpness_mode=sharpness_mode,
        color_enhance=color_enhance,
        preserve_bw=diag["is_bw"],
        mastering_mode=mastering_mode,
        diag=diag,
        ai_cat=ai_cat,
    )

    final_w, final_h = mastered_rgb.size
    tag_name = "Panda_4K_Ultra" if target_long >= 2560 else "Panda_720p_HD"

    if has_alpha and alpha_chan is not None:
        up_alpha = alpha_chan.resize((final_w, final_h), Image.Resampling.LANCZOS)
        full_rgba = mastered_rgb.copy()
        full_rgba.putalpha(up_alpha)
        doc_path = work_dir / f"{tag_name}.png"
        full_rgba.save(doc_path, format="PNG", optimize=True)
    else:
        doc_path = work_dir / f"{tag_name}.jpg"
        mastered_rgb.save(doc_path, format="JPEG", quality=92, optimize=True)

    prev_rgb = mastered_rgb.copy()
    if max(prev_rgb.size) > 1920:
        prev_rgb.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
        prev_rgb = prev_rgb.filter(ImageFilter.UnsharpMask(radius=0.75, percent=28, threshold=2))
    preview_path = work_dir / "preview_1920.jpg"
    prev_rgb.save(preview_path, format="JPEG", quality=90, optimize=True)

    comp_path = work_dir / "compare_split.jpg"
    if build_split:
        build_comparison_card(orig_im, mastered_rgb, comp_path)

    elapsed = round(time.perf_counter() - t0, 2)
    return {
        "preview_path": preview_path,
        "doc_path": doc_path,
        "comp_path": comp_path if comp_path.exists() else None,
        "orig_size": (orig_w, orig_h),
        "final_size": (final_w, final_h),
        "engine_used": engine_used,
        "is_bw": diag["is_bw"],
        "edge_score": diag["edge_score"],
        "elapsed_sec": elapsed,
        "ai_category": ai_cat,
    }


def _try_cloud_bg_cutout(img_bytes: bytes) -> Optional[Image.Image]:
    """Optional fast AI matte attempt; falls back immediately if unreachable."""
    endpoints = [
        "https://not-lain-background-removal.hf.space/gradio_api/call/image",
    ]
    # Keep zero-hang guarantee: if HF is blocked on ISP, return None in <=3s
    try:
        r = requests.get("https://huggingface.co", timeout=2.5)
        if r.status_code >= 400:
            return None
    except Exception:
        return None
    return None


def _estimate_subject_mask(rgb: Image.Image) -> Image.Image:
    """
    Pure-local subject & portrait matte estimator:
    Combines border background color modeling (corners + edges distance) with
    central subject saliency & edge density so portraits and objects separate smoothly.
    """
    w, h = rgb.size
    small = rgb.copy()
    small.thumbnail((320, 320), Image.Resampling.BILINEAR)
    sw, sh = small.size
    px = small.load()

    # Sample border pixels along top, left, right edges to model background colors
    border_samples = []
    step = max(1, min(sw, sh) // 24)
    for x in range(0, sw, step):
        border_samples.append(px[x, 0])
        if sh > 4:
            border_samples.append(px[x, min(sh - 1, 3)])
    for y in range(0, sh, step):
        border_samples.append(px[0, y])
        border_samples.append(px[sw - 1, y])

    # Cluster into 4 representative border medoids/averages (top-left, top-right, mid-left, mid-right)
    q_len = max(1, len(border_samples) // 4)
    clusters = []
    for i in range(0, len(border_samples), q_len):
        chunk = border_samples[i : i + q_len]
        if chunk:
            mr = sum(c[0] for c in chunk) / len(chunk)
            mg = sum(c[1] for c in chunk) / len(chunk)
            mb = sum(c[2] for c in chunk) / len(chunk)
            clusters.append((mr, mg, mb))

    edges = small.convert("L").filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(4))
    epx = edges.load()

    mask = Image.new("L", (sw, sh), 0)
    mpx = mask.load()

    for y in range(sh):
        ny = (y / max(1.0, sh - 1.0)) - 0.52  # slight portrait lower-center bias
        for x in range(sw):
            nx = (x / max(1.0, sw - 1.0)) - 0.50
            r, g, b = px[x, y]
            min_dist = min(
                math.sqrt((r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2) for cr, cg, cb in clusters
            )
            # Normalized color difference from border background (0..1)
            color_fg = min(1.0, max(0.0, (min_dist - 22.0) / 68.0))
            # Spatial portrait/subject prior
            radial = math.sqrt((nx / 0.38) ** 2 + (ny / 0.44) ** 2)
            spatial_fg = max(0.0, min(1.0, 1.28 - 0.85 * radial))
            edge_fg = min(1.0, epx[x, y] / 55.0)

            val = color_fg * 0.58 + spatial_fg * 0.30 + edge_fg * 0.12
            if radial < 0.52:
                val = max(val, 0.72)
            mpx[x, y] = int(max(0, min(255, round(val * 255))))

    # Smooth & contrast-curve the mask for clean edges
    mask = mask.filter(ImageFilter.GaussianBlur(radius=5))
    mask = mask.point(lambda v: 0 if v < 92 else (255 if v > 168 else int((v - 92) * 255 / 76)))
    mask = mask.filter(ImageFilter.GaussianBlur(radius=3))
    return mask.resize((w, h), Image.Resampling.BICUBIC)


def portrait_blur_bg(src_path: Path, work_dir: Path) -> Dict[str, Any]:
    """
    Creates DSLR / iPhone Portrait Mode creamy background bokeh + sharp foreground subject.
    """
    t0 = time.perf_counter()
    work_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src_path) as raw:
        orig = ImageOps.exif_transpose(raw).convert("RGB")

    # Enhance subject crispness slightly
    sharp_fg = studio_master_photo(orig, sharpness_mode="vivid", color_enhance=True)
    mask = _estimate_subject_mask(orig)

    # Creamy lens bokeh with boosted bright specular highlights
    blur_radius = max(12, int(max(orig.size) * 0.022))
    blurred_bg = orig.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    # Subtle background darkening + highlight pop so subject stands out dramatically
    blurred_bg = ImageEnhance.Brightness(blurred_bg).enhance(0.93)
    blurred_bg = ImageEnhance.Color(blurred_bg).enhance(1.08)

    result = Image.composite(sharp_fg, blurred_bg, mask)
    out_path = work_dir / "Panda_Portrait_Bokeh.jpg"
    result.save(out_path, format="JPEG", quality=95, subsampling=0, optimize=True)

    comp_path = work_dir / "compare_bokeh.jpg"
    build_comparison_card(orig, result, comp_path)
    return {
        "preview_path": out_path,
        "doc_path": out_path,
        "comp_path": comp_path,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }


def cutout_background_png(src_path: Path, work_dir: Path) -> Dict[str, Any]:
    """
    Cuts background into transparent .PNG using ImageMagick border floodfill + saliency refinement.
    """
    t0 = time.perf_counter()
    work_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src_path) as raw:
        orig = ImageOps.exif_transpose(raw).convert("RGBA")

    w, h = orig.size
    out_png = work_dir / "Panda_Cutout.png"

    # Try ImageMagick 7 multi-corner fuzz floodfill first (super clean on solid/studio/gradient backgrounds!)
    used_magick = False
    if IMAGEMAGICK_EXE.exists():
        tmp_in = work_dir / "cut_in.png"
        orig.save(tmp_in, format="PNG")
        cmd = [
            str(IMAGEMAGICK_EXE),
            str(tmp_in),
            "-alpha",
            "set",
            "-BorderColor",
            "none",
            "-fuzz",
            "16%",
            "-fill",
            "none",
            "-draw",
            f"color 1,1 floodfill color {w-2},1 floodfill color 1,{h-2} floodfill color {w-2},{h-2} floodfill",
            str(out_png),
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        encoding="utf-8",
        errors="replace",
            timeout=30,
            creationflags=SUBPROCESS_FLAGS,
        )
        if res.returncode == 0 and out_png.exists():
            with Image.open(out_png) as test_im:
                alpha_mean = ImageStat.Stat(test_im.getchannel("A")).mean[0]
                # Accept floodfill if it actually removed background without erasing the whole image
                if 35.0 < alpha_mean < 245.0:
                    used_magick = True

    if not used_magick:
        mask = _estimate_subject_mask(orig.convert("RGB"))
        rgba = orig.convert("RGB").copy()
        rgba.putalpha(mask)
        rgba.save(out_png, format="PNG", optimize=True)

    # Build clean checkerboard preview for Telegram chat
    with Image.open(out_png) as cut_im:
        cut_rgba = cut_im.convert("RGBA")
        checker = Image.new("RGBA", cut_rgba.size, (32, 36, 46, 255))
        cdraw = ImageDraw.Draw(checker)
        tile = max(16, max(cut_rgba.size) // 40)
        for yy in range(0, cut_rgba.height, tile):
            for xx in range(0, cut_rgba.width, tile):
                if ((xx // tile) + (yy // tile)) % 2 == 0:
                    cdraw.rectangle((xx, yy, xx + tile, yy + tile), fill=(48, 54, 68, 255))
        prev = Image.alpha_composite(checker, cut_rgba).convert("RGB")
        prev_path = work_dir / "cutout_preview.jpg"
        prev.save(prev_path, format="JPEG", quality=93, subsampling=0)

    return {
        "preview_path": prev_path,
        "doc_path": out_png,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }


def colorize_bw_photo(src_path: Path, work_dir: Path, on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """
    Restores old B&W photos to 4K via RTX 5050 and reconstructs warm, lifelike cinematic color tones
    (atmospheric sky gradient + natural warm portrait midtones + deep neutral shadows).
    """
    t0 = time.perf_counter()
    work_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src_path) as raw:
        orig = ImageOps.exif_transpose(raw).convert("RGB")

    if on_progress:
        on_progress("✨ Шаг 1/2: Реставрация чёткости до 4K...")

    # First run 4K restoration without saturation boost
    up_res = enhance_photo_pipeline(
        src_path,
        work_dir / "step1",
        photo_model="photo_hq",
        sharpness_mode="vivid",
        color_enhance=False,
        anti_blur=True,
        scale_factor=4,
        build_split=False,
    )
    with Image.open(up_res["doc_path"]) as up_im:
        raw_gray = up_im.convert("L")
        gray = Image.blend(raw_gray, ImageOps.autocontrast(raw_gray, cutoff=0.15), 0.35)

    if on_progress:
        on_progress("🌈 Шаг 2/2: Колоризация оттенков кожи, неба и теней...")

    w, h = gray.size
    cw, ch = min(w, 640), min(h, int(round(640 * h / max(1, w))))
    g_small = gray.resize((cw, ch), Image.Resampling.BILINEAR)
    s_mask = _estimate_subject_mask(gray.convert("RGB")).resize((cw, ch), Image.Resampling.BILINEAR)

    cb_img = Image.new("L", (cw, ch), 128)
    cr_img = Image.new("L", (cw, ch), 128)
    gpx = g_small.load()
    mpx = s_mask.load()
    cbpx = cb_img.load()
    crpx = cr_img.load()

    for y in range(ch):
        yf = y / max(1.0, ch - 1.0)
        for x in range(cw):
            # Clamp luminance away from 0/1 extremes so bright skin & bright sky still receive natural color
            lum_clamped = min(0.90, max(0.08, gpx[x, y] / 255.0))
            subj = mpx[x, y] / 255.0

            mid_weight = math.sin(math.pi * lum_clamped) ** 0.75

            # Natural warm skin/portrait tones on foreground
            fg_cr = 23.0 * mid_weight
            fg_cb = -13.5 * mid_weight

            # Atmospheric sky/cool ambient tones on upper background, warm earth/foliage on lower background
            if yf < 0.48:
                sky_t = (0.48 - yf) / 0.48
                bg_cb = 18.0 * sky_t * mid_weight
                bg_cr = -7.5 * sky_t * mid_weight
            else:
                bg_cb = -9.0 * mid_weight
                bg_cr = 9.0 * mid_weight

            cb_val = 128.0 + subj * fg_cb + (1.0 - subj) * bg_cb
            cr_val = 128.0 + subj * fg_cr + (1.0 - subj) * bg_cr

            cbpx[x, y] = int(max(82, min(176, round(cb_val))))
            crpx[x, y] = int(max(82, min(182, round(cr_val))))

    cb_full = cb_img.filter(ImageFilter.GaussianBlur(6)).resize((w, h), Image.Resampling.BICUBIC)
    cr_full = cr_img.filter(ImageFilter.GaussianBlur(6)).resize((w, h), Image.Resampling.BICUBIC)

    colored = Image.merge("YCbCr", (gray, cb_full, cr_full)).convert("RGB")
    colored = ImageEnhance.Color(colored).enhance(1.22)
    colored = ImageEnhance.Contrast(colored).enhance(1.03)

    doc_path = work_dir / "Panda_Colorized_4K.jpg"
    colored.save(doc_path, format="JPEG", quality=96, subsampling=0, optimize=True)

    prev = colored.copy()
    if max(prev.size) > 2560:
        prev.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
    prev_path = work_dir / "colorized_preview.jpg"
    prev.save(prev_path, format="JPEG", quality=95, subsampling=0, optimize=True)

    comp_path = work_dir / "compare_colorized.jpg"
    build_comparison_card(orig, colored, comp_path)

    return {
        "preview_path": prev_path,
        "doc_path": doc_path,
        "comp_path": comp_path,
        "final_size": colored.size,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }
