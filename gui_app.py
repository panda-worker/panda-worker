# -*- coding: utf-8 -*-
"""
panda_worker/gui_app.py
Sleek Dark Desktop UI & Entrypoint for Panda Worker v2.0 (`PandaWorkerSetup.exe`).

Flow:
1. Pre-check PC hardware (`hw_gate.py`). If weaker than 8 GB discrete GPU -> polite dark dialog +
   zero-trace self-delete (`self_destruct.py`).
2. If first run -> 6-digit Telegram code pairing screen (`pair_with_6digit_code`).
3. Main Dashboard ->
   - Silent resumable background AI engine download progress bar (survives weak Wi-Fi & power-off).
   - Live GPU telemetry (°C, VRAM, Category) + Daily 24h Package progress bar.
   - 4 live Load Mode selector cards (`🌱 Лёгкий`, `⚡ Средний`, `🔥 Турбо`, `🌙 Умный Авто`).
   - 1-click confirmed irreversible Full Uninstall (burns server token + deletes AI engines & `.exe`).
"""
from __future__ import annotations

import queue
import sys
import threading
from typing import Any, Dict, Optional

import worker_config as wcfg
from hw_gate import check_pc_hardware_eligibility, detect_best_discrete_gpu
from self_destruct import execute_full_self_uninstall, install_windows_autostart
from worker_agent import PandaWorkerAgent, main as cli_main, pair_with_6digit_code

# Modern Dark Palette
BG_MAIN = "#090D16"
BG_CARD = "#111827"
BG_CARD_ACTIVE = "#0F2922"
BG_INPUT = "#1F2937"
BORDER_SUBTLE = "#1F2937"
ACCENT_EMERALD = "#10B981"
ACCENT_BLUE = "#3B82F6"
ACCENT_AMBER = "#F59E0B"
ACCENT_DANGER = "#EF4444"
TEXT_PRIMARY = "#F9FAFB"
TEXT_MUTED = "#9CA3AF"


