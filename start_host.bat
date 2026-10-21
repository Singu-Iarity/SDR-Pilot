@echo off
rem Starts the SDR Pilot host in the background: the dashboard (port 8765), the dashboard agent,
rem and MCP over HTTP (port 8770) - so the dashboard is reachable even when the Claude app is closed.
cd /d "%~dp0"
set "PY=%USERPROFILE%\radioconda\pythonw.exe"
if not exist "%PY%" set "PY=%ProgramData%\radioconda\pythonw.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\radioconda\python.exe"
start "SDR Pilot host" /min "%PY%" "%~dp0run_server.py" --host
echo SDR Pilot host started. Dashboard: http://127.0.0.1:8765/  (stop it from the dashboard: Settings - stop the agent host)
timeout /t 4 >nul
