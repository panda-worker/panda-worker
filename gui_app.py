# -*- coding: utf-8 -*-
"""
panda_worker/gui_app.py
Desktop UI & entrypoint for Panda Worker (`PandaWorkerSetup.exe`), v2.5.1 "PANDA production" look.

Flow (unchanged since v2.0):
1. Pre-check PC hardware (`hw_gate.py`). If weaker than 8 GB discrete GPU -> polite dialog +
   zero-trace self-delete (`self_destruct.py`).
2. If first run -> 6-digit Telegram code pairing screen (`pair_with_6digit_code`).
3. Main dashboard ->
   - Silent resumable background AI engine download progress bar (survives weak Wi-Fi & power-off).
   - Live GPU telemetry (°C, VRAM, category) + daily 24h package progress bar.
   - 4 live load-mode cards (Лёгкий / Средний / Турбо / Умный Авто).
   - Pause, minimize, change pairing code, 1-click confirmed irreversible full uninstall.

v2.5.1 UI: black-and-white "paint & film" design (panda_theme.py), HiDPI-crisp, own app icon
(no Python icon in the taskbar), child processes never flash console windows. No personal data
(user/PC names, paths) is shown; the server address is tucked under «Дополнительно».
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import worker_config as wcfg
from hw_gate import check_pc_hardware_eligibility, detect_best_discrete_gpu
from self_destruct import execute_full_self_uninstall, install_windows_autostart
from worker_agent import PandaWorkerAgent, main as cli_main, pair_with_6digit_code

BASE_W, BASE_H = 640, 730  # logical window size (scaled by DPI)
_MUTEX_NAME = "Local\\PandaWorkerSingleton_v2"


def _ui_asset(name: str) -> Path:
    """UI files (fonts, icon) may arrive with a signed hot-update: core_current first, then the exe."""
    for base in (wcfg.HOT_CORE_DIR, wcfg.BUNDLE_DIR):
        cand = base / name
        if cand.exists():
            return cand
    return wcfg.BUNDLE_DIR / name


def _stable_font_dir() -> Path:
    """v2.5.5: Windows держит файлы шрифтов (AddFontResourceEx) открытыми, пока программа работает.
    Если подключать их прямо из core_current, следующее автообновление не может заменить эту
    папку (WinError 5). Поэтому шрифты подключаются из копии в ui_fonts_cache/<хеш>."""
    src = _ui_asset("ui_fonts")
    try:
        import hashlib
        import shutil

        files = sorted(p for p in src.iterdir() if p.is_file())
        if not files:
            return src
        h = hashlib.sha256()
        for p in files:
            h.update(p.name.encode("utf-8"))
            h.update(p.read_bytes())
        cache_root = wcfg.WORKER_ROOT / "ui_fonts_cache"
        dst = cache_root / h.hexdigest()[:16]
        if not all((dst / p.name).is_file() and (dst / p.name).stat().st_size == p.stat().st_size for p in files):
            tmp = cache_root / (dst.name + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            for p in files:
                shutil.copy2(p, tmp / p.name)
            shutil.rmtree(dst, ignore_errors=True)
            tmp.replace(dst)
        for old in cache_root.iterdir():  # старые копии (занятые этим же процессом — пропустятся)
            if old != dst:
                shutil.rmtree(old, ignore_errors=True)
        return dst
    except Exception:
        return src


def _maybe_run_hot_entry() -> None:
    """v2.5.2: the exe always starts its BUNDLED gui_app. If a newer signed hot-update put its own
    gui_app.py into core_current, hand control to it — so window/entry fixes also arrive without a
    reinstall. A broken hot entry is rolled back automatically and the process restarts clean."""
    if getattr(sys, "_panda_hot_entry", False) or "--no-hot" in sys.argv:
        return
    try:
        hot = wcfg.HOT_CORE_DIR / "gui_app.py"
        if not hot.is_file() or Path(__file__).resolve() == hot.resolve():
            return
        if wcfg._ver_tuple(wcfg.get_runtime_version()) <= wcfg._ver_tuple(wcfg.BASE_WORKER_VERSION):
            return  # hot core is not newer than this exe (stale cores are purged by worker_config)
    except Exception:
        return
    sys._panda_hot_entry = True  # type: ignore[attr-defined]
    import runpy

    try:
        runpy.run_path(str(hot), run_name="__main__")
    except SystemExit:
        raise
    except BaseException:
        import logging
        logging.getLogger("panda_worker").exception("Hot-update entry failed — rolling back")
        try:
            from hot_updater import rollback_last_hot_update, relaunch_self_silently
            if not rollback_last_hot_update():
                import shutil
                shutil.rmtree(wcfg.HOT_CORE_DIR, ignore_errors=True)
            relaunch_self_silently()  # restarts with a clean module cache (os._exit)
        except Exception:
            pass
        sys.exit(1)
    sys.exit(0)


def _acquire_single_instance() -> Any:
    """Windows named mutex. After a hot-update relaunch (--after-update) wait up to 45 s for the
    previous process to exit instead of quitting silently (old race: worker vanished after update)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        wait_until = time.time() + (45.0 if "--after-update" in sys.argv else 0.0)
        while True:
            ctypes.set_last_error(0)
            handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
            err = ctypes.get_last_error()
            if err != 183:  # not ERROR_ALREADY_EXISTS -> we own the singleton
                return handle
            if handle:
                k32.CloseHandle(handle)
            if time.time() >= wait_until:
                if "--after-update" not in sys.argv and "--minimized" not in sys.argv:
                    # v2.5.3: повторный запуск .exe (окно спрятано в трей) -> показать уже работающее окно
                    try:
                        import panda_tray
                        panda_tray.signal_running_instance_show()
                    except Exception:
                        pass
                sys.exit(0)
            time.sleep(0.5)
    except SystemExit:
        raise
    except Exception:
        return None


