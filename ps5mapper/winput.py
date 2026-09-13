"""Windows 输入注入（SendInput 封装）。

只依赖 ctypes，不需要额外的包。按键名解析部分是纯 Python，
所以在非 Windows 上也能导入并做单元测试。
"""
from __future__ import annotations

import platform
import sys
import time

IS_WINDOWS = sys.platform == "win32" or platform.system() == "Windows"

# ---------------------------------------------------------------- 键名 → VK

_SIMPLE = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0xA0, "ctrl": 0xA2, "control": 0xA2, "alt": 0xA4, "menu": 0xA4,
    "win": 0x5B, "meta": 0x5B, "cmd": 0x5B, "super": 0x5B,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "spacebar": 0x20,
    "printscreen": 0x2C, "scrolllock": 0x91, "numlock": 0x90,
    "backquote": 0xC0, "`": 0xC0, "minus": 0xBD, "-": 0xBD,
    "equal": 0xBB, "=": 0xBB, "bracketleft": 0xDB, "[": 0xDB,
    "bracketright": 0xDD, "]": 0xDD, "backslash": 0xDC, "\\": 0xDC,
    "semicolon": 0xBA, ";": 0xBA, "quote": 0xDE, "'": 0xDE,
    "comma": 0xBC, ",": 0xBC, "period": 0xBE, ".": 0xBE,
    "slash": 0xBF, "/": 0xBF,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD,
    "medianext": 0xB0, "mediaprev": 0xB1, "mediastop": 0xB2, "mediaplaypause": 0xB3,
    "numpadmultiply": 0x6A, "numpadadd": 0x6B, "numpadsubtract": 0x6D,
    "numpaddecimal": 0x6E,
}

# 需要 KEYEVENTF_EXTENDEDKEY 的键（小键盘之外的方向/编辑键、右侧修饰键等）
_EXTENDED = {
    0x21, 0x22, 0x23, 0x24,            # PageUp PageDown End Home
    0x25, 0x26, 0x27, 0x28,            # ← ↑ → ↓
    0x2D, 0x2E,                        # Insert Delete
    0x5B, 0x5C,                        # LWin RWin
    0x6F,                              # Numpad /
    0xA3, 0xA5,                        # RControl RAlt
    0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3,
}

_ARROWS = {"left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
           "arrowleft": 0x25, "arrowup": 0x26, "arrowright": 0x27, "arrowdown": 0x28,
           "←": 0x25, "↑": 0x26, "→": 0x27, "↓": 0x28}

_NAV = {"pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
        "insert": 0x2D, "delete": 0x2E, "del": 0x2E}

_MODIFIER_VK = {"ctrl": 0xA2, "control": 0xA2, "alt": 0xA4,
                "shift": 0xA0, "win": 0x5B, "meta": 0x5B, "cmd": 0x5B}


def key_to_vk(name: str):
    """把单个键名转成虚拟键码。认不出来返回 None。"""
    if not name:
        return None
    n = name.strip()
    low = n.lower()
    if low in _ARROWS:
        return _ARROWS[low]
    if low in _NAV:
        return _NAV[low]
    if low in _SIMPLE:
        return _SIMPLE[low]
    if len(n) == 1 and n.isalnum():
        return ord(n.upper())
    if low.startswith("f") and low[1:].isdigit():
        i = int(low[1:])
        if 1 <= i <= 24:
            return 0x70 + i - 1
    if low.startswith("numpad") and low[6:].isdigit():
        return 0x60 + int(low[6:])
    return None


def parse_combo(text: str):
    """"Ctrl+Shift+K" → ([修饰键 VK...], 主键 VK)。解析失败返回 (None, None)。"""
    if not text:
        return None, None
    parts = [p.strip() for p in str(text).replace("＋", "+").split("+") if p.strip()]
    if not parts:
        return None, None
    mods = []
    main = None
    last = len(parts) - 1
    for i, p in enumerate(parts):
        low = p.lower()
        if i < last and low in _MODIFIER_VK:
            mods.append(_MODIFIER_VK[low])
            continue
        vk = key_to_vk(p)
        if vk is None and low in _MODIFIER_VK:
            vk = _MODIFIER_VK[low]
        if vk is not None and main is None:
            main = vk
    if main is None:
        return None, None
    return mods, main


# ---------------------------------------------------------------- SendInput

