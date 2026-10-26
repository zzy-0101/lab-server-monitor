@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\launch_monitor.py
  goto done
)
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 (
  python tools\launch_monitor.py
  goto done
)
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 (
  py -3 tools\launch_monitor.py
  goto done
)
echo Python 3.9 or newer was not found. Install Python, then try again.
pause
exit /b 1
:done
if errorlevel 1 pause
