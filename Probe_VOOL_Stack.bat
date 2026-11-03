@echo off
setlocal
rem UTF-8 mode for every Python this launcher starts (as installer\bundle\vool-launch.cmd does):
rem without it Python on Windows reads and writes text as cp1252.
set "PYTHONUTF8=1"
set "SCRIPT_DIR=%~dp0"
if exist "%SCRIPT_DIR%\.venv\Scripts\python.exe" (
  "%SCRIPT_DIR%\.venv\Scripts\python.exe" "%SCRIPT_DIR%\installer\provider_probe.py" %*
  exit /b %errorlevel%
)
py -3 "%SCRIPT_DIR%\installer\provider_probe.py" %*
exit /b %errorlevel%
