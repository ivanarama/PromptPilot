# Build pp.exe with PyInstaller
# Usage: .\build.ps1

$ErrorActionPreference = "Stop"

Write-Host "=== PromptPilot Build ===" -ForegroundColor Cyan

# Install pyinstaller if missing
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Host "Installing PyInstaller..." -ForegroundColor Yellow
    pip install pyinstaller
}

# Clean previous build
if (Test-Path dist) { Remove-Item dist -Recurse -Force }
if (Test-Path build) { Remove-Item build -Recurse -Force }

# Build
Write-Host "Building pp.exe..." -ForegroundColor Yellow
pyinstaller pp.spec --clean

if (Test-Path "dist\pp.exe") {
    $size = [math]::Round((Get-Item "dist\pp.exe").Length / 1MB, 1)
    Write-Host "`nDone: dist\pp.exe ($size MB)" -ForegroundColor Green
    Write-Host "Test: .\dist\pp.exe --help" -ForegroundColor Gray
} else {
    Write-Host "Build failed." -ForegroundColor Red
    exit 1
}
