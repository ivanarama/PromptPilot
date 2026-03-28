# Build pp.exe with PyInstaller
# Usage: .\build.ps1           — normal build
#        .\build.ps1 -Debug    — verbose output for troubleshooting

param([switch]$Debug)

$ErrorActionPreference = "Stop"

Write-Host "=== PromptPilot Build ===" -ForegroundColor Cyan

# Install pyinstaller if missing
python -m PyInstaller --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller..." -ForegroundColor Yellow
    python -m pip install pyinstaller
}

# Clean previous build
if (Test-Path dist) { Remove-Item dist -Recurse -Force }
if (Test-Path build) { Remove-Item build -Recurse -Force }

# Build
if ($Debug) {
    Write-Host "Building pp.exe (debug)..." -ForegroundColor Yellow
    python -m PyInstaller pp.spec --clean --debug all
} else {
    Write-Host "Building pp.exe..." -ForegroundColor Yellow
    python -m PyInstaller pp.spec --clean
}

if (Test-Path "dist\pp.exe") {
    $size = [math]::Round((Get-Item "dist\pp.exe").Length / 1MB, 1)
    Write-Host "`nDone: dist\pp.exe ($size MB)" -ForegroundColor Green
    Write-Host "Test: .\dist\pp.exe --help" -ForegroundColor Gray
} else {
    Write-Host "Build failed." -ForegroundColor Red
    exit 1
}
