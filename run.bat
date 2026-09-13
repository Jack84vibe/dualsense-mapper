@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title DualSense Mapper

set "PY="
for %%C in ("py -3.12" "py -3.13" "py -3.11" "py -3.10" "py -3.9" "python" "python3" "py -3") do (
  %%~C -c "import sys,sysconfig;ok=sys.version_info[:2] in [(3,9),(3,10),(3,11),(3,12),(3,13)] and not sysconfig.get_config_var('Py_GIL_DISABLED');sys.exit(0 if ok else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=%%~C"
  if defined PY goto :found
)

echo.
echo   ================================================================
echo     No usable Python found.
echo   ================================================================
echo.
echo     What is installed on this machine:
py -0p 2>nul
echo.
echo     This project needs Python 3.10 - 3.13, a NORMAL build.
echo     A free-threaded build - exe name ends with "t" - has no wheels yet,
echo     and 3.14 is too new for hidapi / pywebview.
echo.
echo     Recommended: install Python 3.12
echo       https://www.python.org/downloads/release/python-3129/
echo       - tick "Add Python to PATH" during setup
echo       - do NOT tick any free-threaded option
echo.
pause
exit /b 1

:found
echo   Python: !PY!

if exist ".venv\Scripts\python.exe" goto :havevenv
echo   Creating an isolated environment in .venv - one time, keeps your
echo   global Python untouched...
!PY! -m venv .venv
if errorlevel 1 goto :venverr
:havevenv
set "VPY=.venv\Scripts\python.exe"

"%VPY%" -c "import hid, webview" >nul 2>nul
if not errorlevel 1 goto :start

echo   First run - installing dependencies. This takes a couple of minutes.

echo   Updating pip and setuptools, needed to build pywebview dependencies...
"%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip setuptools wheel
if errorlevel 1 goto :toolerr

"%VPY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :pkgerr

:start
echo   Starting...
"%VPY%" main.py
if errorlevel 1 goto :apperr
exit /b 0

:apperr
echo.
echo   The app exited with an error. Send the text above back.
pause
exit /b 1

:toolerr
echo.
echo   Could not update pip / setuptools. Send the text above back.
pause
exit /b 1

:pkgerr
echo.
echo   Package install failed. Send the text above back.
echo   If the error mentions ImpImporter, just run this file once more -
echo   the setuptools upgrade will have taken effect by then.
pause
exit /b 1

:venverr
echo.
echo   Could not create the .venv folder. Send the text above back.
pause
exit /b 1
