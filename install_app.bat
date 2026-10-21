@echo off
rem Makes SDR Pilot a standalone app: "SDR Pilot" shortcuts on the desktop and in the Start menu, and SDR Pilot
rem starts in the background when you log in (and restarts itself if it stops). Undo: install_app.bat uninstall
cd /d "%~dp0"
set "PY=%USERPROFILE%\radioconda\python.exe"
if not exist "%PY%" set "PY=%ProgramData%\radioconda\python.exe"
if "%1"=="uninstall" (
  "%PY%" "%~dp0sdrpilot_app.py" uninstall
) else (
  "%PY%" "%~dp0sdrpilot_app.py" install
  "%PY%" "%~dp0sdrpilot_app.py" open
)
pause
