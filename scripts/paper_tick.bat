@echo off
chcp 65001 >nul
cd /d "%~dp0.."

echo Running paper trading tick (engineering validation only)...
py\.venv\Scripts\python.exe py\paper_loop.py

echo.
pause
