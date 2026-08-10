@echo off
rem ============================================================
rem  Gateway runner - invoked by start.bat in a minimized console.
rem  Logs to gateway.log in this folder (overwrite on each start).
rem  python -u = unbuffered, so prints flush to the log immediately.
rem ============================================================
cd /d "%~dp0"
echo [Agent Gateway] started at %date% %time% > gateway.log
.venv\Scripts\python.exe -u bot.py >> gateway.log 2>&1
