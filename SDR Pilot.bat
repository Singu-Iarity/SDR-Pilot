@echo off
rem SDR Pilot without the Claude app: starts SDR Pilot (dashboard + dashboard agent) if it isn't running and opens
rem the dashboard in its own window. "install_app.bat" adds desktop / Start-menu shortcuts and starts it at logon.
cd /d "%~dp0"
set "PY=%USERPROFILE%\radioconda\pythonw.exe"
if not exist "%PY%" set "PY=%ProgramData%\radioconda\pythonw.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\radioconda\python.exe"
start "" "%PY%" "%~dp0sdrpilot_app.py"
