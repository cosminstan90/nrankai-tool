@echo off
color 0A
echo ===================================================
echo    Website LLM Analyzer - Demo Startup Script
echo ===================================================
echo.

echo [*] Starting FastAPI Backend Server in the background...
set PYTHONUTF8=1

:: Starts the python server in a new window so it runs concurrently with the tunnel
start "GEO Tool FastAPI Backend" cmd /c "C:\Users\Cosmin\AppData\Local\Python\pythoncore-3.14-64\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000"

:: Wait a few seconds for the API to boot up
timeout /t 5 /nobreak > NUL

echo [*] Starting Secure Tunnel (Cloudflare)...
echo.
echo A new URL will be generated below. Look for the link ending in .trycloudflare.com
echo and open it on your presentation laptop.
echo.

:: Start cloudflared in the current window
cmd /c ".\cloudflared.exe tunnel --url http://127.0.0.1:8000"

pause
