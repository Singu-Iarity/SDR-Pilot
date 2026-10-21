@echo off
setlocal
set "PY=%USERPROFILE%\radioconda\python.exe"
if not exist "%PY%" set "PY=%ProgramData%\radioconda\python.exe"
"%PY%" "%~dp0install_claude_config.py" %*
pause
