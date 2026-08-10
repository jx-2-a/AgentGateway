@echo off
rem ============================================================
rem  setup_autostart.bat - enable Agent Gateway auto-start at logon
rem  Creates a shortcut in the Startup folder -> hidden launch.
rem  Re-run any time to (re)enable. Remove = delete the shortcut
rem  that this script prints.
rem ============================================================
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_autostart.ps1"
echo.
pause
