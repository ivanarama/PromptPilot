@echo off
chcp 65001 >nul
title PromptPilot Verdict-Repair Watcher (TypeSafe Jev)
python -X utf8 "%~dp0verdict-repair-watcher.py"
pause
