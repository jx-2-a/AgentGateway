@echo off
rem ============================================================
rem  Agent Gateway - stop by port 8080
rem  Kills the gateway AND its child processes (ttyd sessions,
rem  gotify push server). Use when the web panel is unreachable.
rem  Restart later with start.bat
rem ============================================================
echo Stopping Agent Gateway on port 8080 ...
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8080" ^| findstr "LISTENING"') do (
    echo   Killing PID %%p (and its process tree) ...
    taskkill /PID %%p /T /F
    set FOUND=1
)
if "%FOUND%"=="0" (
    echo   No gateway listening on 8080 - already stopped.
)
ping -n 3 127.0.0.1 >nul
