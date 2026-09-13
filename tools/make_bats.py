"""生成并校验全部 .bat 启动脚本。

为什么要有这个文件：cmd 对语法错误**不报错、不提示、直接静默退出**，
用户看到的永远是「双击没反应」。手工写批处理已经栽过四次，所以统一在这里
生成并强制跑结构校验。

两条铁律（都是踩出来的）：

1. **不用嵌套代码块，一律用 goto 扁平化。**
   cmd 把 `( ... )` 整块当一条命令解析，块内任何一个未转义的 `)` 都会
   提前闭合它。典型翻车：块内写
   `echo Updating build tools (needed by pywebview)...`
   那个右括号直接把 if 块关掉了。

2. **echo 里不准出现未转义的括号。**
   在块外无害，在块内致命。与其分情况判断，不如一律禁止；
   真要显示括号就写 `^(` `^)`。

    python tools/make_bats.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 防止脚本把自己再拉起来。2026-08-23 踩过一次：结尾用 start "" "release"
# 想打开 release 文件夹，但 start 会按 PATHEXT 把裸名字当命令解析，
# 同目录的 release.bat 被它挑中，于是每打包完一次就自己重启一次，无限套娃。
# 目录已改名躲开碰撞，这道保险是二重防线 —— 环境变量会传给子进程。
REENTRY_GUARD = r'''
if not defined DSM_BUILD_RUNNING goto :notrunning
echo.
echo   This script is already running in another window.
echo   Close the other windows first, then start it once.
echo.
pause
exit /b 1
:notrunning
set "DSM_BUILD_RUNNING=1"
'''


# ---------------------------------------------------------------- 公共片段

# 挑一个能装 C 扩展包的 Python：
#  - 跳过自由线程构建 pythonXXt.exe，那些包没有对应的预编译 wheel
#  - 不能只用 `where python`：没装 Python 时 Windows 自带一个只会打开应用商店的占位程序
FIND_PY = r'''@echo off
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
'''

# 绝不往用户的全局 Python 里装东西：
#  * 2026-08-23 我们把用户的 Pillow 9.4.0 顶成了 12.3.0，可能弄坏了他装的 Real-ESRGAN
#  * 那个环境是 3.10 原地升到 3.12 的，残留的 easy_install egg（typing_extensions 4.5.0）
#    会插在 sys.path 最前面，pip 升级也盖不掉，导致 `typing.TypeVar 不可继承`
# venv 的 site-packages 是全新的，两个问题一起解决。
ENSURE_VENV = r'''
if exist ".venv\Scripts\python.exe" goto :havevenv
echo   Creating an isolated environment in .venv - one time, keeps your
echo   global Python untouched...
!PY! -m venv .venv
if errorlevel 1 goto :venverr
:havevenv
set "VPY=.venv\Scripts\python.exe"
'''

VENV_ERR = r'''
:venverr
echo.
echo   Could not create the .venv folder. Send the text above back.
pause
exit /b 1
'''

# pywebview 依赖 proxy_tools —— 只有 setup.py 的老包，必须现场构建。
# 旧 setuptools 在 Python 3.12 上会因为 pkgutil.ImpImporter 被移除而崩溃，
# 所以任何 pip install 之前都要先把构建工具链升上去。
UPGRADE_TOOLS = r'''
echo   Updating pip and setuptools, needed to build pywebview dependencies...
"%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip setuptools wheel
if errorlevel 1 goto :toolerr
'''

TOOL_ERR = r'''
:toolerr
echo.
echo   Could not update pip / setuptools. Send the text above back.
pause
exit /b 1
'''

PKG_ERR = r'''
:pkgerr
echo.
echo   Package install failed. Send the text above back.
echo   If the error mentions ImpImporter, just run this file once more -
echo   the setuptools upgrade will have taken effect by then.
pause
exit /b 1
'''

# ---------------------------------------------------------------- 各脚本

SCRIPTS = {}


def probe_script(pkgs: str, script: str, report: str) -> str:
    """一次性硬件探测脚本的 .bat 模板。

    v1.0 时把历史上那 12 支 probe 全删了 —— 探测脚本是开发过程的产物，
    测完结论就该写进常量旁边的注释，不该攒成一堆历史文件。

    这个函数本身留着，因为它是「下次要现场测硬件」时的配方：
    写好 probeN.py 放根目录，然后在下面加一行

        SCRIPTS["probeN.bat"] = probe_script("hidapi", "probeN.py", "probeN-report.txt")

    跑一次 make_bats.py 就有了。测完把那行连同 .py / .bat / 报告一起删掉。
    """
    return FIND_PY + ENSURE_VENV + UPGRADE_TOOLS + f'''
echo   Installing what the diagnostic needs...
"%VPY%" -m pip install --disable-pip-version-check -q {pkgs}
if errorlevel 1 goto :pkgerr
echo.
"%VPY%" {script}
echo.
echo   ---- finished, see {report} ----
pause
exit /b 0
''' + TOOL_ERR + PKG_ERR + VENV_ERR


# 这里原本有 probe.bat ~ probe5.bat 五行，v1.0 时连同探测脚本一起删了。
# 留着会让 make_bats.py 凭空生出对应不到 .py 的 .bat。
# 下次要加新探测，看上面 probe_script 的文档字符串。

SCRIPTS["run.bat"] = FIND_PY + ENSURE_VENV + r'''
"%VPY%" -c "import hid, webview" >nul 2>nul
if not errorlevel 1 goto :start

echo   First run - installing dependencies. This takes a couple of minutes.
''' + UPGRADE_TOOLS + r'''
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
''' + TOOL_ERR + PKG_ERR + VENV_ERR

SCRIPTS["release.bat"] = FIND_PY + REENTRY_GUARD + ENSURE_VENV + UPGRADE_TOOLS + r'''
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
''' + TOOL_ERR + PKG_ERR + VENV_ERR

SCRIPTS["check.bat"] = r'''@echo off
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
'''


# ---------------------------------------------------------------- 校验

def strip_quoted(line: str) -> str:
    """去掉引号内的内容和 ^ 转义字符，剩下的括号才是语法括号。"""
    return re.sub(r'\^.', '', re.sub(r'"[^"]*"', '', line))


def validate(raw: bytes):
    problems = []
    try:
        txt = raw.decode("ascii")
    except UnicodeDecodeError:
        return ["含非 ASCII 字节，中文提示要交给 Python 打印"]

    if raw.count(b"\n") != raw.count(b"\r\n"):
        problems.append("有 LF-only 行，cmd 需要 CRLF")

    depth = 0
    for i, line in enumerate(txt.splitlines(), 1):
        bare = strip_quoted(line)
        is_echo = bare.strip().lower().startswith("echo")

        # 铁律 2：echo 里不准有未转义括号。块外无害，块内会提前闭合代码块，
        # 这正是 2026-08-23 那次 run.bat 静默失败的原因。
        if is_echo and ("(" in bare or ")" in bare):
            problems.append(f"第 {i} 行 echo 里有未转义的括号")

        if not is_echo:
            depth += bare.count("(") - bare.count(")")
            if depth < 0:
                problems.append(f"第 {i} 行括号提前闭合")
                depth = 0
        elif depth > 0:
            # 铁律 1 的延伸：块内出现 echo 说明还是在用嵌套块，风险太高
            problems.append(f"第 {i} 行在代码块内部用了 echo，改成 goto 扁平结构")

    if depth > 0:
        problems.append(f"有 {depth} 个括号没闭合")

    labels = set(re.findall(r'^\s*:(\w+)', txt, re.M))
    for g in {g for g in re.findall(r'goto\s+:?(\w+)', txt, re.I) if g.lower() != "eof"}:
        if g not in labels:
            problems.append(f"goto :{g} 找不到标签")
    for lbl in labels:
        if not re.search(r'goto\s+:?' + lbl + r'\b', txt, re.I):
            problems.append(f"标签 :{lbl} 没有任何 goto 指向它")

    if "pause" not in txt:
        problems.append("没有 pause，出错时窗口会一闪而过")
    return problems


def main():
    bad = False
    for name, body in sorted(SCRIPTS.items()):
        raw = body.replace("\n", "\r\n").encode("ascii")
        with open(os.path.join(ROOT, name), "wb") as f:
            f.write(raw)
        problems = validate(raw)
        if problems:
            bad = True
            print(f"  [X]  {name}")
            for p in problems:
                print(f"         {p}")
        else:
            print(f"  [ok] {name:12s} {len(raw):5d} bytes")
    if bad:
        print("\n有脚本没通过校验，不要发出去。")
        sys.exit(1)
    print("\n全部通过。")


if __name__ == "__main__":
    main()
