@echo off
rem ============================================================
rem  Agent Gateway - background launcher
rem  Double-click or run: start.bat
rem  Starts the gateway in a MINIMIZED console window (background).
rem  Panel: http://localhost:8080  (token: .env GATEWAY_TOKEN)
rem  Gotify push service is auto-started by the gateway.
rem  Stop: panel stop button, or  run stop.bat
rem ============================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Setup first:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [WARN] .env not found, copying template from .env.example...
    copy /y ".env.example" ".env" >nul
    echo        Please edit .env: GATEWAY_TOKEN / TTYD_PATH / GOTIFY_PATH
    echo.
    pause
    exit /b 1
)

rem ---- launch the gateway in a minimized console (background) ----
start "Agent Gateway" /min gateway_run.bat

echo [OK] Agent Gateway started in background (minimized window).
echo   Panel : http://localhost:8080
echo   Log   : gateway.log  (this folder)
echo   Stop  : panel stop button, or  stop.bat
ping -n 4 127.0.0.1 >nul
