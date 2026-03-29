# Stop all PromptPilot services
# Usage: .\stop.ps1

$pidFile = "$PSScriptRoot\.pp-pids.json"

if (-not (Test-Path $pidFile)) {
    Write-Host "Not running (no .pp-pids.json found)." -ForegroundColor Yellow
    exit 0
}

$services = Get-Content $pidFile | ConvertFrom-Json

foreach ($prop in $services.PSObject.Properties) {
    $name = $prop.Name
    $id   = $prop.Value
    try {
        Stop-Process -Id $id -Force -ErrorAction Stop
        Write-Host "Stopped $name (PID $id)" -ForegroundColor Green
    } catch {
        Write-Host "$name (PID $id) - already stopped" -ForegroundColor DarkGray
    }
}

Remove-Item $pidFile
Write-Host "PromptPilot stopped." -ForegroundColor Cyan
