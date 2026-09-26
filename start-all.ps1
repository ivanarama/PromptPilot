# PromptPilot start-all — поднимает ВСЮ систему одним запуском (идемпотентно:
# каждый компонент стартует только если его ещё нет). Можно запускать повторно.
# Компоненты:
#   1. PromptPilot сервер + воркер   (start.ps1, порт 8420)
#   2. SSH-туннель до KZ-сервера     (SOCKS 127.0.0.1:10811, для Telegram)
#   3. Telegram-бот @PromtPilotBot   (через туннель)
#   4. Verdict-Repair Watcher        (TypeSafe Jev: авто-починка вердиктов
#                                     + таймаут-авторезюме, лимит 2/задача)
#   5. Queued-Nudger                 (пинок queued-воркфлоу, баг resume-not-noticed)

$ErrorActionPreference = 'Continue'
$PpDir    = 'C:\Users\Nachfin\Desktop\Projets\Other\PromptPilot'
$Pem      = 'C:\Users\Nachfin\Desktop\Projets\VPN\vpn-almaty.pem'
$KzServer = 'root@85.198.88.68'
$Socks    = 10811

function Test-Port([int]$Port) {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}
function Test-CmdLine([string]$Pattern) {
    [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match $Pattern })
}
function Start-HiddenConsole([string]$Exe, [string]$ArgList, [string]$WorkDir) {
    Start-Process -FilePath $Exe -ArgumentList $ArgList `
        -WorkingDirectory $WorkDir -WindowStyle Hidden
}

Write-Host '=== PromptPilot start-all ===' -ForegroundColor Cyan

# 1. Server + worker
$healthy = $false
try {
    $s = Invoke-RestMethod 'http://127.0.0.1:8420/api/worker/status' -TimeoutSec 2
    $healthy = ($s.state -eq 'online')
} catch {}
if ($healthy) {
    Write-Host '[1] PromptPilot server+worker: already online' -ForegroundColor Green
} else {
    Write-Host '[1] PromptPilot: running start.ps1...'
    & (Join-Path $PpDir 'start.ps1') | Out-Null
    Start-Sleep -Seconds 8
    try {
        $s = Invoke-RestMethod 'http://127.0.0.1:8420/api/worker/status' -TimeoutSec 2
        if ($s.state -eq 'online') { Write-Host '[1] PromptPilot: online' -ForegroundColor Green }
        else { Write-Host ('[1] PromptPilot: ' + $s.state) -ForegroundColor Red }
    } catch {
        Write-Host '[1] PromptPilot: NOT RESPONDING - run PromptPilot-Start.bat manually' -ForegroundColor Red
    }
}

# 2. SSH tunnel (Telegram)
if (Test-Port $Socks) {
    Write-Host '[2] SSH tunnel KZ: already up' -ForegroundColor Green
} else {
    Write-Host '[2] Raising SSH tunnel...'
    Start-Process -WindowStyle Hidden -FilePath 'ssh' -ArgumentList @(
        '-i', $Pem, '-N', '-D', "$Socks",
        '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
        '-o', 'ExitOnForwardFailure=yes', '-o', 'BatchMode=yes', $KzServer)
    Start-Sleep -Seconds 6
    if (Test-Port $Socks) { Write-Host '[2] Tunnel up' -ForegroundColor Green }
    else { Write-Host '[2] Tunnel FAILED (TG bot will be offline)' -ForegroundColor Yellow }
}

# 3. Telegram bot
if (Test-CmdLine 'promptpilot bot') {
    Write-Host '[3] Telegram bot: already running' -ForegroundColor Green
} elseif (Test-Port $Socks) {
    Start-HiddenConsole 'powershell.exe' '-NoProfile -ExecutionPolicy Bypass -Command "$env:ALL_PROXY=''socks5h://127.0.0.1:10811''; $env:HTTPS_PROXY=''socks5h://127.0.0.1:10811''; & ''C:\Users\Nachfin\Desktop\Projets\Other\PromptPilot\.venv\Scripts\python.exe'' -m promptpilot bot"' $PpDir
    Write-Host '[3] Telegram bot started (hidden)' -ForegroundColor Green
} else {
    Write-Host '[3] Telegram bot: skipped - no tunnel' -ForegroundColor Yellow
}

# 4. Verdict-Repair Watcher (TypeSafe Jev)
if (Test-CmdLine 'verdict-repair-watcher') {
    Write-Host '[4] Verdict-Repair Watcher: already running' -ForegroundColor Green
} else {
    Start-HiddenConsole 'C:\Python314\pythonw.exe' '-X utf8 "C:\Users\Nachfin\Desktop\Projets\Other\PromptPilot\verdict-repair-watcher.py"' $PpDir
    Start-Sleep -Seconds 3
    if (Test-CmdLine 'verdict-repair-watcher') {
        Write-Host '[4] Verdict-Repair Watcher started (hidden)' -ForegroundColor Green
    } else {
        Write-Host '[4] Watcher FAILED - see ~/.promptpilot/verdict-repair.log' -ForegroundColor Red
    }
}

# 4b. Quota-Failover Watcher (квота MiniMax -> перекройка ролей конвейера)
if (Test-CmdLine 'quota-failover-watcher') {
    Write-Host '[4b] Quota-Failover Watcher: already running' -ForegroundColor Green
} else {
    Start-HiddenConsole 'C:\Python314\pythonw.exe' '-X utf8 "C:\Users\Nachfin\Desktop\Projets\Other\PromptPilot\quota-failover-watcher.py"' $PpDir
    Start-Sleep -Seconds 3
    if (Test-CmdLine 'quota-failover-watcher') {
        Write-Host '[4b] Quota-Failover Watcher started (hidden)' -ForegroundColor Green
    } else {
        Write-Host '[4b] Quota-Failover Watcher FAILED - see ~/.promptpilot/quota-failover.log' -ForegroundColor Red
    }
}

# 5. Queued-Nudger
if (Test-CmdLine 'queued-nudger') {
    Write-Host '[5] Queued-Nudger: already running' -ForegroundColor Green
} else {
    Start-HiddenConsole 'C:\Python314\python.exe' '-X utf8 "C:\Users\Nachfin\Desktop\Projets\Other\PromptPilot\queued-nudger.py"' $PpDir
    Write-Host '[5] Queued-Nudger started (hidden)' -ForegroundColor Green
}

# 6. Cascade review controller (optional additive owner of review_chain)
if (Test-CmdLine 'cascade-review\.py') {
    Write-Host '[6] Cascade Review: already running' -ForegroundColor Green
} else {
    $cascadePython = if (Test-Path 'C:\Python314\python.exe') { 'C:\Python314\python.exe' } else { 'python' }
    Start-HiddenConsole $cascadePython '-X utf8 "C:\Users\Nachfin\Desktop\Projets\Other\PromptPilot\cascade-review.py"' $PpDir
    Start-Sleep -Seconds 2
    if (Test-CmdLine 'cascade-review\.py') {
        Write-Host '[6] Cascade Review started (hidden)' -ForegroundColor Green
    } else {
        Write-Host '[6] Cascade Review FAILED - see ~/.promptpilot/cascade-review.log' -ForegroundColor Red
    }
}

# 7. Авто-Продолжить: обычные workflow в awaiting_human получают resume+sync.
# Review-chain воркфлоу принадлежат каскадному контроллеру и здесь не будятся.
Write-Host '[7] Авто-Продолжить обычные воркфлоу...' -ForegroundColor Cyan
try {
    $wfs = Invoke-RestMethod 'http://127.0.0.1:8420/api/workflows' -TimeoutSec 5
    $resumed = @()
    # Мёртвые пакеты: работа слита в main, worktree удалены — не будить
    $dead = @('pkg-reverso-m2', 'pkg-backup-m3')
    foreach ($w in $wfs) {
        if ($w.status -ne 'awaiting_human') { continue }
        if ($dead -contains $w.slug) { continue }
        if ($w.config -and $w.config.review_chain -and $w.config.review_chain.enabled) {
            Write-Host ("    " + $w.slug + ": оставлен каскаду ревью") -ForegroundColor DarkGray
            continue
        }
        try {
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8420/api/workflows/$($w.id)/human-input" -ContentType 'application/json' -Body (@{expected_version=$w.state_version; text='Продолжить работу с учётом сохранённого состояния'; resume=$true} | ConvertTo-Json) -TimeoutSec 15 | Out-Null
            $resumed += $w.slug
            Write-Host ("    " + $w.slug + ": продолжен") -ForegroundColor Green
        } catch {
            Write-Host ("    " + $w.slug + ": resume отклонён") -ForegroundColor Yellow
        }
    }
    Start-Sleep -Seconds 6
    # queued после resume — sync-пинок (воркер мог не заметить)
    foreach ($w in $wfs) {
        if ($w.status -ne 'awaiting_human') { continue }
        try {
            $fresh = Invoke-RestMethod "http://127.0.0.1:8420/api/workflows/$($w.id)" -TimeoutSec 5
            if ($fresh.status -eq 'queued') {
                Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8420/api/workflows/$($w.id)/sync" -ContentType 'application/json' -Body (@{expected_version=$fresh.state_version} | ConvertTo-Json) -TimeoutSec 15 | Out-Null
                Write-Host ("    " + $w.slug + ": sync-пинок") -ForegroundColor Green
            }
        } catch {}
    }
    if ($resumed.Count -eq 0) { Write-Host '    Ожидающих человека воркфлоу нет' }
} catch { Write-Host '[7] ошибка авто-Продолжить' -ForegroundColor Yellow }

Write-Host '=== Done. Logs: ~/.promptpilot/*.log ===' -ForegroundColor Cyan