WHEEL_DELTA = 120

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                    ("wParamH", ctypes.c_ushort)]

    class _IUnion(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_ulong), ("u", _IUnion)]

    INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
    KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x0004, 0x0008
    MOUSEEVENTF = {
        "move": 0x0001, "ldown": 0x0002, "lup": 0x0004,
        "rdown": 0x0008, "rup": 0x0010, "mdown": 0x0020, "mup": 0x0040,
        "xdown": 0x0080, "xup": 0x0100, "wheel": 0x0800, "hwheel": 0x1000,
    }

    # Windows 默认的定时器精度是 15.6ms —— time.sleep(0.004) 实际会睡 15ms 以上，
    # 250Hz / 120Hz 的循环全都被压到 64Hz，指针会明显发卡。
    # timeBeginPeriod(1) 把精度抬到 1ms，游戏和播放器都是这么做的。必须成对调用。
    _winmm = ctypes.WinDLL("winmm")

    def begin_high_res_timer():
        try:
            _winmm.timeBeginPeriod(1)
            return True
        except Exception:
            return False

    def end_high_res_timer():
        try:
            _winmm.timeEndPeriod(1)
        except Exception:
            pass

    # 高精度可等待定时器：不受系统定时器精度影响，程序不在前台时照样准。
    # Windows 10 1803 起支持；拿不到就退回 time.sleep。
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
    _TIMER_ALL_ACCESS = 0x1F0003
    try:
        _kernel32.CreateWaitableTimerExW.argtypes = (
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong)
        _kernel32.CreateWaitableTimerExW.restype = ctypes.c_void_p
        _kernel32.SetWaitableTimer.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_longlong), ctypes.c_long,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)
        _kernel32.SetWaitableTimer.restype = ctypes.c_int
        _kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        _kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        _kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    except Exception:
        pass

    def make_precise_timer():
        try:
            h = _kernel32.CreateWaitableTimerExW(
                None, None, _CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, _TIMER_ALL_ACCESS)
            return h or None
        except Exception:
            return None

    def precise_sleep(handle, seconds: float):
        if seconds <= 0:
            return
        if handle:
            try:
                # 负值 = 相对时间，单位 100 纳秒
                due = ctypes.c_longlong(-int(seconds * 10_000_000))
                if _kernel32.SetWaitableTimer(handle, ctypes.byref(due), 0,
                                              None, None, False):
                    _kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
                    return
            except Exception:
                pass
        time.sleep(seconds)

    def close_precise_timer(handle):
        if handle:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
    _user32.SendInput.restype = ctypes.c_uint
    _user32.MapVirtualKeyW.argtypes = (ctypes.c_uint, ctypes.c_uint)
    _user32.MapVirtualKeyW.restype = ctypes.c_uint

    def _send(items):
        if not items:
            return
        n = len(items)
        arr = (INPUT * n)(*items)
        _user32.SendInput(n, arr, ctypes.sizeof(INPUT))

    def _key_input(vk: int, up: bool) -> "INPUT":
        flags = KEYEVENTF_KEYUP if up else 0
        if vk in _EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        scan = _user32.MapVirtualKeyW(vk, 0)
        return INPUT(type=INPUT_KEYBOARD,
                     ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0))

    def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> "INPUT":
        return INPUT(type=INPUT_MOUSE,
                     mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF,
                                   dwFlags=flags, time=0, dwExtraInfo=0))

else:  # 非 Windows：全部变成空操作，方便在别的系统上跑测试
    def begin_high_res_timer():
        return False

    def end_high_res_timer():
        pass

    def make_precise_timer():
        return None

    def precise_sleep(handle, seconds: float):
        if seconds > 0:
            time.sleep(seconds)

    def close_precise_timer(handle):
        pass

    def _send(items):
        pass

    def _key_input(vk, up):
        return ("key", vk, up)

    def _mouse_input(flags, dx=0, dy=0, data=0):
        return ("mouse", flags, dx, dy, data)

    MOUSEEVENTF = {
        "move": 0x0001, "ldown": 0x0002, "lup": 0x0004,
        "rdown": 0x0008, "rup": 0x0010, "mdown": 0x0020, "mup": 0x0040,
        "xdown": 0x0080, "xup": 0x0100, "wheel": 0x0800, "hwheel": 0x1000,
    }


# ---------------------------------------------------------------- 对外接口

def key_down(vk: int):
    _send([_key_input(vk, False)])


def key_up(vk: int):
    _send([_key_input(vk, True)])


def combo_down(mods, main: int):
    _send([_key_input(m, False) for m in (mods or [])] + [_key_input(main, False)])


def combo_up(mods, main: int):
    _send([_key_input(main, True)] + [_key_input(m, True) for m in reversed(mods or [])])


def tap_combo(mods, main: int):
    combo_down(mods, main)
    combo_up(mods, main)


def mouse_move(dx: int, dy: int):
    if dx or dy:
        _send([_mouse_input(MOUSEEVENTF["move"], int(dx), int(dy))])


def cursor_pos():
    """当前光标坐标。拿不到就返回 None，调用方要能接受这种情况。"""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        p = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(p)):
            return p.x, p.y
    except Exception:
        pass
    return None


def screen_size():
    """主显示器的逻辑分辨率。拿不到就给一个保守的默认值。"""
    if not IS_WINDOWS:
        return (1920, 1080)
    try:
        import ctypes
        u = ctypes.windll.user32
        w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        if w > 0 and h > 0:
            return (w, h)
    except Exception:
        pass
    return (1920, 1080)


def mouse_button(which: str, down: bool):
    key = {"left": "l", "right": "r", "middle": "m"}.get(which, "l")
    _send([_mouse_input(MOUSEEVENTF[key + ("down" if down else "up")])])


def mouse_click(which: str = "left"):
    mouse_button(which, True)
    mouse_button(which, False)


def wheel(clicks: float):
    n = int(round(clicks * WHEEL_DELTA))
    if n:
        _send([_mouse_input(MOUSEEVENTF["wheel"], data=n if n >= 0 else (n & 0xFFFFFFFF))])


def hwheel(clicks: float):
    n = int(round(clicks * WHEEL_DELTA))
    if n:
        _send([_mouse_input(MOUSEEVENTF["hwheel"], data=n if n >= 0 else (n & 0xFFFFFFFF))])
