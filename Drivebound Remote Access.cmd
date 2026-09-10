@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0tools\remote-access.ps1"
if errorlevel 1 pause
endlocal
