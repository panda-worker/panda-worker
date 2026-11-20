# -*- coding: utf-8 -*-
"""
panda_worker/panda_theme.py — "PANDA production" look for the worker window (v2.5.1).

Black-and-white "paint & film" style: crisp vector widgets drawn on tk.Canvas, rounded cards,
a paint-roller striped progress bar, mode tiles with drawn icons and a 6-cell code entry.
Pure tkinter (+ optional Pillow for HiDPI resampling of the embedded artwork); no network, no
external files are required — fonts/icon are optional extras bundled with the exe.
"""
from __future__ import annotations

import base64
import io
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
import tkinter.font as tkfont

import panda_theme_assets as _A

# ---------------------------------------------------------------- palette (paint on paper)
PAPER = "#F5F5F3"
CARD = "#FFFFFF"
INK = "#111111"
INK_HOVER = "#2A2A2A"
INK_SOFT = "#3B3B3B"
MUTED = "#75736D"
LINE = "#E1E0DA"
SHADOW = "#E9E8E3"
SOFT = "#EEEDE8"
GREEN = "#1E9B57"
AMBER = "#D08A06"
RED = "#D23A3A"
RED_SOFT = "#FBECEC"
ON_INK_MUTED = "#B9B8B3"

_SCALE = 1.0
_FAM: Dict[str, Optional[str]] = {}
_FALLBACK = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
_FONT_FILES = {
    "black": ("PandaSans Black", "PandaSansBlack.ttf"),
    "xbold": ("PandaSans ExtraBold", "PandaSansExtraBold.ttf"),
    "bold": ("PandaSans Bold", "PandaSansBold.ttf"),
    "semi": ("PandaSans SemiBold", "PandaSansSemiBold.ttf"),
    "reg": ("PandaSans", "PandaSans.ttf"),
}
_FALLBACK_WEIGHT = {"black": "bold", "xbold": "bold", "bold": "bold", "semi": "bold", "reg": "normal"}


