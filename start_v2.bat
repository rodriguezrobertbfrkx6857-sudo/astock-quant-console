@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\v2\start_local.py
) else (
  python scripts\v2\start_local.py
)
endlocal
