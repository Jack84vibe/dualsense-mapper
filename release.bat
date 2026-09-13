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

if not defined DSM_BUILD_RUNNING goto :notrunning
echo.
echo   This script is already running in another window.
echo   Close the other windows first, then start it once.
echo.
pause
exit /b 1
:notrunning
set "DSM_BUILD_RUNNING=1"

if exist ".venv\Scripts\python.exe" goto :havevenv
echo   Creating an isolated environment in .venv - one time, keeps your
echo   global Python untouched...
!PY! -m venv .venv
if errorlevel 1 goto :venverr
:havevenv
set "VPY=.venv\Scripts\python.exe"

echo   Updating pip and setuptools, needed to build pywebview dependencies...
"%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip setuptools wheel
if errorlevel 1 goto :toolerr

echo.
echo   [1/5] Installing build dependencies into .venv ...
echo         Nothing is installed system-wide.
"%VPY%" -m pip install --disable-pip-version-check -r requirements.txt pyinstaller
if errorlevel 1 goto :pkgerr

echo.
echo   [2/5] Cleaning previous output ...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "output" rmdir /s /q "output"
if exist "release" rmdir /s /q "release"
if exist "DualSenseMapper.spec" del /q "DualSenseMapper.spec"

echo.
echo   [3/5] Building. This takes a few minutes ...
"%VPY%" -m PyInstaller --noconfirm --clean --name DualSenseMapper --windowed --add-data "ps5mapper\ui;ps5mapper\ui" --add-data "ps5mapper\default_config.json;ps5mapper" --hidden-import hid --hidden-import pystray._win32 --collect-all webview main.py
if errorlevel 1 goto :builderr
if not exist "dist\DualSenseMapper\DualSenseMapper.exe" goto :builderr

echo.
echo   [4/5] Assembling a clean folder ...
mkdir "output\DualSenseMapper"
xcopy /e /i /q /y "dist\DualSenseMapper" "output\DualSenseMapper" >nul
if errorlevel 1 goto :builderr
copy /y "*.md" "output\DualSenseMapper\" >nul 2>nul

echo.
echo   [5/5] Zipping ...
powershell -NoProfile -Command "Compress-Archive -Path 'output\DualSenseMapper' -DestinationPath 'output\DualSenseMapper.zip' -Force"
if errorlevel 1 goto :ziperr

rmdir /s /q "build"
rmdir /s /q "dist"
del /q "DualSenseMapper.spec"

echo.
echo   ================================================================
echo     Done.
echo       output\DualSenseMapper.zip   - hand this around
echo       output\DualSenseMapper\     - the same thing, unzipped
echo.
echo     Extract anywhere and double-click DualSenseMapper.exe.
echo     No Python, no installer, nothing written outside the folder.
echo   ================================================================
explorer "%~dp0output"
pause
exit /b 0

:ziperr
echo.
echo   Zipping failed, but output\DualSenseMapper\ is ready - zip it yourself.
pause
exit /b 1

:builderr
echo.
echo   Build failed. Send the text above back.
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
