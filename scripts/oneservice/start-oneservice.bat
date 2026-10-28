@echo off
chcp 65001 >nul 2>&1
title Oneservice Pipeline

echo === Oneservice Pipeline ===
echo.

:: Убить старые процессы конвейера (чтобы не было дублей)
echo [0/4] Остановка старых процессов...
taskkill /f /fi "WINDOWTITLE eq OS-*" >nul 2>&1

cd /d "C:\Projects\PromptPilot"

echo [1/4] PP server (:8420)...
start /min "OS-Server" cmd /c "py -3.11 -m promptpilot server"

timeout /t 3 >nul 2>&1

echo [2/4] PP worker...
start /min "OS-Worker" cmd /c "py -3.11 -m promptpilot worker"

timeout /t 3 >nul 2>&1

echo [3/4] Email intake (почта → GitLab issues)...
start /min "OS-Email" cmd /c "py -3.11 scripts\oneservice\os_intake.py email"

echo [4/4] TG bot команды...
start /min "OS-TGBot" cmd /c "py -3.11 scripts\oneservice\os_intake.py tg"

echo.
echo === Всё запущено. Свёрнутые окна в панели задач ===
echo.
echo Панель управления: http://127.0.0.1:8420
echo Дашборд-айсберг: scripts\oneservice\os_iceberg.html
echo Книга предложений: scripts\oneservice\outbox\
echo.
echo Для остановки: закрой окна OS-* или taskkill /f /im python.exe
pause
