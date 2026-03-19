@echo off
color 0A
echo ===================================================
echo    GEO Tool - Dev Server Restart
echo ===================================================
echo.

:: Kill any existing uvicorn / python process on port 8000
echo [*] Stopping any existing server on port 8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    echo     Killing PID %%a
    taskkill /PID %%a /F >NUL 2>&1
)
echo [OK] Port 8000 cleared.
echo.

:: Small pause to ensure port is released
timeout /t 2 /nobreak > NUL

:: Start fresh server
echo [*] Starting FastAPI server...
set PYTHONUTF8=1
start "GEO Tool - Dev Server" cmd /k "C:\Users\Cosmin\AppData\Local\Python\pythoncore-3.14-64\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000"

timeout /t 4 /nobreak > NUL
echo [OK] Server started at http://127.0.0.1:8000
echo.
echo Press any key to close this window (server keeps running)...
pause > NUL
