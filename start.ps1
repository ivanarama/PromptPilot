# Start PromptPilot services (worker + server + optional bot)
# Usage:
#   .\start.ps1           - worker + server
#   .\start.ps1 -Bot      - worker + server + bot (requires PP_TG_TOKEN)

param(
    [switch]$Bot
)

$ErrorActionPreference = "Stop"
$pidFile = "$PSScriptRoot\.pp-pids.json"
$logDir  = "$PSScriptRoot\logs"

# Load .env if present (same logic as config.py)
$envFile = "$PSScriptRoot\.env"
if (Test-Path -LiteralPath $envFile -PathType Leaf) {
    Get-Content -LiteralPath $envFile -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]*?)\s*=\s*"?([^"]*)"?\s*$') {
            $k = $Matches[1].Trim(); $v = $Matches[2].Trim()
            if ($k -and -not (Test-Path "env:$k")) {
                Set-Item -Path "env:$k" -Value $v
            }
        }
    }
    Write-Host "Loaded .env" -ForegroundColor DarkGray
}

function Test-PromptPilotCommandLine([string]$Pattern) {
    [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match $Pattern })
}

# Check if already running
if (Test-Path $pidFile) {
    $old = Get-Content $pidFile | ConvertFrom-Json
    $alive = $old.PSObject.Properties | Where-Object {
        # A watcher may survive a crashed core process.  It is discovered by
        # command line in stop.ps1, but must not make a fresh core start look
        # healthy or block recovery from a stale PID file.
        if ($_.Name -eq 'verdict_watcher') { return $false }
        # pid=0 means "not launched" (long-dead Telegram bot); Get-Process -Id 0
        # succeeds on the System Idle process and made a fully dead stack look
        # "already running" (incident 07.10: start-all refused to revive it).
        if (-not $_.Value -or [int]$_.Value -le 0) { return $false }
        try { Get-Process -Id $_.Value -ErrorAction Stop; $true } catch { $false }
    }
    if ($alive) {
        Write-Host "PromptPilot is already running. Use .\stop.ps1 first." -ForegroundColor Yellow
        exit 1
    }
    Remove-Item $pidFile
}

# Pick executable: prefer built dist, then the repository venv, then PATH.
if (Test-Path "$PSScriptRoot\dist\pp.exe") {
    $exe = "$PSScriptRoot\dist\pp.exe"
    Write-Host "Using: dist\pp.exe" -ForegroundColor DarkGray
} elseif (Test-Path "$PSScriptRoot\.venv\Scripts\pp.exe") {
    $exe = "$PSScriptRoot\.venv\Scripts\pp.exe"
    Write-Host "Using: .venv\Scripts\pp.exe" -ForegroundColor DarkGray
} else {
    $exe = "pp"
    Write-Host "Using: pp (from PATH)" -ForegroundColor DarkGray
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Write-Host "Starting PromptPilot..." -ForegroundColor Cyan

$w = Start-Process $exe -ArgumentList "worker" `
    -RedirectStandardOutput "$logDir\worker.log" `
    -RedirectStandardError  "$logDir\worker.err" `
    -WindowStyle Hidden -PassThru

$s = Start-Process $exe -ArgumentList "server" `
    -RedirectStandardOutput "$logDir\server.log" `
    -RedirectStandardError  "$logDir\server.err" `
    -WindowStyle Hidden -PassThru

$pids = [ordered]@{ worker = $w.Id; server = $s.Id }

Write-Host "  Worker  PID $($w.Id)   logs\worker.log" -ForegroundColor Green
Write-Host "  Server  PID $($s.Id)   http://127.0.0.1:8420" -ForegroundColor Green

# The review-chain controller is an additive companion.  It uses the same
# PromptPilot DB and port, and stays idle for workflows without review_chain.
$cascadePython = if (Test-Path 'C:\Python314\python.exe') { 'C:\Python314\python.exe' } elseif (Test-Path "$PSScriptRoot\.venv\Scripts\python.exe") { "$PSScriptRoot\.venv\Scripts\python.exe" } else { 'python' }
$cascade = Start-Process $cascadePython -ArgumentList @('-X', 'utf8', "$PSScriptRoot\cascade-review.py") `
    -WorkingDirectory $PSScriptRoot -RedirectStandardOutput "$logDir\cascade-review.log" `
    -RedirectStandardError "$logDir\cascade-review.err" -WindowStyle Hidden -PassThru
$pids.cascade = $cascade.Id
Write-Host "  Cascade PID $($cascade.Id)   ~/.promptpilot/cascade-review.log" -ForegroundColor Green

# The verdict-repair watcher is part of the local autonomous workflow.  It
# consumes ELICIT events and auto-resolves the narrow capability question
# (helper-agent) when the workflow is explicitly automated.  start-all.ps1
# also knows how to start it; the command-line guard keeps both entry points
# idempotent and prevents two watchers from consuming the same event stream.
if (Test-PromptPilotCommandLine 'verdict-repair-watcher\.py') {
    Write-Host "  Verdict watcher already running" -ForegroundColor DarkGray
} else {
    $watcherPython = if (Test-Path 'C:\Python314\pythonw.exe') { 'C:\Python314\pythonw.exe' } elseif (Test-Path "$PSScriptRoot\.venv\Scripts\pythonw.exe") { "$PSScriptRoot\.venv\Scripts\pythonw.exe" } else { 'pythonw' }
    $watcher = Start-Process $watcherPython -ArgumentList @('-X', 'utf8', "$PSScriptRoot\verdict-repair-watcher.py") `
        -WorkingDirectory $PSScriptRoot -RedirectStandardOutput "$logDir\verdict-repair-watcher.stdout.log" `
        -RedirectStandardError "$logDir\verdict-repair-watcher.stderr.log" -WindowStyle Hidden -PassThru
    $pids.verdict_watcher = $watcher.Id
    Write-Host "  Verdict watcher PID $($watcher.Id)   ~/.promptpilot/verdict-repair.log" -ForegroundColor Green
}

if ($Bot -or $env:PP_TG_TOKEN) {
    if (-not $env:PP_TG_TOKEN) {
        Write-Host "PP_TG_TOKEN is not set, skipping bot." -ForegroundColor Yellow
    } else {
        $b = Start-Process $exe -ArgumentList "bot" `
        -RedirectStandardOutput "$logDir\bot.log" `
        -RedirectStandardError  "$logDir\bot.err" `
        -WindowStyle Hidden -PassThru
        $pids.bot = $b.Id
        Write-Host "  Bot     PID $($b.Id)   logs\bot.log" -ForegroundColor Green
    }
}

# ASCII: без BOM — PowerShell-UTF8 пишет BOM и ломает json-читателей
$pids | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding Ascii

Write-Host "`nAll logs in .\logs\   Stop with: .\stop.ps1" -ForegroundColor DarkGray
