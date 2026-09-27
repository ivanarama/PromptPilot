@echo off
chcp 65001 >nul
title PromptPilot Update
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1"
pause
