@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=D:\Miniconda3\envs\ActionAgent\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" launcher.py --stop
echo.
pause
