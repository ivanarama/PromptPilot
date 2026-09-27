@echo off
chcp 65001 >nul
title PromptPilot start-all
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1"
pause