def run_gui(start_minimized: bool = False) -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title(f"Panda AI GPU Helper v{wcfg.get_runtime_version()}")
    root.geometry("620x700")
    root.minsize(590, 670)
    root.configure(bg=BG_MAIN)

    ui_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    agent_ref: Dict[str, Optional[PandaWorkerAgent]] = {"agent": None}

    container = tk.Frame(root, bg=BG_MAIN)
    container.pack(fill="both", expand=True, padx=22, pady=18)

    def _clear_container() -> None:
        for child in container.winfo_children():
            child.destroy()

    # ------------------------------------------------------------------
    # Screen A: Polite Rejection & Zero-Trace Self-Delete for Weak PCs
    # ------------------------------------------------------------------
    def _show_rejection_screen(hw: Dict[str, Any]) -> None:
        _clear_container()
        tk.Label(
            container,
            text="🐼 Panda 4K AI — Проверка оборудования",
            font=("Segoe UI", 16, "bold"),
            fg=TEXT_PRIMARY,
            bg=BG_MAIN,
        ).pack(anchor="w", pady=(10, 14))

        card = tk.Frame(container, bg=BG_CARD, highlightbackground=ACCENT_AMBER, highlightthickness=1, padx=18, pady=18)
        card.pack(fill="x", pady=8)

        tk.Label(
            card,
            text="Компьютер не подходит по минимальным требованиям",
            font=("Segoe UI", 12, "bold"),
            fg=ACCENT_AMBER,
            bg=BG_CARD,
        ).pack(anchor="w", pady=(0, 10))

        tk.Label(
            card,
            text=hw.get("reason_ru", ""),
            font=("Segoe UI", 10),
            fg=TEXT_PRIMARY,
            bg=BG_CARD,
            justify="left",
            wraplength=470,
        ).pack(anchor="w")

        countdown = [25]
        btn_var = tk.StringVar(value="Понятно · Удалить установщик без следов (25с)")

        def _cleanup_now() -> None:
            execute_full_self_uninstall(burn_remote=False, exit_process=True)

        btn = tk.Button(
            container,
            textvariable=btn_var,
            command=_cleanup_now,
            font=("Segoe UI", 10, "bold"),
            bg=ACCENT_DANGER,
            fg="#FFFFFF",
            activebackground="#DC2626",
            activeforeground="#FFFFFF",
            relief="flat",
            padx=16,
            pady=11,
            cursor="hand2",
        )
        btn.pack(fill="x", pady=(22, 6))

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
        tk.Label(
            container,
            text="🐼 Panda AI — Подключение GPU",
            font=("Segoe UI", 17, "bold"),
            fg=TEXT_PRIMARY,
            bg=BG_MAIN,
        ).pack(anchor="w")
        tk.Label(
            container,
            text="Помогайте ускорять ИИ в фоне и получайте бесплатные Дневные Пакеты 4K + 60FPS",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_MAIN,
        ).pack(anchor="w", pady=(2, 14))

        hw_card = tk.Frame(container, bg=BG_CARD, highlightbackground=ACCENT_EMERALD, highlightthickness=1, padx=16, pady=12)
        hw_card.pack(fill="x", pady=(0, 14))
        tk.Label(
            hw_card,
            text="✅ Ваша видеокарта прошла проверку!",
            font=("Segoe UI", 10, "bold"),
            fg=ACCENT_EMERALD,
            bg=BG_CARD,
        ).pack(anchor="w")
        tk.Label(
            hw_card,
            text=f"{hw['gpu_name']}  •  {hw['vram_mb']} МБ  •  {hw.get('gpu_tier_label', '')}",
            font=("Segoe UI", 10),
            fg=TEXT_PRIMARY,
            bg=BG_CARD,
        ).pack(anchor="w", pady=(3, 0))

        pair_card = tk.Frame(container, bg=BG_CARD, highlightbackground=BORDER_SUBTLE, highlightthickness=1, padx=18, pady=18)
        pair_card.pack(fill="x", pady=4)

        tk.Label(
            pair_card,
            text="1. Откройте @panda_4k_bot в Telegram → «⚡ Пакет за GPU & Фарм ⭐️»\n"
                 "2. Нажмите «🔑 Получить 6-значный код» и введите его ниже:",
            font=("Segoe UI", 10),
            fg=TEXT_PRIMARY,
            bg=BG_CARD,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        code_var = tk.StringVar()
        code_entry = tk.Entry(
            pair_card,
            textvariable=code_var,
            font=("Consolas", 22, "bold"),
            justify="center",
            bg=BG_INPUT,
            fg=ACCENT_EMERALD,
            insertbackground=TEXT_PRIMARY,
            relief="flat",
        )
        code_entry.pack(fill="x", ipady=8, pady=(0, 12))
        code_entry.focus_set()

        tk.Label(
            pair_card,
            text="Адрес сервера Panda AI:",
            font=("Segoe UI", 8),
            fg=TEXT_MUTED,
            bg=BG_CARD,
        ).pack(anchor="w")
        srv_var = tk.StringVar(value=wcfg.SERVER_URL)
        srv_entry = tk.Entry(
            pair_card,
            textvariable=srv_var,
            font=("Consolas", 10),
            bg=BG_INPUT,
            fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            relief="flat",
        )
        srv_entry.pack(fill="x", ipady=4, pady=(2, 8))

        err_var = tk.StringVar(value="")
        tk.Label(
            pair_card,
            textvariable=err_var,
            font=("Segoe UI", 9),
            fg=ACCENT_DANGER,
            bg=BG_CARD,
            wraplength=460,
        ).pack(anchor="w", pady=(4, 0))

        def _do_pair() -> None:
            raw_code = code_var.get().strip()
            s_url = srv_var.get().strip()
            if len("".join(c for c in raw_code if c.isdigit())) != 6:
                err_var.set("Введите 6 цифр кода привязки (например: 482-915).")
                return
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
                    err_var.set(str(res.get("reason") or "Ошибка привязки кода."))
                    return
                install_windows_autostart()
                _start_dashboard()
            except Exception as exc:
                err_var.set(f"Сервер недоступен: проверьте интернет или адрес сервера ({type(exc).__name__}).")

        tk.Button(
            container,
            text="🚀 Подключить ПК и запустить фоновую загрузку ИИ",
            command=_do_pair,
            font=("Segoe UI", 11, "bold"),
            bg=ACCENT_EMERALD,
            fg="#04130E",
            activebackground="#059669",
            relief="flat",
            pady=12,
            cursor="hand2",
        ).pack(fill="x", pady=(14, 6))

    # ------------------------------------------------------------------
    # Screen C: Live Dark Dashboard
    # ------------------------------------------------------------------
    def _start_dashboard() -> None:
        _clear_container()
        # Mandatory autostart: re-verify & heal autostart on every launch,
        # and make clicking [X] minimize instead of terminating the worker!
        install_windows_autostart()
        root.protocol("WM_DELETE_WINDOW", root.iconify)

        st = wcfg.load_state()
        gpu_n, vram_m = detect_best_discrete_gpu()

        # Header
        hdr = tk.Frame(container, bg=BG_MAIN)
        hdr.pack(fill="x", pady=(0, 10))
        tk.Label(
            hdr,
            text=f"🐼 Panda GPU Helper v{wcfg.get_runtime_version()}",
            font=("Segoe UI", 16, "bold"),
            fg=TEXT_PRIMARY,
            bg=BG_MAIN,
        ).pack(side="left")
        temp_lbl = tk.Label(
            hdr,
            text="GPU: --°C",
            font=("Consolas", 10, "bold"),
            fg=ACCENT_EMERALD,
            bg=BG_CARD,
            padx=10,
            pady=4,
        )
        temp_lbl.pack(side="right")

        gpu_sub = tk.Label(
            container,
            text=f"{gpu_n or 'Discrete GPU'}  •  {vram_m} МБ  •  {st.get('gpu_tier_label', 'Дискретная GPU')}",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_MAIN,
        )
        gpu_sub.pack(anchor="w", pady=(0, 10))

        # Status + Progress Bar Card (shows Background AI Download first, then Daily Reward Progress!)
        prog_card = tk.Frame(container, bg=BG_CARD, highlightbackground=BORDER_SUBTLE, highlightthickness=1, padx=16, pady=14)
        prog_card.pack(fill="x", pady=(0, 12))

        banner_var = tk.StringVar(value="Подключение к серверу Panda AI...")
        tk.Label(
            prog_card,
            textvariable=banner_var,
            font=("Segoe UI", 10, "bold"),
            fg=TEXT_PRIMARY,
            bg=BG_CARD,
            anchor="w",
            justify="left",
            wraplength=475,
        ).pack(fill="x")

        bar_canvas = tk.Canvas(prog_card, height=14, bg=BG_INPUT, highlightthickness=0)
        bar_canvas.pack(fill="x", pady=(10, 6))
        bar_rect = bar_canvas.create_rectangle(0, 0, 0, 14, fill=ACCENT_EMERALD, width=0)

        reward_sub_var = tk.StringVar(
            value="Интернет можно отключать или выключать ПК — фоновая докачка продолжится с того же места."
        )
        tk.Label(
            prog_card,
            textvariable=reward_sub_var,
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_CARD,
            anchor="w",
            justify="left",
            wraplength=475,
        ).pack(fill="x")

        # 4 Interactive Load Mode Cards
        tk.Label(
            container,
            text="Выберите удобный режим нагрузки (можно менять в любой момент):",
            font=("Segoe UI", 10, "bold"),
            fg=TEXT_PRIMARY,
            bg=BG_MAIN,
        ).pack(anchor="w", pady=(4, 8))

        modes_frame = tk.Frame(container, bg=BG_MAIN)
        modes_frame.pack(fill="x")

        mode_defs = [
            ("light", "🌱 Лёгкий (×1)", "Минимум нагрузки — комфортно для браузера, фильмов и работы"),
            ("medium", "⚡ Средний (×2)", "Оптимальный баланс тишины и скорости набора пакета"),
            ("turbo", "🔥 Турбо (×3.5)", "Максимум баллов в час — быстрее открывает Топ-пакет 50Ф+12В"),
            ("auto", "🌙 Умный Авто", "Лёгкий пока вы за ПК, автоматически Турбо когда вы отошли"),
        ]
        mode_cards: Dict[str, tk.Frame] = {}

        def _select_mode(m_key: str) -> None:
            ag = agent_ref.get("agent")
            if ag:
                ag.set_user_load_mode(m_key)
            _highlight_mode(m_key)

        def _highlight_mode(active_key: str) -> None:
            for k, frm in mode_cards.items():
                is_sel = k == active_key
                bg_c = BG_CARD_ACTIVE if is_sel else BG_CARD
                bd_c = ACCENT_EMERALD if is_sel else BORDER_SUBTLE
                frm.configure(bg=bg_c, highlightbackground=bd_c, highlightthickness=2 if is_sel else 1)
                for ch in frm.winfo_children():
                    ch.configure(bg=bg_c)

        for m_key, m_title, m_desc in mode_defs:
            mc = tk.Frame(
                modes_frame,
                bg=BG_CARD,
                highlightbackground=BORDER_SUBTLE,
                highlightthickness=1,
                padx=12,
                pady=8,
                cursor="hand2",
            )
            mc.pack(fill="x", pady=3)
            t_lbl = tk.Label(mc, text=m_title, font=("Segoe UI", 10, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD)
            t_lbl.pack(anchor="w")
            d_lbl = tk.Label(mc, text=m_desc, font=("Segoe UI", 8), fg=TEXT_MUTED, bg=BG_CARD, justify="left", wraplength=535)
            d_lbl.pack(anchor="w")
            for w in (mc, t_lbl, d_lbl):
                w.bind("<Button-1>", lambda _e, mk=m_key: _select_mode(mk))
            mode_cards[m_key] = mc

        _highlight_mode(str(st.get("load_mode") or "auto"))

        # Bottom Controls: Pause | Hide to Background | 1-Click Full Uninstall
        ctrl = tk.Frame(container, bg=BG_MAIN)
        ctrl.pack(fill="x", side="bottom", pady=(12, 0))

        pause_var = tk.StringVar(value="⏸ Пауза")

        def _toggle_pause() -> None:
            ag = agent_ref.get("agent")
            if not ag:
                return
            nxt = not ag._manual_paused
            ag.set_manual_pause(nxt)
            pause_var.set("▶️ Продолжить" if nxt else "⏸ Пауза")

        tk.Button(
            ctrl,
            textvariable=pause_var,
            command=_toggle_pause,
            font=("Segoe UI", 9, "bold"),
            bg=BG_CARD,
            fg=TEXT_PRIMARY,
            relief="flat",
            padx=12,
            pady=8,
            cursor="hand2",
        ).pack(side="left")

        tk.Button(
            ctrl,
            text="🗕 Свернуть",
            command=root.iconify,
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=TEXT_MUTED,
            relief="flat",
            padx=10,
            pady=8,
            cursor="hand2",
        ).pack(side="left", padx=6)

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

        repair_btn = tk.Button(
            ctrl,
            text="🔑 Сменить код",
            command=_switch_to_repair,
            font=("Segoe UI", 9, "bold"),
            bg=BG_CARD,
            fg=ACCENT_BLUE,
            activebackground=ACCENT_BLUE,
            activeforeground="#FFFFFF",
            relief="flat",
            padx=12,
            pady=8,
            cursor="hand2",
        )
        repair_btn.pack(side="left", padx=(0, 8))

        def _confirm_permanent_uninstall() -> None:
            ans = messagebox.askyesno(
                "Полное удаление Panda Worker",
                "Удалить программу и все ИИ-движки с этого компьютера навсегда?\n\n"
                "• Будут стёрты все нейросети, настройки, автозапуск и сам .exe файл.\n"
                "• Ключ этого ПК на сервере будет безвозвратно уничтожен.\n"
                "• Чтобы вернуться в будущем, потребуется скачать установщик заново и привязать новый код с нуля.\n\n"
                "Вы уверены, что хотите полностью удалить программу?",
                icon="warning",
            )
            if not ans:
                return
            ag = agent_ref.get("agent")
            if ag:
                ag.stop()
            execute_full_self_uninstall(burn_remote=True, exit_process=True)

        tk.Button(
            ctrl,
            text="🗑 Удалить",
            command=_confirm_permanent_uninstall,
            font=("Segoe UI", 9, "bold"),
            bg="#2A1215",
            fg=ACCENT_DANGER,
            activebackground=ACCENT_DANGER,
            activeforeground="#FFFFFF",
            relief="flat",
            padx=10,
            pady=8,
            cursor="hand2",
        ).pack(side="right")

        # Start Worker Agent thread
        ag = PandaWorkerAgent(on_ui_state=lambda snap: ui_queue.put(snap))
        agent_ref["agent"] = ag
        threading.Thread(target=ag.serve_forever, name="panda-gui-agent", daemon=True).start()

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
                temp_lbl.configure(
                    text=f"GPU: {tc}°C" if tc > 0 else "GPU: OK",
                    fg=ACCENT_AMBER if tc >= 78 else ACCENT_EMERALD,
                )
                sb_txt = str(latest.get("status_banner") or "Дежурство в сети Panda AI")
                if sb_txt.startswith("⛔"):
                    repair_btn.configure(bg=ACCENT_BLUE, fg="#FFFFFF", text="🔑 Ввести новый код")
                    banner_var.set("⛔ Связь сброшена — нажмите «🔑 Ввести новый код» внизу")
                elif latest.get("paused"):
                    banner_var.set(f"⏸ {latest.get('pause_reason')}")
                else:
                    banner_var.set(sb_txt)

                _highlight_mode(str(latest.get("load_mode") or "auto"))
                w_px = max(10, bar_canvas.winfo_width())
                if not latest.get("engines_ready"):
                    pct = max(2, min(100, int(latest.get("dl_progress_pct") or 0)))
                    bar_canvas.coords(bar_rect, 0, 0, int(w_px * pct / 100.0), 14)
                    bar_canvas.itemconfig(bar_rect, fill=ACCENT_BLUE)
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
                    bar_canvas.coords(bar_rect, 0, 0, int(w_px * pct_pts / 100.0), 14)
                    bar_canvas.itemconfig(bar_rect, fill=ACCENT_EMERALD)
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
            root.iconify()

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

    root.mainloop()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        import json as _json
        hw = check_pc_hardware_eligibility(wcfg.WORKER_ROOT)
        out_f = wcfg.WORKER_ROOT / "selftest_ok.json"
        out_f.write_text(
            _json.dumps(
                {
                    "ok": True,
                    "frozen": wcfg.IS_FROZEN,
                    "version": wcfg.get_runtime_version(),
                    "server_url": wcfg.SERVER_URL,
                    "has_secret_leak": bool(wcfg.WORKER_TOKEN),
                    "hw": hw,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        sys.exit(0)
    elif "--cli" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--cli"]
        cli_main()
    else:
        _mutex_handle = None
        if sys.platform == "win32":
            try:
                import ctypes
                _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                _mutex_handle = _k32.CreateMutexW(None, False, "Local\\PandaWorkerSingleton_v2")
                if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                    sys.exit(0)
            except Exception:
                pass
        run_gui(start_minimized=("--minimized" in sys.argv))
