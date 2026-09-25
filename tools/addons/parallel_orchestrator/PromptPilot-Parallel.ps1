[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Plan,
    [string]$StateFile,
    [ValidateRange(1, 16)]
    [int]$Concurrency = 4,
    [int]$PollSeconds = 3,
    [switch]$Once,
    [switch]$DryRun,
    [string]$BaseUrl = 'http://127.0.0.1:8420'
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = 'python'
}

# This is an additive scheduler setting. It does not start/stop a worker.
$env:PP_CONCURRENCY = [string]$Concurrency
$arguments = @('-m', 'tools.addons.parallel_orchestrator', 'run', $Plan, '--base-url', $BaseUrl, '--poll-seconds', [string]$PollSeconds)
if ($StateFile) { $arguments += @('--state-file', $StateFile) }
if ($Once) { $arguments += '--once' }
if ($DryRun) { $arguments += '--dry-run' }

Push-Location $repoRoot
try {
    & $python @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
