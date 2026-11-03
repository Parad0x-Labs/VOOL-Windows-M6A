@echo off
setlocal
rem UTF-8 mode for every Python this launcher starts (as installer\bundle\vool-launch.cmd does):
rem without it Python on Windows reads and writes text as cp1252.
set "PYTHONUTF8=1"
set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 -m apps.vool_cli stage-trainable-base --activate %*
) else (
  python -m apps.vool_cli stage-trainable-base --activate %*
)
