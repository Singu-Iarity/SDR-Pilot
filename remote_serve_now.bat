@echo off
rem One click: put the dashboard on your tailnet (tailscale serve) and save the phone link to logs\remote_access_result.txt
cd /d "%~dp0"
set "PY=%USERPROFILE%\radioconda\python.exe"
if not exist "%PY%" set "PY=%ProgramData%\radioconda\python.exe"
if not exist logs mkdir logs
"%PY%" remote_setup.py serve > logs\remote_access_result.txt 2>&1
type logs\remote_access_result.txt
timeout /t 20
