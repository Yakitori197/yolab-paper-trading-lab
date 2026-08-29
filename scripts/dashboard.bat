@echo off
chcp 65001 >nul
cd /d "%~dp0.."

echo Dashboard: http://127.0.0.1:8787
echo Log: data\dashboard.log
py\.venv\Scripts\python.exe -m uvicorn dashboard:app --app-dir py --host 127.0.0.1 --port 8787 >> data\dashboard.log 2>&1

pause
