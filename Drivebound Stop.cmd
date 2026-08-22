@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0tools\setup-drivebound.ps1" -Action Stop
if errorlevel 1 pause
