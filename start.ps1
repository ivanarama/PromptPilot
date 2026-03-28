# Start PromptPilot services (worker + server + optional bot)
# Usage:
#   .\start.ps1           — worker + server
#   .\start.ps1 -Bot      — worker + server + bot (requires PP_TG_TOKEN)

param(
    [switch]$Bot
)

$ErrorActionPreference = "Stop"
$pidFile = "$PSScriptRoot\.pp-pids.json"
$logDir  = "$PSScriptRoot\logs"

# Load .env if present (same logic as config.py)
$envFile = "$PSScriptRoot\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]*?)\s*=\s*"?([^"]*)"?\s*$') {
            $k = $Matches[1].Trim(); $v = $Matches[2].Trim()
            if ($k -and -not [System.Environment]::GetEnvironmentVariable($k, 'Process')) {
                [System.Environment]::SetEnvironmentVariable($k, $v, 'Process')
            }
        }
    }
    Write-Host "Loaded .env" -ForegroundColor DarkGray
}

# Check if already running
if (Test-Path $pidFile) {
    $old = Get-Content $pidFile | ConvertFrom-Json
    $alive = $old.PSObject.Properties | Where-Object {
        try { Get-Process -Id $_.Value -ErrorAction Stop; $true } catch { $false }
    }
    if ($alive) {
        Write-Host "PromptPilot is already running. Use .\stop.ps1 first." -ForegroundColor Yellow
        exit 1
    }
    Remove-Item $pidFile
}

# Pick executable: prefer built dist\pp.exe, fall back to pp from PATH
if (Test-Path "$PSScriptRoot\dist\pp.exe") {
    $exe = "$PSScriptRoot\dist\pp.exe"
    Write-Host "Using: dist\pp.exe" -ForegroundColor DarkGray
} else {
    $exe = "pp"
    Write-Host "Using: pp (from PATH)" -ForegroundColor DarkGray
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Write-Host "Starting PromptPilot..." -ForegroundColor Cyan

$w = Start-Process $exe -ArgumentList "worker" `
    -RedirectStandardOutput "$logDir\worker.log" `
    -RedirectStandardError  "$logDir\worker.err" `
    -NoNewWindow -PassThru

$s = Start-Process $exe -ArgumentList "server" `
    -RedirectStandardOutput "$logDir\server.log" `
    -RedirectStandardError  "$logDir\server.err" `
    -NoNewWindow -PassThru

$pids = [ordered]@{ worker = $w.Id; server = $s.Id }

Write-Host "  Worker  PID $($w.Id)   logs\worker.log" -ForegroundColor Green
Write-Host "  Server  PID $($s.Id)   http://127.0.0.1:8420" -ForegroundColor Green

if ($Bot -or $env:PP_TG_TOKEN) {
    if (-not $env:PP_TG_TOKEN) {
        Write-Host "PP_TG_TOKEN is not set, skipping bot." -ForegroundColor Yellow
    } else {
        $b = Start-Process $exe -ArgumentList "bot" `
            -RedirectStandardOutput "$logDir\bot.log" `
            -RedirectStandardError  "$logDir\bot.err" `
            -NoNewWindow -PassThru
        $pids.bot = $b.Id
        Write-Host "  Bot     PID $($b.Id)   logs\bot.log" -ForegroundColor Green
    }
}

$pids | ConvertTo-Json | Set-Content $pidFile

Write-Host "`nAll logs in .\logs\   Stop with: .\stop.ps1" -ForegroundColor DarkGray
