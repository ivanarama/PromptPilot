# PromptPilot-Продолжить.ps1 — одна кнопка «продолжить пайплайн».
# Что делает:
#   1. Берёт текущий статус воркфлоу reader.
#   2. awaiting_human  -> шлёт resume (то же, что кнопка «Продолжить» в UI).
#   3. queued/executing-> шлёт sync (пинок воркеру, если он не заметил resume).
#   4. Печатает итоговый статус и провайдера экзекьютора.
# При 409 (конфликт версии) сам перечитывает версию и пробует ещё раз.

$ErrorActionPreference = 'Stop'
$BaseUrl = 'http://127.0.0.1:8420'
$WfId    = 'wf_1e0bd7a5cfb14d13a60c596e06c85fd9'   # workflow reader (BookApp)

function Get-Wf {
    Invoke-RestMethod -Uri "$BaseUrl/api/workflows/$WfId" -TimeoutSec 15
}

function Post($suffix, $body) {
    Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/workflows/$WfId/$suffix" `
        -ContentType 'application/json' -Body ($body | ConvertTo-Json -Depth 10) -TimeoutSec 20
}

try {
    $wf = Get-Wf
} catch {
    Write-Host "Сервер PromptPilot не отвечает ($BaseUrl). Запусти PromptPilot-Start.bat" -ForegroundColor Red
    exit 1
}

$exec = $wf.config.roles.executor.provider
Write-Host ("Статус сейчас : {0} (раунд {1}, экзекьютор: {2})" -f $wf.status, $wf.current_round, $exec)

switch ($wf.status) {
    'awaiting_human' {
        try {
            Post 'human-input' @{ expected_version = $wf.state_version; text = 'Продолжить работу с учётом сохранённого состояния'; resume = $true } | Out-Null
            Write-Host 'Resume отправлен.' -ForegroundColor Green
        } catch {
            # версия уехала — перечитать и повторить один раз
            $wf = Get-Wf
            Post 'human-input' @{ expected_version = $wf.state_version; text = 'Продолжить работу с учётом сохранённого состояния'; resume = $true } | Out-Null
            Write-Host 'Resume отправлен (со второй попытки).' -ForegroundColor Green
        }
        Start-Sleep -Seconds 3
        $wf = Get-Wf
        if ($wf.status -eq 'queued') {
            try { Post 'sync' @{ expected_version = $wf.state_version } | Out-Null; Write-Host 'Sync-пинок отправлен.' } catch {}
        }
    }
    'queued' {
        try {
            Post 'sync' @{ expected_version = $wf.state_version } | Out-Null
            Write-Host 'Sync-пинок отправлен (воркер не заметил задачу).' -ForegroundColor Green
        } catch {
            $wf = Get-Wf
            Post 'sync' @{ expected_version = $wf.state_version } | Out-Null
            Write-Host 'Sync-пинок отправлен (со второй попытки).' -ForegroundColor Green
        }
    }
    default {
        Write-Host 'Ничего делать не нужно — пайплайн сам работает (executing/gating/reviewing).' -ForegroundColor Yellow
        exit 0
    }
}

Start-Sleep -Seconds 8
$wf = Get-Wf
Write-Host ("Итог         : {0} (раунд {1})" -f $wf.status, $wf.current_round) -ForegroundColor Cyan
if ($wf.status -in @('executing','gating','reviewing')) {
    Write-Host 'Поехало. Дальше само (гейт может идти до часа — это нормально).' -ForegroundColor Green
} elseif ($wf.status -eq 'queued') {
    Write-Host 'Всё ещё queued: подожди минуту и запусти этот скрипт ещё раз.' -ForegroundColor Yellow
    Write-Host 'Если и после этого queued — PromptPilot-Stop.bat, затем PromptPilot-Start.bat, затем этот скрипт.' -ForegroundColor Yellow
} else {
    Write-Host ("Остановился в {0} — открой UI http://127.0.0.1:8420 и посмотри карточку (что сказал агент)." -f $wf.status) -ForegroundColor Yellow
}
