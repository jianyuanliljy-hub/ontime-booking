@echo off
setlocal
cd /d "%~dp0"

py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 goto use_py
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 goto use_python

echo Python 3.9 or newer was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo Include the Python launcher or add Python to PATH during installation.
set "ONTIME_EXIT=1"
goto finish

:use_py
py -3 -X utf8 "%~dp0ticket_web.py" --open %*
set "ONTIME_EXIT=%errorlevel%"
goto finish

:use_python
python -X utf8 "%~dp0ticket_web.py" --open %*
set "ONTIME_EXIT=%errorlevel%"

:finish
if not "%ONTIME_EXIT%"=="0" echo On Time stopped with an error. See the message above.
pause
exit /b %ONTIME_EXIT%
