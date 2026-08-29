@echo off
chcp 65001 >nul
cd /d "%~dp0.."

echo ==== TICK START %date% %time% ==== >> data\tick.log
py\.venv\Scripts\python.exe py\paper_loop.py >> data\tick.log 2>&1
py\.venv\Scripts\python.exe tools\export_trades.py >> data\tick.log 2>&1
