@echo off
chcp 65001 >nul
title DG-Lab Coyote Audio-Reactive Controller
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. First run:  uv venv --python 3.12 .venv
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py %*
if errorlevel 1 pause
