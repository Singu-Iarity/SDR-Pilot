@echo off
setlocal
title SDR Pilot remote access
cd /d "%~dp0"
set "PY=%USERPROFILE%\radioconda\python.exe"
if not exist "%PY%" set "PY=%ProgramData%\radioconda\python.exe"
if not exist "%PY%" set "PY=python"
:menu
echo.
echo  SDR Pilot - remote access over Tailscale
echo  ----------------------------------------
echo   1  Put the dashboard on my tailnet (HTTPS, recommended)
echo   2  Also put the MCP endpoint on my tailnet (for AI apps on my other devices)
echo   3  Start the SDR Pilot host automatically when I log in
echo   4  Show my phone links again
echo   5  Turn remote access off
echo   6  Quit
echo.
choice /c 123456 /n /m "Choose 1-6: "
if errorlevel 6 goto :eof
if errorlevel 5 ("%PY%" remote_setup.py off & goto menu)
if errorlevel 4 ("%PY%" remote_setup.py status & goto menu)
if errorlevel 3 ("%PY%" remote_setup.py autostart on & goto menu)
if errorlevel 2 ("%PY%" remote_setup.py serve-mcp & goto menu)
if errorlevel 1 ("%PY%" remote_setup.py serve & goto menu)
goto menu
