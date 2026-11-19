# -*- coding: utf-8 -*-
"""
panda_worker/build_hot_update.py — сборка подписываемого пакета автообновления (v2.5.2).

Пакет `worker_hot_update_X.Y.Z.zip` ставится воркером БЕЗ переустановки .exe:
он скачивается с GitHub, проверяется (sha256 + Ed25519 по SIGNATURES.json) и распаковывается
в %LOCALAPPDATA%\\PandaWorker\\core_current. Начиная с exe 2.5.2 из пакета берётся и окно
(gui_app.py), и шрифты/иконка интерфейса.

Что НЕ обновляется пакетом: worker_config.py (вшит в exe) и сам .exe (иконка файла, загрузчик).
Поэтому сборщик проверяет совместимость: все обращения `wcfg.<имя>` в модулях пакета должны
существовать в worker_config.py самой старой поддерживаемой версии exe (--baseline-config).
Если нет — нужен новый exe и поднятый min_required.

Использование:
  python build_hot_update.py --version 2.5.2 --out dist_hot [--baseline-config path/to/old/worker_config.py]
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import py_compile
import re
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

HOT_MODULES = [
    "gui_app.py", "worker_agent.py", "hot_updater.py", "engine_downloader.py", "executors.py",
    "gpu_probe.py", "hw_gate.py", "panda_protocol.py", "release_verify.py", "self_destruct.py",
    "worker_selfcheck.py", "panda_theme.py", "panda_theme_assets.py", "panda_tray.py",
]
HOT_EXTRA = ["panda_icon.ico"]
HOT_DIRS = ["ui_fonts"]
NEVER = {"worker_config.py", "public_client.env", ".env", "state.json"}
ZIP_DATE = (2026, 1, 1, 0, 0, 0)  # детерминированный архив: одинаковые исходники -> одинаковый sha


def _wcfg_names_used(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "wcfg":
            used.add(node.attr)
    return used


def _config_names(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.If, ast.Try)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    names.add(sub.target.id)
                elif isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                    names.add(sub.name)
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--src", default=str(HERE))
    ap.add_argument("--out", required=True)
    ap.add_argument("--baseline-config", default="",
                    help="worker_config.py самого старого поддерживаемого exe (проверка совместимости)")
    a = ap.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", a.version):
        raise SystemExit("--version: формат X.Y.Z")
    src = Path(a.src).resolve()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    files = []
    for name in HOT_MODULES + HOT_EXTRA:
        p = src / name
        if not p.is_file():
            raise SystemExit(f"нет файла пакета: {p}")
        files.append((name, p))
    for d in HOT_DIRS:
        for p in sorted((src / d).rglob("*")):
            if p.is_file():
                files.append((p.relative_to(src).as_posix(), p))
    assert not any(n in NEVER for n, _ in files)

    # 1) все модули компилируются
    with tempfile.TemporaryDirectory() as td:
        for name, p in files:
            if name.endswith(".py"):
                py_compile.compile(str(p), cfile=str(Path(td) / (name + "c")), doraise=True)

    # 2) версия ядра в исходниках не старее пакета
    m = re.search(r'_EXE_BASE_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', (src / "worker_config.py").read_text(encoding="utf-8"))
    if m:
        def vt(v: str) -> tuple:
            return tuple(int(x) for x in v.split("."))
        if vt(m.group(1)) > vt(a.version):
            raise SystemExit(f"версия пакета {a.version} старее исходников ({m.group(1)})")

    # 3) совместимость со старым worker_config (он не обновляется пакетом)
    if a.baseline_config:
        base_names = _config_names(Path(a.baseline_config))
        missing = {}
        for name, p in files:
            if name.endswith(".py"):
                miss = sorted(_wcfg_names_used(p) - base_names)
                if miss:
                    missing[name] = miss
        if missing:
            lines = "\n".join(f"  {k}: {', '.join(v)}" for k, v in missing.items())
            raise SystemExit("НЕСОВМЕСТИМО со старым exe (нет в его worker_config):\n" + lines +
                             "\n=> соберите новый exe и поднимите --min-required до его версии.")

    info = {
        "product": "panda-worker",
        "kind": "hot_update",
        "version": a.version,
        "files": {n: hashlib.sha256(p.read_bytes()).hexdigest() for n, p in files},
    }
    zpath = out / f"worker_hot_update_{a.version}.zip"
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, p in sorted(files):
            zi = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, p.read_bytes())
        zi = zipfile.ZipInfo("HOT_UPDATE_INFO.json", date_time=ZIP_DATE)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi, json.dumps(info, ensure_ascii=False, indent=1, sort_keys=True))
    sha = hashlib.sha256(zpath.read_bytes()).hexdigest()
    print(f"OK {zpath.name}  files={len(files)}  bytes={zpath.stat().st_size}  sha256={sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
