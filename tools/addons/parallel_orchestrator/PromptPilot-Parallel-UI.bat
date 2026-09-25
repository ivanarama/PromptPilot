@echo off
setlocal
cd /d "%~dp0..\..\.."

if exist ".venv\Scripts\python.exe" (
  set "PYTHON=.venv\Scripts\python.exe"
) else (
  set "PYTHON=python"
)

echo Starting PromptPilot Parallel visual UI...
echo Browser will open at http://127.0.0.1:8431
"%PYTHON%" -m tools.addons.parallel_orchestrator.ui_server --open
