@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo Stopping WorkBuddy Token Monitor...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"

echo Done.
ping -n 3 127.0.0.1 >nul
endlocal
