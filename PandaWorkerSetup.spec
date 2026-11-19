# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['worker_config', 'hw_gate', 'engine_downloader', 'hot_updater', 'self_destruct', 'worker_agent', 'executors', 'gpu_probe', 'panda_protocol', 'release_verify', 'uuid', 'dataclasses']
hiddenimports += collect_submodules('PIL')
# v2.5.0: Ed25519-проверка подписи релизов обязательна
hiddenimports += collect_submodules('cryptography')


a = Analysis(
    ['gui_app.py'],
    pathex=[],
    binaries=[],
    datas=[('public_client.env', '.'), ('panda_server_ca.pem', '.')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['_deepeng', '_deepeng2', '_engcheck', '_execchk', '_manualupd'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PandaWorkerSetup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
)
