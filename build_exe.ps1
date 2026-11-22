$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$PyInstaller = Join-Path $Root ".venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $PyInstaller)) {
    throw "PyInstaller not found at $PyInstaller"
}

& $PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name RemoteScreen `
    --paths src `
    --hidden-import pystray._win32 `
    --collect-data certifi `
    --add-data "src\remote_screen\static;remote_screen\static" `
    launcher.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller exited with code $LASTEXITCODE"
}

$ExePath = Join-Path $Root "dist\RemoteScreen.exe"
if (-not (Test-Path $ExePath)) {
    throw "Expected executable missing: $ExePath"
}

$SizeMB = [Math]::Round((Get-Item $ExePath).Length / 1MB, 2)
Write-Host "Built successfully: $ExePath ($SizeMB MB)"
