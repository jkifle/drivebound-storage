@echo off
setlocal
set "DriveboundNoPause="
set "DriveboundNonInteractiveArgument="
if /i "%DRIVEBOUND_NONINTERACTIVE%"=="1" set "DriveboundNoPause=1"
if /i "%DRIVEBOUND_NONINTERACTIVE%"=="1" set "DriveboundNonInteractiveArgument=-NonInteractive"

:scanArguments
if "%~1"=="" goto runDrivebound
if /i "%~1"=="-NonInteractive" set "DriveboundNoPause=1"
if /i "%~1"=="-NonInteractive" set "DriveboundNonInteractiveArgument="
shift
goto scanArguments

:runDrivebound
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0tools\remote-access.ps1" %* %DriveboundNonInteractiveArgument%
set "DriveboundExitCode=%errorlevel%"
if "%DriveboundExitCode%"=="0" goto finished

echo.
echo Drivebound Remote Access could not finish. Exit code: %DriveboundExitCode%.
echo Keep the error text above and any diagnostic log path it shows.
if defined DriveboundNoPause goto finished
echo This window will stay open so you can copy the error before retrying.
pause

:finished
endlocal & exit /b %DriveboundExitCode%
