@echo off
chcp 65001 >nul
rem Install the Windows scheduled task: run paper_tick_scheduled.bat every
rem 4 hours at :05 past the boundary (00:05 / 04:05 / 08:05 / 12:05 /
rem 16:05 / 20:05) -- five minutes after each 4h bar closes.
schtasks /create /tn "paper-lab-tick" /tr "\"%~dp0paper_tick_scheduled.bat\"" /sc hourly /mo 4 /st 00:05 /f
