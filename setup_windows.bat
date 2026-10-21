@echo off
setlocal
title SDR Pilot setup
set "PY=%USERPROFILE%\radioconda\python.exe"
if not exist "%PY%" set "PY=%ProgramData%\radioconda\python.exe"
if not exist "%PY%" (
  echo Could not find radioconda's python.exe.
  echo Install radioconda from https://github.com/ryanvolz/radioconda or edit PY in this file.
  pause
  exit /b 1
)
"%PY%" "%~dp0setup_sdrpilot.py"
pause
