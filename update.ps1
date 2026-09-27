# PromptPilot-Update — обновление до последнего релиза автора в один клик.
# Логика:
#   1. Сохранить текущее состояние (backup-ветка).
#   2. git fetch origin (иванарама/PromptPilot).
#   3. Слить origin/main в локальный main. Наши локальные адд-оны (инструменты
#      в корне, чипы UI, хотфиксы worker.py) сохраняются; конфликты — стоп
#      с ясным сообщением (решаем вручную, терять ничего не даём).
#   4. Если merge прошёл чисто — перезапустить систему (start-all).
# ВАЖНО: запускать лучше, когда пайплайн стоит (awaiting_human) — рестарт
# воркера прерывает текущую задачу. Система сама продолжит её после старта.

$ErrorActionPreference = 'Continue'
$PpDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host '=== PromptPilot Update ===' -ForegroundColor Cyan
Set-Location $PpDir

# 0. Статус пайплайна — предупреждение, если задачи едут
$runningTasks = 0
try {
    $tasks = Invoke-RestMethod 'http://127.0.0.1:8420/api/tasks?limit=100' -TimeoutSec 5
    $runningTasks = ($tasks | Where-Object { $_.status -eq 'running' } | Measure-Object).Count
} catch {}
if ($runningTasks -gt 0) {
    Write-Host ("ВНИМАНИЕ: сейчас выполняется задач: " + $runningTasks + ". Рестарт воркера их прервёт (система продолжит сама).") -ForegroundColor Yellow
    $confirm = Read-Host 'Продолжить обновление? (y/N)'
    if ($confirm -ne 'y') { Write-Host 'Обновление отменено.'; exit 0 }
}

# 1. Backup текущего main
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
git branch -f "backup/main-$stamp" main 2>$null
Write-Host "[1] Backup-ветка: backup/main-$stamp" -ForegroundColor Green

# 2. Fetch
Write-Host '[2] Загружаю изменения автора...'
git fetch origin 2>&1 | Select-Object -Last 2

# 3. Merge origin/main
Write-Host '[3] Слияние origin/main...'
$mergeOk = $true
git merge origin/main --no-edit 2>&1 | ForEach-Object { Write-Host "    $_" }
if ($LASTEXITCODE -ne 0) { $mergeOk = $false }

if (-not $mergeOk) {
    Write-Host '[3] КОНФЛИКТЫ СЛИЯНИЯ — остановлено. Конфликтные файлы:' -ForegroundColor Red
    git diff --name-only --diff-filter=U
    Write-Host 'Разрешите конфликты (или позовите агента), затем: git commit + перезапуск.' -ForegroundColor Yellow
    exit 1
}

# 4. Перезапуск системы (start-all идемпотентен: остановит старое, поднимет новое)
Write-Host '[4] Перезапуск сервисов...'
& (Join-Path $PpDir 'stop.ps1') 2>$null | Out-Null
Start-Sleep -Seconds 3
& (Join-Path $PpDir 'start.ps1') 2>$null | Out-Null
Start-Sleep -Seconds 8
try {
    $s = Invoke-RestMethod 'http://127.0.0.1:8420/api/worker/status' -TimeoutSec 5
    Write-Host ("[4] Система: " + $s.state) -ForegroundColor $(if ($s.state -eq 'online') { 'Green' } else { 'Red' })
} catch {
    Write-Host '[4] Сервер не поднялся — запусти PromptPilot-Старт-всё.bat' -ForegroundColor Red
}

# 5. Авто-Продолжить пайплайна (то же, что в start-all, шаг 6)
Write-Host '[5] Авто-Продолжить воркфлоу...'
try {
    $wfs = Invoke-RestMethod 'http://127.0.0.1:8420/api/workflows' -TimeoutSec 5
    foreach ($w in $wfs) {
        if ($w.status -ne 'awaiting_human') { continue }
        if ($w.slug -like 'pkg-*') { continue }   # мёртвые пакеты не будим
        try {
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8420/api/workflows/$($w.id)/human-input" -ContentType 'application/json' -Body (@{expected_version=$w.state_version; text='Продолжить работу с учётом сохранённого состояния'; resume=$true} | ConvertTo-Json -Depth 5) -TimeoutSec 15 | Out-Null
            Write-Host ("    " + $w.slug + ": продолжен") -ForegroundColor Green
        } catch {}
    }
    Start-Sleep -Seconds 6
    foreach ($w in $wfs) {
        if ($w.status -ne 'awaiting_human') { continue }
        if ($w.slug -like 'pkg-*') { continue }
        try {
            $fresh = Invoke-RestMethod "http://127.0.0.1:8420/api/workflows/$($w.id)" -TimeoutSec 5
            if ($fresh.status -eq 'queued') {
                Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8420/api/workflows/$($w.id)/sync" -ContentType 'application/json' -Body (@{expected_version=$fresh.state_version} | ConvertTo-Json -Depth 5) -TimeoutSec 15 | Out-Null
            }
        } catch {}
    }
} catch { Write-Host '[5] авто-Продолжить: ошибка' -ForegroundColor Yellow }

Write-Host '=== Обновление завершено. Логи: ~/.promptpilot/*.log ===' -ForegroundColor Cyan
