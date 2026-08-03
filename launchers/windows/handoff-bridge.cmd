@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "BRIDGE_ROOT=%%~fI"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%BRIDGE_ROOT%\handoff_desktop.py" %*
  exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python "%BRIDGE_ROOT%\handoff_desktop.py" %*
  exit /b %ERRORLEVEL%
)

echo Python 3 was not found. Install Python 3, then run this launcher again.
pause
exit /b 1
