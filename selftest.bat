@echo off
set "PY=%USERPROFILE%\radioconda\python.exe"
if not exist "%PY%" set "PY=%ProgramData%\radioconda\python.exe"
"%PY%" "%~dp0selftest.py"
pause
