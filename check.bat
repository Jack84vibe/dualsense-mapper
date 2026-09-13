@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title DualSense Mapper - check

echo.
echo   ---- every Python installed on this machine ----
py -0p 2>nul
if errorlevel 1 where python 2>nul

echo.
echo   ---- picking a usable one ----
set "PY="
for %%C in ("py -3.12" "py -3.13" "py -3.11" "py -3.10" "py -3.9" "python" "python3" "py -3") do (
  %%~C -c "import sys,sysconfig;ok=sys.version_info[:2] in [(3,9),(3,10),(3,11),(3,12),(3,13)] and not sysconfig.get_config_var('Py_GIL_DISABLED');sys.exit(0 if ok else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=%%~C"
  if defined PY goto :found
)
echo     NONE usable. Need a NORMAL Python 3.10-3.13 build.
echo     https://www.python.org/downloads/release/python-3129/
echo.
pause
exit /b 1

:found
echo     using: !PY!
!PY! -c "import sys,sysconfig;print('     version    :',sys.version.split()[0]);print('     exe        :',sys.executable);print('     gil        :','disabled BAD' if sysconfig.get_config_var('Py_GIL_DISABLED') else 'normal good')"
!PY! -c "import setuptools,pip;print('     pip        :',pip.__version__);print('     setuptools :',setuptools.__version__)" 2>nul
if errorlevel 1 echo      setuptools : MISSING or broken

echo.
echo   ---- project environment ----
set "VPY=.venv\Scripts\python.exe"
if exist "%VPY%" goto :havevenv
echo     .venv does not exist yet - run.bat will create it
goto :done
:havevenv
echo     .venv found
"%VPY%" -c "import sys;print('     venv python:',sys.version.split()[0])"
for %%M in (hid webview pystray PIL pycaw comtypes) do (
  "%VPY%" -c "import %%M" >nul 2>nul
  if errorlevel 1 echo     [ ] %%M    NOT installed
  if not errorlevel 1 echo     [x] %%M    ok
)

echo.
echo   ---- controller ----
"%VPY%" -c "import hid;d=[i for i in hid.enumerate(0x054C,0) if i['product_id'] in (0x0CE6,0x0DF2)];print('     DualSense devices found:',len(d));[print('      -',x.get('product_string'),hex(x['product_id'])) for x in d]" 2>nul
if errorlevel 1 echo     could not check - hidapi not installed in .venv yet
:done

echo.
pause
