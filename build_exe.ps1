# panda_worker/build_exe.ps1  (v2.5.2)
# v2.5.2: self-test runs with --no-hot (checks the BUILT code, not a hot-update on this PC).
# v2.5.1: new "PANDA production" UI (panda_theme*.py), own exe/taskbar icon, bundled UI fonts (OFL).
# Builds PandaWorkerSetup.exe (with ZERO secret leakage) + optional panda_engines_pack.zip
# Usage:
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1 -ServerUrl "https://130.61.36.196:8790" -Python "C:\path\python.exe"
#
# FIX-2026-11-19: https по умолчанию, в сборку кладётся сертификат сервера (pinning),
# ключ проверки подписи релизов и модуль cryptography; локальной папки engines больше нет
# (движки качаются подписанным пакетом); результат кладётся в .\dist, без чужих путей.

param(
    [string]$ServerUrl = "https://130.61.36.196:8790",
    [string]$Python = "",
    [string]$ReleasePubkeyHex = "b85afa6e02871ef5bffcacf3d3f27f96180200c5f90f7dff5c450a16720ccd20",
    [switch]$PackEngines
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here
Set-Location $Here

if (-not $ServerUrl.StartsWith("https://")) {
    throw "ServerUrl must start with https:// (the server rejects plain HTTP workers)."
}
foreach ($need in @("panda_icon.ico", "panda_theme.py", "panda_theme_assets.py", "ui_fonts\PandaSansBlack.ttf")) {
    if (-not (Test-Path (Join-Path $Here $need))) { throw "UI asset missing: $need" }
}
if (-not (Test-Path (Join-Path $Here "panda_server_ca.pem"))) {
    throw "panda_server_ca.pem is missing next to build_exe.ps1 (pinned server certificate)."
}

Write-Host "=== [1/4] Python + dependencies ===" -ForegroundColor Cyan
$py = $Python
if (-not $py) {
    $cands = @(
        (Join-Path $Here ".venv\Scripts\python.exe"),
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    )
    foreach ($c in $cands) { if (Test-Path $c) { $py = $c; break } }
    if (-not $py) { $py = "python" }
}
Write-Host "Python: $py"
& $py -m pip install --quiet --upgrade pyinstaller -r (Join-Path $Here "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# Strictly non-secret public defaults (NEVER bundle a local .env!)
$pubEnv = Join-Path $Here "public_client.env"
"SERVER_URL=$ServerUrl`r`nTLS_VERIFY=1`r`nRELEASE_PUBKEY_HEX=$ReleasePubkeyHex`r`n" | Set-Content -Path $pubEnv -Encoding ASCII

Write-Host "=== [2/4] Building PandaWorkerSetup.exe ===" -ForegroundColor Cyan
& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "PandaWorkerSetup" `
    --version-file "version_info.txt" `
    --icon "panda_icon.ico" `
    --add-data "public_client.env;." `
    --add-data "panda_icon.ico;." `
    --add-data "ui_fonts;ui_fonts" `
    --add-data "panda_server_ca.pem;." `
    --hidden-import "worker_config" `
    --hidden-import "hw_gate" `
    --hidden-import "engine_downloader" `
    --hidden-import "hot_updater" `
    --hidden-import "self_destruct" `
    --hidden-import "worker_agent" `
    --hidden-import "executors" `
    --hidden-import "gpu_probe" `
    --hidden-import "panda_protocol" `
    --hidden-import "release_verify" `
    --hidden-import "panda_theme" `
    --hidden-import "panda_theme_assets" `
    --hidden-import "uuid" `
    --hidden-import "dataclasses" `
    --collect-submodules "PIL" `
    --collect-submodules "cryptography" `
    --exclude-module "_deepeng" `
    --exclude-module "_deepeng2" `
    --exclude-module "_engcheck" `
    --exclude-module "_execchk" `
    --exclude-module "_manualupd" `
    "gui_app.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$BuiltExe = Join-Path $Here "dist\PandaWorkerSetup.exe"
if (-not (Test-Path $BuiltExe)) { throw "Build output not found: $BuiltExe" }

Write-Host "=== [3/4] Self-test of the built exe ===" -ForegroundColor Cyan
# Isolated home: the installed worker on this PC (its token / hot-update) must not affect the test.
$StHome = Join-Path $env:TEMP ("panda_selftest_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $StHome | Out-Null
$prevHome = $env:PANDA_WORKER_HOME
$env:PANDA_WORKER_HOME = $StHome
try {
    $p = Start-Process -FilePath $BuiltExe -ArgumentList "--self-test","--no-hot" -Wait -PassThru -WindowStyle Hidden
} finally {
    $env:PANDA_WORKER_HOME = $prevHome
}
$stJson = Join-Path $StHome "selftest_ok.json"
if (Test-Path $stJson) { Write-Host ("SELFTEST: " + (Get-Content $stJson -Raw)) }
Remove-Item -Recurse -Force $StHome -ErrorAction SilentlyContinue
if ($p.ExitCode -ne 0) { throw "Self-test of the built exe FAILED (exit $($p.ExitCode))" }

$hash = (Get-FileHash -Algorithm SHA256 $BuiltExe).Hash.ToLower()
$size = (Get-Item $BuiltExe).Length
Write-Host "READY EXE: $BuiltExe" -ForegroundColor Green
Write-Host "SIZE:   $size bytes"
Write-Host "SHA256: $hash"
Write-Host "Next: upload to server worker_releases, sign with tools/sign_release.py sign, then panda_worker_admin.py set-update."

if ($PackEngines) {
    Write-Host "=== [4/4] Packing panda_engines_pack.zip ===" -ForegroundColor Cyan
    & $py (Join-Path $Root "tools\build_engines_pack.py")
}