# ---------------------------------------------------------------- process-level setup
def prepare_process(app_id: str = "PandaProduction.GPUHelper") -> None:
    """Call BEFORE tk.Tk(): crisp HiDPI rendering + own taskbar identity (no python icon)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system-DPI aware
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def register_fonts(font_dir: Path) -> None:
    """Make bundled PandaSans (Nunito, OFL) available to THIS process only (FR_PRIVATE)."""
    if sys.platform != "win32" or not font_dir.is_dir():
        return
    try:
        import ctypes
        add = ctypes.windll.gdi32.AddFontResourceExW
        for _fam, fname in _FONT_FILES.values():
            p = font_dir / fname
            if p.is_file():
                add(str(p), 0x10, 0)  # FR_PRIVATE
    except Exception:
        pass


def init(root: tk.Tk, base_w: int, base_h: int) -> float:
    """Detect DPI scale (clamped so the window fits the screen) and resolve font families."""
    global _SCALE
    try:
        dpi_scale = float(root.winfo_fpixels("1i")) / 96.0
    except Exception:
        dpi_scale = 1.0
    try:
        scr_h = int(root.winfo_screenheight())
        scr_w = int(root.winfo_screenwidth())
        fit = min((scr_h - 70) / float(base_h), (scr_w - 40) / float(base_w))
    except Exception:
        fit = dpi_scale
    _SCALE = max(0.72, min(dpi_scale, fit, 3.0))
    try:
        fams = set(tkfont.families(root))
    except Exception:
        fams = set()
    for role, (fam, _f) in _FONT_FILES.items():
        _FAM[role] = fam if fam in fams else None
    return _SCALE


def px(v: float) -> int:
    return int(round(v * _SCALE))


def font(role: str, size: float) -> Tuple:
    fam = _FAM.get(role)
    if fam:
        return (fam, -px(size))
    return (_FALLBACK, -px(size), _FALLBACK_WEIGHT.get(role, "normal"))


def _scaled_photo(b64png: str, logical_w: int, stored_factor: float) -> tk.PhotoImage:
    """Embedded @2x PNG -> PhotoImage at the current scale (Pillow LANCZOS, Tk fallback)."""
    target_w = max(1, px(logical_w))
    raw = base64.b64decode(b64png)
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        if im.width != target_w:
            th = max(1, int(round(im.height * target_w / im.width)))
            im = im.resize((target_w, th), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))
    except Exception:
        img = tk.PhotoImage(data=b64png)
        sub = max(1, int(round(stored_factor / max(0.01, _SCALE))))
        return img.subsample(sub) if sub > 1 else img


def header_photo() -> tk.PhotoImage:
    return _scaled_photo(_A.HEADER_PNG_B64, _A.HEADER_SIZE[0], 2.0)


def mascot_photo(logical_h: int = 60) -> tk.PhotoImage:
    from PIL import Image  # noqa: F401  (fallback below handles absence)
    raw = Image.open(io.BytesIO(base64.b64decode(_A.MASCOT_PNG_B64)))
    lw = int(round(raw.width * logical_h / float(raw.height)))
    return _scaled_photo(_A.MASCOT_PNG_B64, lw, 2.0)


def apply_window_icon(root: tk.Tk, ico_path: Optional[Path]) -> None:
    try:
        imgs = [tk.PhotoImage(data=_A.ICON_PNG_B64), tk.PhotoImage(data=_A.ICON64_PNG_B64)]
        root.iconphoto(True, *imgs)
        root._panda_icons = imgs  # keep references alive
    except Exception:
        pass
    if sys.platform == "win32" and ico_path is not None and ico_path.is_file():
        try:
            root.iconbitmap(default=str(ico_path))
        except Exception:
            pass


# ---------------------------------------------------------------- drawing helpers
def _poly_round_rect(cv: tk.Canvas, x1: float, y1: float, x2: float, y2: float, r: float, **kw) -> int:
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, splinesteps=24, **kw)


_AA_CACHE: "Dict[tuple, tk.PhotoImage]" = {}
_AA_OK = [True]


def _hex_rgba(c: Optional[str]) -> Tuple[int, int, int, int]:
    if not c:
        return (0, 0, 0, 0)
    return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16), 255)


def _aa_image(w: int, h: int, r: float, fill: Optional[str], outline: Optional[str], bw: float) -> tk.PhotoImage:
    key = (w, h, round(r, 1), fill, outline, round(bw, 1))
    img = _AA_CACHE.get(key)
    if img is not None:
        return img
    from PIL import Image, ImageDraw
    k = 4
    big = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    rr = max(0, int(min(r, w / 2.0, h / 2.0) * k))
    o = _hex_rgba(outline) if outline else None
    f = _hex_rgba(fill) if fill else None
    if o and bw > 0:
        d.rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=rr, fill=o)
        b = int(round(bw * k))
        d.rounded_rectangle((b, b, w * k - 1 - b, h * k - 1 - b), radius=max(0, rr - b),
                            fill=f if f else (0, 0, 0, 0))
    else:
        d.rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=rr, fill=f or (0, 0, 0, 0))
    small = big.resize((w, h), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "PNG")
    img = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))
    if len(_AA_CACHE) > 400:
        _AA_CACHE.clear()
    _AA_CACHE[key] = img
    return img


def round_rect(cv: tk.Canvas, x1: float, y1: float, x2: float, y2: float, r: float,
               fill: Optional[str] = None, outline: Optional[str] = None, width: float = 1, **kw) -> int:
    """Anti-aliased rounded rectangle (Pillow-rendered, cached); polygon fallback without Pillow."""
    w, h = int(round(x2 - x1)) + 1, int(round(y2 - y1)) + 1
    if _AA_OK[0] and w > 2 and h > 2:
        try:
            img = _aa_image(w, h, r, fill or None, outline or None, float(width) if outline else 0.0)
            return cv.create_image(int(round(x1)), int(round(y1)), image=img, anchor="nw", **kw)
        except Exception:
            _AA_OK[0] = False
    return _poly_round_rect(cv, x1, y1, x2, y2, r, fill=fill or "", outline=outline or "", width=width, **kw)


def draw_icon(cv: tk.Canvas, name: str, cx: float, cy: float, size: float, color: str, bg: str) -> None:
    """Tiny vector icon set (leaf, bolt, flame, moon, pause, play, min, key, trash, arrow, check)."""
    s = size / 24.0

    def P(x: float, y: float) -> Tuple[float, float]:
        return cx + (x - 12) * s, cy + (y - 12) * s

    lw = max(1, int(round(2.2 * s)))
    if name == "leaf":
        pts = [P(4, 20), P(4, 9), P(11, 3), P(21, 3), P(21, 13), P(15, 20), P(4, 20)]
        cv.create_polygon([c for p in pts for c in p], smooth=True, fill=color, outline=color)
        cv.create_line(*P(5, 19), *P(15, 9), fill=bg, width=lw, capstyle="round")
    elif name == "bolt":
        pts = [P(14, 2), P(4, 14), P(11, 14), P(9, 22), P(20, 9), P(13, 9), P(15, 2)]
        cv.create_polygon([c for p in pts for c in p], fill=color, outline=color, joinstyle="round")
    elif name == "flame":
        pts = [P(12, 2), P(18, 9), P(20, 15), P(17, 21), P(12, 23), P(7, 21), P(4, 15), P(7, 9), P(9, 12), P(12, 2)]
        cv.create_polygon([c for p in pts for c in p], smooth=True, fill=color, outline=color)
        inner = [P(12, 12), P(15, 16), P(14.5, 20), P(12, 21), P(9.5, 20), P(9, 16.5), P(12, 12)]
        cv.create_polygon([c for p in inner for c in p], smooth=True, fill=bg, outline=bg)
    elif name == "moon":
        cv.create_oval(*P(3, 3), *P(21, 21), fill=color, outline=color)
        cv.create_oval(*P(9, 0), *P(25, 16), fill=bg, outline=bg)
    elif name == "pause":
        cv.create_line(*P(8.5, 5), *P(8.5, 19), fill=color, width=max(2, int(3.4 * s)), capstyle="round")
        cv.create_line(*P(15.5, 5), *P(15.5, 19), fill=color, width=max(2, int(3.4 * s)), capstyle="round")
    elif name == "play":
        pts = [P(7, 4), P(20, 12), P(7, 20)]
        cv.create_polygon([c for p in pts for c in p], fill=color, outline=color, joinstyle="round",
                          width=max(1, int(2 * s)))
    elif name == "min":
        cv.create_line(*P(5, 17), *P(19, 17), fill=color, width=max(2, int(2.8 * s)), capstyle="round")
    elif name == "key":
        cv.create_oval(*P(2.5, 7.5), *P(11.5, 16.5), outline=color, width=lw)
        cv.create_line(*P(11.5, 12), *P(22, 12), fill=color, width=lw, capstyle="round")
        cv.create_line(*P(18, 12), *P(18, 16), fill=color, width=lw, capstyle="round")
        cv.create_line(*P(21, 12), *P(21, 15), fill=color, width=lw, capstyle="round")
    elif name == "trash":
        cv.create_line(*P(4, 6.5), *P(20, 6.5), fill=color, width=lw, capstyle="round")
        cv.create_line(*P(9.5, 6), *P(10, 3.5), *P(14, 3.5), *P(14.5, 6), fill=color, width=lw, capstyle="round")
        pts = [P(6, 8.5), P(7.2, 21), P(16.8, 21), P(18, 8.5)]
        cv.create_line([c for p in pts for c in p], fill=color, width=lw, capstyle="round", joinstyle="round")
        cv.create_line(*P(10, 11.5), *P(10, 17.5), fill=color, width=max(1, lw - 1), capstyle="round")
        cv.create_line(*P(14, 11.5), *P(14, 17.5), fill=color, width=max(1, lw - 1), capstyle="round")
    elif name == "arrow":
        cv.create_line(*P(4, 12), *P(19, 12), fill=color, width=lw, capstyle="round")
        cv.create_line(*P(13, 6), *P(19.5, 12), *P(13, 18), fill=color, width=lw, capstyle="round",
                       joinstyle="round")
    elif name == "check":
        cv.create_line(*P(5, 12.5), *P(10, 17.5), *P(19.5, 7), fill=color, width=max(2, int(2.8 * s)),
                       capstyle="round", joinstyle="round")
    elif name == "alert":
        pts = [P(12, 2.5), P(22.5, 21), P(1.5, 21)]
        cv.create_polygon([c for p in pts for c in p], fill=color, outline=color, joinstyle="round",
                          width=max(1, int(2 * s)))
        cv.create_line(*P(12, 9), *P(12, 14.5), fill=bg, width=lw, capstyle="round")
        cv.create_oval(*P(10.8, 16.8), *P(13.2, 19.2), fill=bg, outline=bg)


# ---------------------------------------------------------------- widgets
class RoundCard(tk.Canvas):
    """White rounded card with a soft hard-shadow; put children into `.inner`."""

    def __init__(self, parent: tk.Misc, pad: int = 16, radius: int = 14, fill: str = CARD,
                 border: str = LINE, bg: str = PAPER, shadow: bool = True) -> None:
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, height=px(40))
        self._pad, self._r, self._fill, self._border, self._shadow = px(pad), px(radius), fill, border, shadow
        self._sh = px(3) if shadow else 0
        self.inner = tk.Frame(self, bg=fill)
        self._win = self.create_window(self._pad, self._pad, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner, add="+")
        self.bind("<Configure>", self._on_outer, add="+")

    def set_border(self, color: str) -> None:
        self._border = color
        self._redraw()

    def _on_outer(self, e: "tk.Event") -> None:
        self.itemconfigure(self._win, width=max(1, e.width - 2 * self._pad))
        self._redraw()

    def _on_inner(self, _e: "tk.Event") -> None:
        h = self.inner.winfo_reqheight() + 2 * self._pad + self._sh
        if int(float(self.cget("height"))) != h:
            self.configure(height=h)
        self._redraw()

    def _redraw(self) -> None:
        self.delete("bgshape")
        w, h = self.winfo_width(), int(float(self.cget("height")))
        if w < 4:
            return
        if self._shadow:
            round_rect(self, 0, self._sh, w - 1, h - 1, self._r, fill=SHADOW, outline=SHADOW, tags="bgshape")
        round_rect(self, 0, 0, w - 1, h - 1 - self._sh, self._r, fill=self._fill, outline=self._border,
                   width=max(1, px(1)), tags="bgshape")
        self.tag_lower("bgshape")


_BTN_STYLES = {
    #            fill,   hover,    press,    text,    border,  hover_text
    "primary": (INK, INK_HOVER, "#000000", "#FFFFFF", INK, "#FFFFFF"),
    "secondary": (CARD, CARD, SOFT, INK, LINE, INK),
    "ghost": (None, SOFT, LINE, MUTED, None, INK),
    "danger": (CARD, RED, "#B42F2F", RED, "#F0C8C8", "#FFFFFF"),
    "danger_solid": (RED, "#BF3131", "#A82A2A", "#FFFFFF", RED, "#FFFFFF"),
}


class PillButton(tk.Canvas):
    """Rounded canvas button with an optional vector icon, hover/press states and a fixed height."""

    def __init__(self, parent: tk.Misc, text: str = "", command: Optional[Callable[[], None]] = None,
                 kind: str = "secondary", icon: Optional[str] = None, height: int = 42,
                 size: float = 13.5, bg: Optional[str] = None, min_width: int = 0,
                 textvariable: Optional[tk.StringVar] = None) -> None:
        self._bg = bg or (parent.cget("bg") if hasattr(parent, "cget") else PAPER)
        super().__init__(parent, bg=self._bg, highlightthickness=0, bd=0, height=px(height),
                         cursor="hand2", takefocus=0)
        self._cmd = command
        self._kind = kind
        self._icon = icon
        self._size = size
        self._h = px(height)
        self._hover = False
        self._press = False
        self._var = textvariable
        self._text = textvariable.get() if textvariable is not None else text
        self._min_w = px(min_width)
        if textvariable is not None:
            textvariable.trace_add("write", lambda *_a: self.set_text(textvariable.get()))
        self._fit_width()
        self.bind("<Configure>", lambda _e: self._redraw(), add="+")
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _fit_width(self) -> None:
        f = tkfont.Font(font=font("bold", self._size))
        w = f.measure(self._text) + px(36) + (px(26) if self._icon else 0)
        if not self._text:
            w = self._h
        self.configure(width=max(self._min_w, w))

    def set_text(self, text: str) -> None:
        self._text = text
        self._fit_width()
        self._redraw()

    def configure_style(self, kind: Optional[str] = None, icon: Optional[str] = None,
                        text: Optional[str] = None) -> None:
        if kind:
            self._kind = kind
        if icon:
            self._icon = icon
        if text is not None:
            self._text = text
            self._fit_width()
        self._redraw()

    def set_icon(self, icon: Optional[str]) -> None:
        self._icon = icon
        self._redraw()

    def _set_hover(self, v: bool) -> None:
        self._hover = v
        if not v:
            self._press = False
        self._redraw()

    def _on_press(self, _e: "tk.Event") -> None:
        self._press = True
        self._redraw()

    def _on_release(self, e: "tk.Event") -> None:
        was = self._press
        self._press = False
        self._redraw()
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        if was and inside and self._cmd:
            self.after(1, self._cmd)

    def _redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self._h
        if w < 4:
            w = int(float(self.cget("width")))
        fill, hov, prs, txt, bd, htxt = _BTN_STYLES.get(self._kind, _BTN_STYLES["secondary"])
        cur_fill = prs if self._press else (hov if self._hover else fill)
        cur_txt = htxt if (self._hover or self._press) else txt
        cur_bd = bd
        if self._kind == "secondary" and (self._hover or self._press):
            cur_bd = INK
        if self._kind == "danger" and (self._hover or self._press):
            cur_bd = cur_fill
        dy = px(1) if self._press else 0
        r = h / 2.0
        if self._kind in ("primary", "danger_solid") and not self._press:
            round_rect(self, 1, px(3), w - 2, h - 1, r, fill="#CFCEC9", outline="#CFCEC9")
        sh = px(3) if self._kind in ("primary", "danger_solid") else 0
        if cur_fill or cur_bd:
            round_rect(self, 1, 1 + dy, w - 2, h - 1 - sh + dy, r,
                       fill=cur_fill or self._bg, outline=cur_bd or (cur_fill or self._bg), width=max(1, px(1.3)))
        cy = (h - sh) / 2.0 + dy
        f = font("bold", self._size)
        if self._icon and self._text:
            tw = tkfont.Font(font=f).measure(self._text)
            total = px(18) + px(8) + tw
            x0 = (w - total) / 2.0
            draw_icon(self, self._icon, x0 + px(9), cy, px(18), cur_txt, cur_fill or self._bg)
            self.create_text(x0 + px(26), cy, text=self._text, font=f, fill=cur_txt, anchor="w")
        elif self._icon:
            draw_icon(self, self._icon, w / 2.0, cy, px(18), cur_txt, cur_fill or self._bg)
        else:
            self.create_text(w / 2.0, cy, text=self._text, font=f, fill=cur_txt)


class Chip(tk.Canvas):
    """Rounded status chip with a coloured dot (e.g. GPU temperature)."""

    def __init__(self, parent: tk.Misc, text: str = "", color: str = GREEN, bg: Optional[str] = None) -> None:
        self._bg = bg or parent.cget("bg")
        super().__init__(parent, bg=self._bg, highlightthickness=0, bd=0, height=px(30), width=px(110))
        self._text, self._color = text, color
        self._redraw()

    def set(self, text: str, color: str) -> None:
        if (text, color) != (self._text, self._color):
            self._text, self._color = text, color
            self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        f = font("bold", 12.5)
        tw = tkfont.Font(font=f).measure(self._text)
        w = tw + px(40)
        h = px(30)
        self.configure(width=w)
        round_rect(self, 1, 1, w - 2, h - 2, h / 2.0, fill=CARD, outline=LINE, width=max(1, px(1)))
        r = px(4.5)
        cx, cy = px(16), h / 2.0
        self.create_oval(cx - r - px(3), cy - r - px(3), cx + r + px(3), cy + r + px(3),
                         fill=_mix(self._color, CARD, 0.82), outline="")
        self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=self._color, outline="")
        self.create_text(px(28), cy, text=self._text, font=f, fill=INK, anchor="w")


def _mix(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))


class PaintBar(tk.Canvas):
    """Rounded progress bar; when `striped`, diagonal stripes roll like a paint roller."""

    def __init__(self, parent: tk.Misc, height: int = 12, bg: Optional[str] = None) -> None:
        super().__init__(parent, bg=bg or parent.cget("bg"), highlightthickness=0, bd=0, height=px(height))
        self._h = px(height)
        self._pct = 0.0
        self._shown = 0.0
        self._color = INK
        self._striped = False
        self._phase = 0.0
        self.bind("<Configure>", lambda _e: self._redraw(), add="+")
        self.after(40, self._tick)

    def set(self, pct: float, color: str, striped: bool) -> None:
        self._pct = max(0.0, min(100.0, float(pct)))
        self._color = color
        self._striped = striped

    def _tick(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        moved = abs(self._pct - self._shown) > 0.05
        self._shown += (self._pct - self._shown) * 0.18
        if self._striped:
            self._phase = (self._phase + px(0.9)) % px(16)
        if moved or self._striped:
            self._redraw()
        self.after(40, self._tick)

    def _redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self._h
        if w < 6:
            return
        r = h / 2.0
        round_rect(self, 0, 0, w - 1, h - 1, r, fill=SOFT)
        fw = max(h, w * self._shown / 100.0) if self._shown > 0.2 else 0
        if fw <= 0:
            return
        _poly_round_rect(self, 0, 0, fw - 1, h - 1, r, fill=self._color, outline=self._color)
        if self._striped and fw > h * 1.5:
            step = px(16)
            lo, hi = r, fw - r
            stripe = _mix(self._color, "#FFFFFF", 0.16)
            x = -step + self._phase
            while x < fw:
                xs = [x, x + step * 0.5, x + step * 0.5 - h * 0.6, x - h * 0.6]
                ys = [0, 0, h - 1, h - 1]
                xs = [min(hi, max(lo, v)) for v in xs]
                if xs[1] - xs[0] > 0.5 or xs[2] - xs[3] > 0.5:
                    self.create_polygon(xs[0], ys[0], xs[1], ys[1], xs[2], ys[2], xs[3], ys[3],
                                        fill=stripe, outline="")
                x += step
        # glossy top highlight
        if fw > h:
            self.create_line(r, px(2.5), fw - r, px(2.5), fill=_mix(self._color, "#FFFFFF", 0.28),
                             width=max(1, px(1.2)), capstyle="round")


class ModeTile(tk.Canvas):
    """Selectable load-mode card with a drawn icon, multiplier badge and description."""

    def __init__(self, parent: tk.Misc, key: str, title: str, badge: str, desc: str, icon: str,
                 command: Callable[[str], None], height: int = 92) -> None:
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0, height=px(height),
                         cursor="hand2")
        self.key, self._title, self._badge, self._desc, self._icon = key, title, badge, desc, icon
        self._cmd = command
        self._sel = False
        self._hover = False
        self.bind("<Configure>", lambda _e: self._redraw(), add="+")
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: self._cmd(self.key))

    def set_selected(self, sel: bool) -> None:
        if sel != self._sel:
            self._sel = sel
            self._redraw()

    def _set_hover(self, v: bool) -> None:
        self._hover = v
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), int(float(self.cget("height")))
        if w < 10:
            return
        sh = px(3)
        sel = self._sel
        fill = INK if sel else CARD
        border = INK if (sel or self._hover) else LINE
        fg = "#FFFFFF" if sel else INK
        sub = ON_INK_MUTED if sel else MUTED
        r = px(14)
        round_rect(self, 0, sh, w - 1, h - 1, r, fill=("#CFCEC9" if sel else SHADOW), outline="")
        round_rect(self, 0, 0, w - 1, h - 1 - sh, r, fill=fill, outline=border, width=max(1, px(1.4)))
        compact = h < px(80)
        pad = px(11) if compact else px(14)
        # icon badge
        ib = px(30) if compact else px(34)
        round_rect(self, pad, pad, pad + ib, pad + ib, px(10), fill=("#262626" if sel else SOFT), outline="")
        draw_icon(self, self._icon, pad + ib / 2.0, pad + ib / 2.0, px(19), fg, "#262626" if sel else SOFT)
        self.create_text(pad + ib + px(11), pad + ib / 2.0, text=self._title, font=font("xbold", 15),
                         fill=fg, anchor="w")
        # multiplier pill
        bf = font("bold", 11.5)
        bw = tkfont.Font(font=bf).measure(self._badge) + px(16)
        bh = px(22)
        bx2 = w - pad
        by = pad + (ib - bh) / 2.0
        round_rect(self, bx2 - bw, by, bx2, by + bh, bh / 2.0, fill=("#FFFFFF" if sel else SOFT), outline="")
        self.create_text(bx2 - bw / 2.0, by + bh / 2.0, text=self._badge, font=bf, fill=INK)
        if self._desc and h - sh > pad + ib + px(24):  # compact tiles (v2.5.9) skip the description
            self.create_text(pad, pad + ib + px(8), text=self._desc, font=font("semi", 11.5), fill=sub,
                             anchor="nw", width=w - 2 * pad)


class CodeBoxes(tk.Canvas):
    """6 digit cells driven by the keyboard (digits, Backspace, Ctrl+V in any layout, Enter)."""

    def __init__(self, parent: tk.Misc, n: int = 6, on_submit: Optional[Callable[[], None]] = None,
                 on_change: Optional[Callable[[], None]] = None) -> None:
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0, height=px(62),
                         takefocus=1, cursor="xterm")
        self._n = n
        self._digits: List[str] = []
        self._focus = False
        self._blink = True
        self._submit = on_submit
        self._change = on_change
        self._error = False
        self.bind("<Configure>", lambda _e: self._redraw(), add="+")
        self.bind("<Button-1>", lambda _e: self.focus_set())
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self.bind("<KeyPress>", self._on_key)
        self.bind("<<Paste>>", lambda _e: self._paste())
        self.after(530, self._blink_tick)

    def get(self) -> str:
        return "".join(self._digits)

    def set_error(self, v: bool) -> None:
        self._error = v
        self._redraw()

    def clear(self) -> None:
        self._digits = []
        self._redraw()

    def _set_focus(self, v: bool) -> None:
        self._focus = v
        self._blink = True
        self._redraw()

    def _blink_tick(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if self._focus:
            self._blink = not self._blink
            self._redraw()
        self.after(530, self._blink_tick)

    def _paste(self) -> str:
        try:
            txt = self.clipboard_get()
        except Exception:
            return "break"
        for ch in txt:
            if ch.isdigit() and len(self._digits) < self._n:
                self._digits.append(ch)
        self._changed()
        return "break"

    def _changed(self) -> None:
        self._error = False
        self._blink = True
        self._redraw()
        if self._change:
            self._change()

    def _on_key(self, e: "tk.Event") -> Optional[str]:
        ctrl = bool(e.state & 0x4)
        if ctrl and (e.keycode == 86 or e.keysym.lower() in ("v", "cyrillic_em")):
            return self._paste()
        if e.keysym in ("BackSpace", "Delete"):
            if self._digits:
                self._digits.pop()
                self._changed()
            return "break"
        if e.keysym in ("Return", "KP_Enter"):
            if self._submit:
                self._submit()
            return "break"
        if e.keysym == "Escape":
            self._digits = []
            self._changed()
            return "break"
        ch = e.char or ""
        if len(ch) == 1 and ch.isdigit() and len(self._digits) < self._n:
            self._digits.append(ch)
            self._changed()
            return "break"
        return None

    def _redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), int(float(self.cget("height")))
        if w < 20:
            return
        gap = px(10)
        mid_gap = px(14)  # visual split 3 + 3
        bw = (w - px(4) - gap * (self._n - 2) - mid_gap) / float(self._n)
        bh = h - px(4)
        x = float(px(1))
        active = min(len(self._digits), self._n - 1) if self._focus else -1
        for i in range(self._n):
            filled = i < len(self._digits)
            is_act = (i == active)
            if self._error:
                border, width = RED, px(2)
            elif is_act:
                border, width = INK, px(2.2)
            elif filled:
                border, width = INK_SOFT, px(1.4)
            else:
                border, width = LINE, px(1.4)
            round_rect(self, x, px(3), x + bw, px(3) + bh, px(12), fill=SHADOW, outline="")
            round_rect(self, x, 0, x + bw, bh, px(12), fill=CARD, outline=border, width=max(1, width))
            if filled:
                self.create_text(x + bw / 2.0, bh / 2.0, text=self._digits[i], font=font("black", 28), fill=INK)
            elif is_act and self._blink:
                self.create_line(x + bw / 2.0, bh * 0.28, x + bw / 2.0, bh * 0.72, fill=INK, width=max(1, px(2)))
            x += bw + (mid_gap if i == (self._n // 2 - 1) else gap)
            if i == (self._n // 2 - 1):
                cx = x - mid_gap / 2.0
                self.create_line(cx - px(3.5), bh / 2.0, cx + px(3.5), bh / 2.0, fill=MUTED, width=max(1, px(2)),
                                 capstyle="round")
