@echo off
chcp 65001 >nul
rem Install the system-tray dashboard as a logon task, then start it now.
rem The tray icon starts/attaches the uvicorn server on 127.0.0.1:8787.
schtasks /create /f /tn "paper-lab-dashboard" /sc onlogon /tr "\"%~dp0..\py\.venv\Scripts\pythonw.exe\" \"%~dp0..\py\tray_app.py\""
schtasks /run /tn "paper-lab-dashboard"
