@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3.11 -c "import sys; sys.exit(sys.version_info[:2] != (3, 11))" >nul 2>nul
    if not errorlevel 1 (
        py -3.11 -B support\setup_genie.py %*
        goto finished
    )
)
python -c "import sys; sys.exit(sys.version_info[:2] != (3, 11))" >nul 2>nul
if not errorlevel 1 (
    python -B support\setup_genie.py %*
    goto finished
)
echo Install Python 3.11 from https://www.python.org/downloads/ and enable Add Python to PATH.
pause
exit /b 1
:finished
set "setup_result=%errorlevel%"
if not "%setup_result%"=="0" echo Setup stopped. Read the error above, fix it, then run setup.cmd again.
pause
exit /b %setup_result%
