@echo off
cd /d "D:\Projects\geo_tool"
:restart
echo [%date% %time%] Starting uvicorn... >> D:\Projects\geo_tool\uvicorn_service.log
C:\Users\Cosmin\AppData\Local\Python\pythoncore-3.14-64\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 >> D:\Projects\geo_tool\uvicorn.log 2>&1
echo [%date% %time%] Uvicorn exited with code %ERRORLEVEL%, restarting in 10s... >> D:\Projects\geo_tool\uvicorn_service.log
timeout /t 10 /nobreak > NUL
goto restart
