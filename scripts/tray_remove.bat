@echo off
chcp 65001 >nul
schtasks /delete /tn "paper-lab-dashboard" /f
echo NOTE: if the tray icon is currently running, close it via its right-click
echo menu ("close dashboard") -- this only removes the logon task, it does not
echo stop a running icon.
