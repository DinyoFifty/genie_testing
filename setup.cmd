@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0support\setup_windows.ps1" %*
set "setup_result=%errorlevel%"
if not "%setup_result%"=="0" echo Setup stopped. Read the error above, fix it, then run setup.cmd again.
pause
exit /b %setup_result%