def _hide_child_consoles() -> None:
    """Windowed exe: make sure no child process (ffmpeg, nvidia-smi, ...) ever flashes a console."""
    if sys.platform != "win32":
        return
    try:
        import subprocess

        if getattr(subprocess.Popen, "_panda_no_window", False):
            return
        _orig_init = subprocess.Popen.__init__

        def _init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if len(args) < 13 and not kwargs.get("creationflags"):
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            _orig_init(self, *args, **kwargs)

        subprocess.Popen.__init__ = _init  # type: ignore[method-assign]
        subprocess.Popen._panda_no_window = True  # type: ignore[attr-defined]
    except Exception:
        pass


def run_gui(start_minimized: bool = False) -> None:
    import tkinter as tk
    from tkinter import messagebox

    import panda_theme as T

    _hide_child_consoles()
    T.prepare_process()
    T.register_fonts(_stable_font_dir())

    root = tk.Tk()
    root.withdraw()
    T.init(root, BASE_W, BASE_H)
    px, F = T.px, T.font

    root.title(f"PANDA · GPU Helper v{wcfg.get_runtime_version()}")
    T.apply_window_icon(root, _ui_asset("panda_icon.ico"))
    root.configure(bg=T.PAPER)
    W, H = px(BASE_W), px(BASE_H)
    try:
        sx = max(0, (root.winfo_screenwidth() - W) // 2)
        sy = max(0, (root.winfo_screenheight() - H) // 2 - px(20))
        root.geometry(f"{W}x{H}+{sx}+{sy}")
    except Exception:
        root.geometry(f"{W}x{H}")
    root.minsize(W, px(680))
    root.resizable(False, True)

    ui_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    agent_ref: Dict[str, Optional[PandaWorkerAgent]] = {"agent": None}

    # Persistent artwork header (paint drips · PANDA production · panda with roller · film)
    header_img = T.header_photo()
    header = tk.Label(root, image=header_img, bg=T.PAPER, bd=0, highlightthickness=0)
    header.image = header_img  # keep a reference
    header.pack(side="top", fill="x")

    container = tk.Frame(root, bg=T.PAPER)
    container.pack(fill="both", expand=True, padx=px(24), pady=(px(12), px(18)))
    CONTENT_W = W - 2 * px(24)

    # ------------------------------------------------------------------
    # v2.5.3: background mode — [X] hides the window to the tray, PANDA keeps working
    # ------------------------------------------------------------------
    tray_ref: Dict[str, Any] = {"tray": None, "hint_shown": False, "actions": {}, "tip": ""}

    def _show_window() -> None:
        try:
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.after(300, lambda: root.attributes("-topmost", False))
            root.focus_force()
        except Exception:
            pass
        sys._panda_window_hidden = False  # type: ignore[attr-defined]

    def _hide_to_background(show_hint: bool = True) -> None:
        tr = tray_ref["tray"]
        if tr is None or not tr.running:
            root.iconify()  # no tray available -> old behaviour (taskbar)
            return
        root.withdraw()
        sys._panda_window_hidden = True  # type: ignore[attr-defined]
        if show_hint and not tray_ref["hint_shown"]:
            tray_ref["hint_shown"] = True
            tr.notify("PANDA работает в фоне",
                      "Значок панды — возле часов (если не видно, нажмите «^»). "
                      "Нажмите на него, чтобы снова открыть окно.")

    def _drop_tray() -> None:
        try:
            import panda_tray
            panda_tray.remove_all()
        except Exception:
            pass

    def _ensure_tray() -> None:
        if tray_ref["tray"] is not None:
            return
        try:
            import panda_tray
        except Exception:
            return
        tr = panda_tray.make_tray(f"PANDA · GPU Helper v{wcfg.get_runtime_version()}", _ui_asset("panda_icon.ico"))
        if tr is None:
            return
        tray_ref["tray"] = tr

        def _poll_tray() -> None:
            try:
                while True:
                    ev = tr.events.get_nowait()
                    if ev == panda_tray.EV_OPEN:
                        _show_window()
                    else:
                        fn = tray_ref["actions"].get(ev)
                        if fn:
                            fn()
            except queue.Empty:
                pass
            except Exception:
                pass
            try:
                root.after(150, _poll_tray)
            except Exception:
                pass

        root.after(150, _poll_tray)

    def _set_tray_tip(text: str) -> None:
        tr = tray_ref["tray"]
        if tr is not None and text != tray_ref["tip"]:
            tray_ref["tip"] = text
            tr.set_tooltip(text)

    def _clear_container() -> None:
        for child in container.winfo_children():
            child.destroy()

    def _title(parent: tk.Misc, text: str, sub: str = "") -> None:
        tk.Label(parent, text=text, font=F("xbold", 21), fg=T.INK, bg=T.PAPER, anchor="w").pack(fill="x")
        if sub:
            tk.Label(parent, text=sub, font=F("semi", 12.5), fg=T.MUTED, bg=T.PAPER, anchor="w",
                     justify="left", wraplength=CONTENT_W).pack(fill="x", pady=(px(1), 0))

    # ------------------------------------------------------------------
    # Screen A: Polite Rejection & Zero-Trace Self-Delete for Weak PCs
    # ------------------------------------------------------------------
    def _show_rejection_screen(hw: Dict[str, Any]) -> None:
        _clear_container()
        _title(container, "Проверка оборудования", "Panda 4K AI · минимальные требования")

        card = T.RoundCard(container, pad=18, border="#EBCB8F")
        card.pack(fill="x", pady=(px(14), px(4)))
        row = tk.Frame(card.inner, bg=T.CARD)
        row.pack(fill="x")
        try:
            masc = T.mascot_photo(58)
            ml = tk.Label(row, image=masc, bg=T.CARD, bd=0)
            ml.image = masc
            ml.pack(side="left", anchor="n", padx=(0, px(14)))
        except Exception:
            pass
        col = tk.Frame(row, bg=T.CARD)
        col.pack(side="left", fill="x", expand=True)
        tk.Label(col, text="Компьютер не подходит по минимальным требованиям", font=F("xbold", 15),
                 fg=T.AMBER, bg=T.CARD, anchor="w", justify="left",
                 wraplength=CONTENT_W - px(120)).pack(fill="x")
        tk.Label(col, text=hw.get("reason_ru", ""), font=F("semi", 13), fg=T.INK, bg=T.CARD, anchor="w",
                 justify="left", wraplength=CONTENT_W - px(120)).pack(fill="x", pady=(px(6), 0))

        countdown = [25]
        btn_var = tk.StringVar(value="Понятно · Удалить установщик без следов (25с)")

        def _cleanup_now() -> None:
            _drop_tray()
            # FIX-2026-11-19: если устройство уже было привязано — токен гасится и на сервере.
            execute_full_self_uninstall(burn_remote=True, exit_process=True)

        btn = T.PillButton(container, textvariable=btn_var, command=_cleanup_now, kind="danger_solid",
                           icon="trash", height=50, size=14)
        btn.pack(fill="x", pady=(px(22), px(8)))
        tk.Label(container, text="Установщик сотрёт себя и все временные файлы — на компьютере ничего не останется.",
                 font=F("semi", 11.5), fg=T.MUTED, bg=T.PAPER, justify="center",
                 wraplength=CONTENT_W).pack(fill="x")

        def _tick() -> None:
            countdown[0] -= 1
            if countdown[0] <= 0:
                _cleanup_now()
                return
            btn_var.set(f"Понятно · Удалить установщик без следов ({countdown[0]}с)")
            root.after(1000, _tick)

        root.protocol("WM_DELETE_WINDOW", _cleanup_now)
        root.after(1000, _tick)

    # ------------------------------------------------------------------
    # Screen B: 6-Digit Pairing Screen
    # ------------------------------------------------------------------
    def _show_pairing_screen(hw: Dict[str, Any]) -> None:
        _clear_container()
        _title(container, "Подключение видеокарты",
               "Помогайте ускорять ИИ в фоне и получайте бесплатные Дневные Пакеты 4K + 60FPS")

        # GPU check passed
        hw_card = T.RoundCard(container, pad=14, border="#BFE3CD")
        hw_card.pack(fill="x", pady=(px(12), px(10)))
        hrow = tk.Frame(hw_card.inner, bg=T.CARD)
        hrow.pack(fill="x")
        ok_cv = tk.Canvas(hrow, width=px(34), height=px(34), bg=T.CARD, highlightthickness=0, bd=0)
        ok_cv.create_oval(1, 1, px(33), px(33), fill=T.GREEN, outline="")
        T.draw_icon(ok_cv, "check", px(17), px(17), px(18), "#FFFFFF", T.GREEN)
        ok_cv.pack(side="left", padx=(0, px(12)))
        hcol = tk.Frame(hrow, bg=T.CARD)
        hcol.pack(side="left", fill="x", expand=True)
        tk.Label(hcol, text="Видеокарта прошла проверку", font=F("xbold", 14), fg=T.INK, bg=T.CARD,
                 anchor="w").pack(fill="x")
        tk.Label(hcol, text=f"{hw['gpu_name']}  •  {hw['vram_mb']} МБ  •  {hw.get('gpu_tier_label', '')}",
                 font=F("semi", 12), fg=T.MUTED, bg=T.CARD, anchor="w").pack(fill="x")

        # Steps + code
        pair_card = T.RoundCard(container, pad=18)
        pair_card.pack(fill="x", pady=(0, px(4)))
        pin = pair_card.inner

        def _step(num: str, text: str) -> None:
            r = tk.Frame(pin, bg=T.CARD)
            r.pack(fill="x", pady=(0, px(8)))
            cv = tk.Canvas(r, width=px(24), height=px(24), bg=T.CARD, highlightthickness=0, bd=0)
            cv.create_oval(1, 1, px(23), px(23), fill=T.INK, outline="")
            cv.create_text(px(12), px(12.5), text=num, font=F("black", 12), fill="#FFFFFF")
            cv.pack(side="left", anchor="n", padx=(0, px(10)))
            tk.Label(r, text=text, font=F("semi", 13), fg=T.INK, bg=T.CARD, anchor="w", justify="left",
                     wraplength=CONTENT_W - px(80)).pack(side="left", fill="x", expand=True)

        _step("1", "Откройте @panda_4k_bot в Telegram → «⚡ Пакет за GPU & Фарм ⭐️»")
        _step("2", "Нажмите «🔑 Получить 6-значный код» и введите его ниже:")

        err_var = tk.StringVar(value="")
        code_box: Dict[str, Any] = {}

        def _on_code_change() -> None:
            if err_var.get():
                err_var.set("")

        code = T.CodeBoxes(pin, on_submit=lambda: _do_pair(), on_change=_on_code_change)
        code.pack(fill="x", pady=(px(6), px(8)))
        code_box["w"] = code
        root.after(150, code.focus_set)

        err_lbl = tk.Label(pin, textvariable=err_var, font=F("bold", 12), fg=T.RED, bg=T.CARD, anchor="w",
                           justify="left", wraplength=CONTENT_W - px(40))
        err_lbl.pack(fill="x")

        # Advanced: server address (hidden by default — nothing technical/personal on screen)
        adv = tk.Frame(pin, bg=T.CARD)
        srv_var = tk.StringVar(value=wcfg.SERVER_URL)
        adv_open = [False]
        adv_lbl_var = tk.StringVar(value="Дополнительно  ▸")
        adv_link = tk.Label(pin, textvariable=adv_lbl_var, font=F("bold", 11.5), fg=T.MUTED, bg=T.CARD,
                            cursor="hand2", anchor="w")
        adv_link.pack(fill="x", pady=(px(2), 0))
        tk.Label(adv, text="Адрес сервера Panda AI:", font=F("semi", 11), fg=T.MUTED, bg=T.CARD,
                 anchor="w").pack(fill="x", pady=(px(6), px(3)))
        srv_entry = tk.Entry(adv, textvariable=srv_var, font=("Consolas", -px(13)), bg=T.PAPER, fg=T.INK,
                             insertbackground=T.INK, relief="flat", highlightthickness=max(1, px(1)),
                             highlightbackground=T.LINE, highlightcolor=T.INK)
        srv_entry.pack(fill="x", ipady=px(6))

        def _toggle_adv(_e: Any = None) -> None:
            adv_open[0] = not adv_open[0]
            if adv_open[0]:
                adv.pack(fill="x")
                adv_lbl_var.set("Дополнительно  ▾")
            else:
                adv.pack_forget()
                adv_lbl_var.set("Дополнительно  ▸")

        adv_link.bind("<Button-1>", _toggle_adv)
        adv_link.bind("<Enter>", lambda _e: adv_link.configure(fg=T.INK))
        adv_link.bind("<Leave>", lambda _e: adv_link.configure(fg=T.MUTED))

        def _do_pair() -> None:
            raw_code = code.get().strip()
            s_url = srv_var.get().strip()
            if len("".join(c for c in raw_code if c.isdigit())) != 6:
                err_lbl.configure(fg=T.RED)
                code.set_error(True)
                err_var.set("Введите 6 цифр кода привязки (например: 482-915).")
                return
            err_lbl.configure(fg=T.MUTED)
            err_var.set("Подключение к серверу Panda AI...")
            root.update_idletasks()
            try:
                res = pair_with_6digit_code(
                    server_url=s_url,
                    pair_code=raw_code,
                    gpu_name=hw["gpu_name"],
                    vram_mb=hw["vram_mb"],
                )
                if not res.get("ok"):
                    if res.get("self_destruct"):
                        _show_rejection_screen({"reason_ru": res.get("reason", "Оборудование не подходит.")})
                        return
                    err_lbl.configure(fg=T.RED)
                    code.set_error(True)
                    err_var.set(str(res.get("reason") or "Ошибка привязки кода."))
                    return
                install_windows_autostart()
                _start_dashboard()
            except Exception as exc:
                err_lbl.configure(fg=T.RED)
                err_var.set(f"Сервер недоступен: проверьте интернет или адрес сервера ({type(exc).__name__}).")

        T.PillButton(container, text="Подключить ПК и запустить загрузку ИИ", command=_do_pair,
                     kind="primary", icon="arrow", height=52, size=14.5).pack(fill="x", pady=(px(14), px(4)))

    # ------------------------------------------------------------------
    # Screen C: Live Dashboard
    # ------------------------------------------------------------------
    def _start_dashboard() -> None:
        _clear_container()
        # Mandatory autostart: re-verify & heal autostart on every launch,
        # and make clicking [X] hide the window to the tray: the worker keeps running (v2.5.3).
        install_windows_autostart()
        _ensure_tray()
        root.protocol("WM_DELETE_WINDOW", _hide_to_background)

        st = wcfg.load_state()
        gpu_n, vram_m = detect_best_discrete_gpu()

        # Header row: title + GPU line | temperature chip
        hdr = tk.Frame(container, bg=T.PAPER)
        hdr.pack(fill="x", pady=(0, px(10)))
        left = tk.Frame(hdr, bg=T.PAPER)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="Панель воркера", font=F("xbold", 21), fg=T.INK, bg=T.PAPER,
                 anchor="w").pack(fill="x")
        gpu_sub = tk.Label(
            left,
            text=f"{gpu_n or 'Discrete GPU'}  •  {vram_m} МБ  •  {st.get('gpu_tier_label', 'Дискретная GPU')}",
            font=F("semi", 12), fg=T.MUTED, bg=T.PAPER, anchor="w",
        )
        gpu_sub.pack(fill="x")
        temp_lbl = T.Chip(hdr, text="GPU: --°C", color=T.GREEN, bg=T.PAPER)
        temp_lbl.pack(side="right", anchor="n", pady=(px(4), 0))

        # Status + progress card (background AI download first, then daily reward progress)
        prog_card = T.RoundCard(container, pad=16)
        prog_card.pack(fill="x", pady=(0, px(12)))
        pc = prog_card.inner
        banner_var = tk.StringVar(value="Подключение к серверу Panda AI...")
        tk.Label(pc, textvariable=banner_var, font=F("xbold", 14), fg=T.INK, bg=T.CARD, anchor="w",
                 justify="left", wraplength=CONTENT_W - px(40)).pack(fill="x")
        bar = T.PaintBar(pc, height=12, bg=T.CARD)
        bar.pack(fill="x", pady=(px(10), px(8)))
        bar.set(2, T.INK, True)
        reward_sub_var = tk.StringVar(
            value="Интернет можно отключать или выключать ПК — фоновая докачка продолжится с того же места."
        )
        tk.Label(pc, textvariable=reward_sub_var, font=F("semi", 11.5), fg=T.MUTED, bg=T.CARD, anchor="w",
                 justify="left", wraplength=CONTENT_W - px(40)).pack(fill="x")

        # Load mode tiles (2 x 2)
        lrow = tk.Frame(container, bg=T.PAPER)
        lrow.pack(fill="x", pady=(px(2), px(8)))
        tk.Label(lrow, text="Режим нагрузки", font=F("xbold", 15), fg=T.INK, bg=T.PAPER).pack(side="left")
        tk.Label(lrow, text="можно менять в любой момент", font=F("semi", 11.5), fg=T.MUTED,
                 bg=T.PAPER).pack(side="left", padx=(px(8), 0), pady=(px(2), 0))

        modes_frame = tk.Frame(container, bg=T.PAPER)
        modes_frame.pack(fill="x")
        modes_frame.grid_columnconfigure(0, weight=1, uniform="m")
        modes_frame.grid_columnconfigure(1, weight=1, uniform="m")

        mode_defs = [
            ("light", "Лёгкий", "×1", "Минимум нагрузки — комфортно для браузера, фильмов и работы", "leaf"),
            ("medium", "Средний", "×2", "Оптимальный баланс тишины и скорости набора пакета", "bolt"),
            ("turbo", "Турбо", "×3.5", "Максимум баллов в час — быстрее открывает Топ-пакет 50Ф+12В", "flame"),
            ("auto", "Умный Авто", "АВТО", "Лёгкий пока вы за ПК, Турбо когда отошли. В играх — пауза", "moon"),
        ]
        mode_cards: Dict[str, Any] = {}

        def _select_mode(m_key: str) -> None:
            ag = agent_ref.get("agent")
            if ag:
                ag.set_user_load_mode(m_key)
            _highlight_mode(m_key)

        def _highlight_mode(active_key: str) -> None:
            for k, tile in mode_cards.items():
                tile.set_selected(k == active_key)

        for i, (m_key, m_title, m_badge, m_desc, m_icon) in enumerate(mode_defs):
            tile = T.ModeTile(modes_frame, m_key, m_title, m_badge, m_desc, m_icon, _select_mode, height=100)
            tile.grid(row=i // 2, column=i % 2, sticky="ew",
                      padx=(0, px(6)) if i % 2 == 0 else (px(6), 0), pady=(0, px(10)))
            mode_cards[m_key] = tile

        _highlight_mode(str(st.get("load_mode") or "auto"))

        # Bottom controls: Pause | Minimize | Change code ........ Uninstall
        ctrl = tk.Frame(container, bg=T.PAPER)
        ctrl.pack(fill="x", side="bottom", pady=(px(8), 0))

        pause_var = tk.StringVar(value="Пауза")
        pause_btn: Dict[str, Any] = {}

        def _toggle_pause() -> None:
            ag = agent_ref.get("agent")
            if not ag:
                return
            nxt = not ag._manual_paused
            ag.set_manual_pause(nxt)
            if tray_ref["tray"] is not None:
                tray_ref["tray"].set_paused(nxt)
            pause_var.set("Продолжить" if nxt else "Пауза")
            pause_btn["w"].configure_style(kind="primary" if nxt else "secondary", icon="play" if nxt else "pause")

        pause_btn["w"] = T.PillButton(ctrl, textvariable=pause_var, command=_toggle_pause, kind="secondary",
                                      icon="pause", height=42, size=13, bg=T.PAPER)
        pause_btn["w"].pack(side="left")

        T.PillButton(ctrl, text="Свернуть", command=_hide_to_background, kind="ghost", icon="min", height=42,
                     size=13, bg=T.PAPER).pack(side="left", padx=(px(6), px(6)))

        def _switch_to_repair() -> None:
            ag_cur = agent_ref.get("agent")
            if ag_cur:
                ag_cur.stop()
                agent_ref["agent"] = None
            st_cur = wcfg.load_state()
            st_cur["worker_token"] = ""
            wcfg.save_state(st_cur)
            hw_info = check_pc_hardware_eligibility(wcfg.WORKER_ROOT)
            _show_pairing_screen(hw_info)

        repair_btn = T.PillButton(ctrl, text="Сменить код", command=_switch_to_repair, kind="secondary",
                                  icon="key", height=42, size=13, bg=T.PAPER)
        repair_btn.pack(side="left")

        def _confirm_permanent_uninstall() -> None:
            ans = messagebox.askyesno(
                "Полное удаление Panda Worker",
                "Удалить программу и все ИИ-движки с этого компьютера навсегда?\n\n"
                "• Будут стёрты все нейросети, настройки, автозапуск и сам .exe файл.\n"
                "• Ключ этого ПК на сервере будет безвозвратно уничтожен.\n"
                "• Чтобы вернуться в будущем, потребуется скачать установщик заново и привязать новый код с нуля.\n\n"
                "Вы уверены, что хотите полностью удалить программу?",
                icon="warning",
                parent=root,
            )
            if not ans:
                return
            ag = agent_ref.get("agent")
            if ag:
                ag.stop()
            _drop_tray()
            execute_full_self_uninstall(burn_remote=True, exit_process=True)

        def _exit_from_tray() -> None:
            _show_window()
            ans = messagebox.askyesno(
                "Выход из PANDA",
                "Закрыть программу полностью?\n\n"
                "• Пока PANDA выключена, задания не выполняются и баллы не начисляются.\n"
                "• Программа снова запустится сама при следующем входе в Windows.\n\n"
                "Чтобы просто убрать окно — нажмите крестик: PANDA продолжит работать в фоне.",
                parent=root,
            )
            if not ans:
                return
            ag_x = agent_ref.get("agent")
            if ag_x:
                try:
                    ag_x.stop()
                except Exception:
                    pass
            _drop_tray()
            root.destroy()

        tray_ref["actions"] = {"toggle_pause": _toggle_pause, "exit": _exit_from_tray}

        T.PillButton(ctrl, text="Удалить", command=_confirm_permanent_uninstall, kind="danger", icon="trash",
                     height=42, size=13, bg=T.PAPER).pack(side="right")

        # Start Worker Agent thread
        ag = PandaWorkerAgent(on_ui_state=lambda snap: ui_queue.put(snap))
        agent_ref["agent"] = ag
        threading.Thread(target=ag.serve_forever, name="panda-gui-agent", daemon=True).start()

        reset_state = {"shown": False}

        def _poll_ui_queue() -> None:
            if not temp_lbl.winfo_exists():
                return
            latest: Optional[Dict[str, Any]] = None
            while not ui_queue.empty():
                try:
                    latest = ui_queue.get_nowait()
                except queue.Empty:
                    break
            if latest:
                tc = int(latest.get("temp_c") or 0)
                temp_lbl.set(
                    f"GPU: {tc}°C" if tc > 0 else "GPU: OK",
                    T.AMBER if tc >= 78 else T.GREEN,
                )
                sb_txt = str(latest.get("status_banner") or "Дежурство в сети Panda AI")
                if sb_txt.startswith("⛔"):
                    if not reset_state["shown"]:
                        reset_state["shown"] = True
                        repair_btn.configure_style(kind="primary", text="Ввести новый код")
                        prog_card.set_border(T.RED)
                    banner_var.set("⛔ Связь сброшена — нажмите «Ввести новый код» внизу")
                elif latest.get("paused"):
                    banner_var.set(f"⏸ {latest.get('pause_reason')}")
                else:
                    banner_var.set(sb_txt)

                if latest.get("paused"):
                    _set_tray_tip(f"PANDA — пауза\n{latest.get('pause_reason') or ''}"[:127])
                else:
                    _set_tray_tip((f"PANDA — работает в фоне\nGPU {tc}°C" if tc > 0
                                   else "PANDA — работает в фоне")[:127])
                _highlight_mode(str(latest.get("load_mode") or "auto"))
                if not latest.get("engines_ready"):
                    pct = max(2, min(100, int(latest.get("dl_progress_pct") or 0)))
                    bar.set(pct, T.INK, True)
                else:
                    vol = latest.get("volunteer") or {}
                    pts = float(vol.get("points_today") or 0.0)
                    next_goal = max(35.0, float(vol.get("next_tier_points") or 250.0))
                    tier_n = int(vol.get("reward_tier_today") or 0)
                    duty_m = int(vol.get("duty_minutes_today") or 0)
                    if pts >= 250.0:
                        step_prog = (pts - 250.0) % 50.0
                        pct_pts = max(8, min(100, int(round(100.0 * step_prog / 50.0))))
                    else:
                        pct_pts = max(4, min(100, int(round(100.0 * pts / 250.0))))
                    bar.set(pct_pts, T.GREEN, False)
                    tier_txt = {
                        0: "До 1-го пакета (10Ф+2В): 35 баллов",
                        1: "🥉 Пакет 10Ф+2В! Следующий: 110 б. (25Ф+6В)",
                        2: "🥈 Пакет 25Ф+6В! Следующий: 250 б. (50Ф+12В)",
                        3: f"🔥 Сверхфарм 24/7 на Склад Биржи ⭐️ (след. бонус: {next_goal:.0f} б.)",
                    }.get(tier_n, "")
                    reward_sub_var.set(f"Баллы: {pts:.1f} / {next_goal:.0f}  •  Дежурство: {duty_m} мин  •  {tier_txt}")

            root.after(500, _poll_ui_queue)

        root.after(400, _poll_ui_queue)
        if start_minimized:
            _hide_to_background(show_hint=False)

    # Entry routing
    st_now = wcfg.load_state()
    has_token = bool((st_now.get("worker_token") or wcfg.WORKER_TOKEN).strip())
    if has_token:
        _start_dashboard()
    else:
        hw_res = check_pc_hardware_eligibility(wcfg.WORKER_ROOT)
        if not hw_res["eligible"]:
            _show_rejection_screen(hw_res)
        else:
            _show_pairing_screen(hw_res)

    if start_minimized and has_token:
        tr0 = tray_ref["tray"]
        if tr0 is None or not tr0.running:
            root.deiconify()
            root.iconify()
    else:
        root.deiconify()
        sys._panda_window_hidden = False  # type: ignore[attr-defined]
    try:
        root.mainloop()
    finally:
        _drop_tray()


if __name__ == "__main__":
    _maybe_run_hot_entry()
    if "--self-test" in sys.argv:
        import json as _json
        hw = check_pc_hardware_eligibility(wcfg.WORKER_ROOT)
        # FIX-2026-11-19: self-test проверяет то, без чего v2.5.0 не работает:
        # https-адрес, вшитый сертификат сервера, ключ подписи и модуль cryptography.
        problems = []
        if not wcfg.SERVER_URL.startswith("https://"):
            problems.append("server_url_not_https")
        if not wcfg.SERVER_CA_FILE.is_file():
            problems.append("server_ca_missing")
        if len(wcfg.RELEASE_PUBKEY_HEX) != 64:
            problems.append("release_pubkey_missing")
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(wcfg.RELEASE_PUBKEY_HEX))
        except Exception as _exc:  # noqa: BLE001
            problems.append(f"cryptography:{type(_exc).__name__}")
        try:
            import release_verify  # noqa: F401
            import worker_agent  # noqa: F401
            import hot_updater  # noqa: F401
            import panda_theme  # noqa: F401  (v2.5.1 UI)
            import panda_tray  # noqa: F401  (v2.5.3 background mode)
            import panda_theme_assets as _pta
            import base64 as _b64
            if not _b64.b64decode(_pta.HEADER_PNG_B64).startswith(b"\x89PNG"):
                problems.append("ui_assets_corrupt")
            if not (wcfg.BUNDLE_DIR / "panda_icon.ico").is_file():
                problems.append("ui_icon_missing")
        except Exception as _exc:  # noqa: BLE001
            problems.append(f"import:{type(_exc).__name__}")
        ok = not problems
        out_f = wcfg.WORKER_ROOT / "selftest_ok.json"
        out_f.write_text(
            _json.dumps(
                {
                    "ok": ok,
                    "problems": problems,
                    "frozen": wcfg.IS_FROZEN,
                    "version": wcfg.get_runtime_version(),
                    "server_url": wcfg.SERVER_URL,
                    "server_ca": str(wcfg.SERVER_CA_FILE),
                    "has_secret_leak": bool(wcfg.WORKER_TOKEN),
                    "entry": str(Path(__file__).resolve()),
                    "hot_entry": bool(getattr(sys, "_panda_hot_entry", False)),
                    "hw": hw,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        sys.exit(0 if ok else 3)
    elif "--cli" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--cli"]
        cli_main()
    else:
        _mutex_handle = _acquire_single_instance()
        run_gui(start_minimized=("--minimized" in sys.argv))
