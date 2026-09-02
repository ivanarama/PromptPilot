# Stop all PromptPilot services.
# Usage: .\stop.ps1

[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = "Stop"
$pidFile = "$PSScriptRoot\.pp-pids.json"

# Load only the port needed for runtime discovery. A worker may replace itself
# during code reload, so the PID written by start.ps1 is necessarily only a
# startup hint, not the source of truth.
$port = 8420
$envFile = "$PSScriptRoot\.env"
if (Test-Path -LiteralPath $envFile -PathType Leaf) {
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        if ($line -match '^\s*PP_PORT\s*=\s*"?([0-9]+)"?\s*$') {
            $port = [int]$Matches[1]
        }
    }
}
if ($env:PP_PORT -match '^[0-9]+$') {
    $port = [int]$env:PP_PORT
}

$targets = [ordered]@{}

function Add-PromptPilotTarget([string]$name, [int]$processId) {
    if ($processId -le 0 -or $targets.Contains([string]$processId)) {
        return
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if (-not $process) {
        return
    }
    $command = [string]$process.CommandLine
    $looksLikePromptPilot =
        $command -match '(?i)-m\s+promptpilot\s+(worker|server|bot)\b' -or
        ($command -match '(?i)[\\/]pp(?:\.exe)?["'']?\s+(worker|server|bot)\b' -and
         $command.IndexOf($PSScriptRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    if (-not $looksLikePromptPilot) {
        Write-Host "Skip stale $name PID $processId (command is not PromptPilot)." -ForegroundColor Yellow
        return
    }
    $targets[[string]$processId] = $name
}

# The heartbeat is authoritative for a self-reloaded worker.
try {
    $status = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/worker/status" -TimeoutSec 2
    if ($status.pid) {
        Add-PromptPilotTarget "worker (heartbeat)" ([int]$status.pid)
    }
} catch {}

# The listening socket is authoritative for the server even when the launcher
# process from start.ps1 has already exited.
try {
    foreach ($listener in Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop) {
        Add-PromptPilotTarget "server (port $port)" ([int]$listener.OwningProcess)
    }
} catch {}

# Keep startup PIDs as a fallback for services (notably the optional bot) which
# have no HTTP liveness endpoint. Reused/stale PIDs are rejected above.
if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
    try {
        $services = Get-Content -LiteralPath $pidFile -Encoding UTF8 -Raw | ConvertFrom-Json
        foreach ($prop in $services.PSObject.Properties) {
            Add-PromptPilotTarget $prop.Name ([int]$prop.Value)
        }
    } catch {
        Write-Host "Cannot read .pp-pids.json: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if (-not $targets.Count) {
    Write-Host "PromptPilot is not running (no verified processes found)." -ForegroundColor Yellow
} else {
    $snapshot = @(Get-CimInstance Win32_Process)
    function Stop-ProcessTree([int]$processId) {
        foreach ($child in @($snapshot | Where-Object { $_.ParentProcessId -eq $processId })) {
            Stop-ProcessTree ([int]$child.ProcessId)
        }
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }

    foreach ($entry in $targets.GetEnumerator()) {
        $processId = [int]$entry.Key
        if ($PSCmdlet.ShouldProcess("$($entry.Value) (PID $processId)", "Stop process tree")) {
            Stop-ProcessTree $processId
            Write-Host "Stopped $($entry.Value) (PID $processId)" -ForegroundColor Green
        }
    }
}

if ((Test-Path -LiteralPath $pidFile -PathType Leaf) -and
    $PSCmdlet.ShouldProcess($pidFile, "Remove stale PID file")) {
    Remove-Item -LiteralPath $pidFile -Force
}

if ($WhatIfPreference) {
    Write-Host "PromptPilot stop plan checked; no process was stopped." -ForegroundColor Cyan
} else {
    Write-Host "PromptPilot stopped." -ForegroundColor Cyan
}
