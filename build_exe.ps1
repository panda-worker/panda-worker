# panda_worker/build_exe.ps1
# Builds PandaWorkerSetup.exe (with ZERO secret leakage) + optional panda_engines_pack.zip
# Usage:
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1 -ServerUrl "https://worker.yourdomain.com"

param(
    [string]$ServerUrl = "http://130.61.36.196:8790",
    [switch]$PackEngines
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here
Set-Location $Here

Write-Host "=== [1/3] Checking PyInstaller ===" -ForegroundColor Cyan
$py = "C:\Users\user\dash\colibri_bot\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $py = "python"
}
& $py -m pip install --quiet pyinstaller requests pillow

# Write strictly non-secret public defaults (NEVER bundle local .env!)
$pubEnv = Join-Path $Here "public_client.env"
"SERVER_URL=$ServerUrl`r`nTLS_VERIFY=1`r`n" | Set-Content -Path $pubEnv -Encoding ASCII

Write-Host "=== [2/3] Building PandaWorkerSetup.exe ===" -ForegroundColor Cyan
& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "PandaWorkerSetup" `
    --version-file "version_info.txt" `
    --add-data "public_client.env;." `
    --add-data "engines;engines_bundled" `
    --hidden-import "worker_config" `
    --hidden-import "hw_gate" `
    --hidden-import "engine_downloader" `
    --hidden-import "hot_updater" `
    --hidden-import "self_destruct" `
    --hidden-import "worker_agent" `
    --hidden-import "executors" `
    --hidden-import "gpu_probe" `
    --hidden-import "panda_protocol" `
    --hidden-import "uuid" `
    --hidden-import "dataclasses" `
    --collect-submodules "PIL" `
    "gui_app.py"

$BotReleases = Join-Path $Root "colibri_bot\worker_releases"
New-Item -ItemType Directory -Force -Path $BotReleases | Out-Null

$BuiltExe = Join-Path $Here "dist\PandaWorkerSetup.exe"
Copy-Item -Force $BuiltExe (Join-Path $BotReleases "PandaWorkerSetup.exe")
Write-Host "READY EXE: $BotReleases\PandaWorkerSetup.exe" -ForegroundColor Green

if ($PackEngines) {
    Write-Host "=== [3/3] Packing panda_engines_pack.zip ===" -ForegroundColor Cyan
    & $py (Join-Path $Root "tools\build_engines_pack.py")
}
