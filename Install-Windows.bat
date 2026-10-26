@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" goto install
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 (
  python -m venv .venv
  if errorlevel 1 goto failed
  goto install
)
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
  goto install
)
echo Python 3.9 or newer was not found.
goto failed
:install
".venv\Scripts\python.exe" -m pip install -r server_monitor\requirements-windows.txt
if errorlevel 1 goto failed
echo.
echo Installation complete. Double-click Start-Windows.bat to open the monitor.
pause
exit /b 0
:failed
echo.
echo Installation failed. Check Python and Internet access, then retry.
pause
exit /b 1
