"""配置读写。绿色免安装：配置文件就放在 exe 旁边。"""
from __future__ import annotations

import copy
import json
import os
import sys

from . import lights

CONFIG_NAME = "ps5mapper.config.json"


def app_dir() -> str:
    """打包后是 exe 所在目录，源码运行时是项目根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path() -> str:
    return os.path.join(app_dir(), CONFIG_NAME)


# ---------------------------------------------------------------- 动作构造

def combo(keys: str, label: str, repeat: bool = None) -> dict:
    """repeat=False 用于「按一次就该只发生一次」的动作，比如唤出任务视图——
    长按连发会把它反复重开。"""
    act = {"type": "combo", "keys": keys, "label": label}
    if repeat is not None:
        act["repeat"] = repeat
    return act


def mouse_btn(button: str, label: str) -> dict:
    return {"type": "mouse", "button": button, "label": label}


def builtin(name: str, label: str) -> dict:
    return {"type": "builtin", "name": name, "label": label}


NONE_ACTION = {"type": None, "label": None}

BUILTINS = {
    "pause_toggle": "暂停 / 恢复映射",
    "profile_next": "切换配置档",
    "audio_switch": "切换默认录音设备",
    "slow_pointer": "按住时指针减速",
    "double_click": "双击",
    "wheel_up": "滚轮上",
    "wheel_down": "滚轮下",
    "toggle_lights": "开关手柄灯光",
}


# ---------------------------------------------------------------- 默认配置

def default_trigger(depth=62, force=180, binding=None) -> dict:
    return {
        "enabled": True,
        "mode": "single",          # single | dual
        "preset": "wall",
        "depth": depth,            # 阻力墙起点 %（不是按键触发点！）
        "depth2": 85,              # 第二段的墙（双段模式）
        # 按键真正触发的判定：wall = 按穿整道墙才触发（默认）
        #                    custom = 用 fire_depth 自己定
        #                    bottom = 按到底才触发
        "fire_mode": "wall",
        "fire_depth": 75,          # fire_mode=custom 时第一段的触发深度 %
        "fire_depth2": 95,         # fire_mode=custom 时第二段的触发深度 %
        "force": force,            # 0..255 阻力硬度
        "hysteresis": 38,          # 回弹到此深度以下才允许再次触发
        "haptic": False,           # 触发瞬间震一下
        "binding": binding or copy.deepcopy(NONE_ACTION),
        "binding2": copy.deepcopy(NONE_ACTION),
    }


def default_stick(mode="mouse") -> dict:
    return {
        "mode": mode,              # mouse | scroll | dpad | off
        # 实测这只手柄静息漂移只有 1~2%，8% 足够安全，比原来的 11% 跟手
        "deadzone": 8,             # %
        "speed": 12,               # 1..20
        "curve": 2.0,              # 加速曲线指数
        "horizontal_scroll": True,
        "invert_scroll": False,        # 上下
        "invert_hscroll": False,       # 左右
    }


def default_touchpad() -> dict:
    return {
        "pointer_enabled": True,
        "sensitivity": 6,          # 1..10
        "acceleration": True,
        "two_finger_scroll": True,
        # 上下和左右各有各的方向。以前只有一个设置，而且只管上下，
        # 左右的方向是写死的，想反过来都没办法。
        "natural_scroll": True,        # 上下
        "natural_hscroll": True,       # 左右
        "scroll_speed": 5,
        "pinch_zoom": True,
        "edge_slider": True,
        "edge_side": "right",      # left | right
        "edge_target": "volume",   # volume | brightness | scroll
        "edge_width": 12,          # %
        # -- 手势：照抄笔记本触摸板的行为 --
        "tap_to_click": True,          # 单指轻点 = 左键（不用真的压下去）
        "two_finger_tap_right": True,  # 双指轻点 = 右键
        "tap_drag": True,              # 轻点一下再按住 = 拖动
        # -- 下压防漂移 --
        "click_stabilize": True,
        # smart = 只倒回又慢又短、看着像是压手指压出来的那段位移
        # always = 一律倒回；off = 不倒回，只锁定
        "rewind_mode": "smart",
        "click_gate": 6,               # 拖动门槛，0 = 按下就能立刻拖
    }


def default_gyro() -> dict:
    """陀螺仪指针。默认值来自 probe10 在真手柄上的实测，不是估的。

    默认**关着**：陀螺仪一开就是手一动光标就跑，没准备好的时候很吓人。
    用组合键开关（和暂停映射同一套机制），组合键本身可以改。
    """
    return {
        "enabled": False,
        # 开关组合键。和 pause_combo 一样，按下即切换，松开复位。
        # L2+R2 同时按到底。两个扳机一起按下时，它们各自的绑定（默认是
        # 鼠标左右键）会被整个屏蔽，所以不会顺手点出两下鼠标。
        "toggle_combo": ["l2", "r2"],
        # 每转 1 度光标走多少像素。25 是由实测的自然横扫幅度反推的：
        # 一次舒服的横扫约 75 度，正好扫过 1920 像素。上限放到 150。
        "sensitivity": 25.0,
        "vertical_sensitivity": 25.0,
        # 两个轴用同一个灵敏度。**不相等会直接把斜向的方向感弄歪**：
        # 水平 80、垂直 50 的话，你做一个标准的 45 度斜向动作，光标走出来
        # 是 atan(50/80) ≈ 32 度，比你想的平得多。默认锁定，要分开调再解锁。
        "link_sensitivity": True,
        # 坐标系：world = 用重力把转动换算到屏幕的左右上下，手柄歪着拿也对；
        # local = 用手柄自己的偏航/俯仰，手柄一侧倾，斜向就跟着歪。
        # 端正状态下两者完全一致，所以切换不会影响已调好的参数。
        "space": "world",
        # 垂直轴：relative = 和水平一样看转速；absolute = 用重力算俯仰角，
        # 手柄抬多高光标就在多高，不会漂。两种手感差别很大，自己切着试。
        "vertical_mode": "relative",   # relative | absolute
        # 绝对模式下，俯仰角从 -X 度到 +X 度对应屏幕从上到下
        "absolute_range": 35.0,
        # 抖动压制阈值（度/秒）。传感器本底只有 0.37，但握在手里会抖得多。
        # 1.5 实测偏低（「手轻轻抖一下鼠标就晃」），提到 3.0。
        # 界面上有实时角速度，照着自己的抖动调。
        "tremor": 3.0,
        # 平滑帧数。上报 586Hz，4 帧约 7 毫秒。
        "smoothing": 4,
        # 加速强度 0~100。慢速转动降增益、快速转动给全增益。
        # 这是解开「要够快」和「不要抖」的关键：固定增益下两者绑死，
        # 灵敏度调高多少，手抖就放大多少。
        "accel": 60,
        # 方向。probe10 没能测出符号（每段都做了来回两个方向），
        # 所以默认按常见约定，方向反了在界面上点一下就好。
        "invert_x": False,
        "invert_y": False,
    }


def _factory_profile(name="日常桌面", lightbar=(40, 110, 255)) -> dict:
    return {
        "name": name,
        # 注意语义：这不是「这个档常驻的颜色」，而是**切到这个档时闪的提示色**。
        # 常驻颜色是全局的，在 cfg["lights"]["color"]。
        "lightbar": list(lightbar),
        "buttons": {
            "cross": combo("Enter", "确认"),
            "circle": combo("Delete", "删除"),
            "square": combo("Backspace", "退格"),
            "triangle": combo("Esc", "退出"),
            "dpad_up": combo("Up", "上箭头"),
            "dpad_down": combo("Down", "下箭头"),
            "dpad_left": combo("Left", "左箭头"),
            "dpad_right": combo("Right", "右箭头"),
            "l1": combo("Ctrl+C", "复制"),
            "r1": combo("Ctrl+V", "粘贴"),
            "l3": builtin("slow_pointer", "指针减速"),
            "r3": copy.deepcopy(NONE_ACTION),
            "create": builtin("profile_next", "切换配置档"),
            "options": combo("Win+Tab", "任务视图", repeat=False),
            "ps": copy.deepcopy(NONE_ACTION),
            "touchpad_click": copy.deepcopy(NONE_ACTION),
            "mic": combo("Win+H", "语音输入", repeat=False),
        },
        "triggers": {
            # 墙起点要比「想在哪触发」浅一档：weapon 的墙跨 3 个区，
            # 按键在按穿墙时才触发，所以 45% 的墙 → 70% 触发，25% 的墙 → 50% 触发。
            # 左键用得最多，给它最浅的触发点。
            "l2": default_trigger(45, 180, mouse_btn("right", "鼠标右键")),
            "r2": default_trigger(25, 180, mouse_btn("left", "鼠标左键")),
        },
        "sticks": {
            "left": default_stick("mouse"),
            "right": default_stick("scroll"),
        },
        "touchpad": default_touchpad(),
        "gyro": default_gyro(),
    }


def _factory_config() -> dict:
    chat = _factory_profile("聊天打字", (0, 200, 190))
    chat["buttons"]["mic"] = combo("Win+H", "语音输入", repeat=False)
    chat["buttons"]["cross"] = combo("Enter", "发送")
    video = _factory_profile("看片模式", (170, 80, 255))
    video["buttons"]["cross"] = combo("Space", "暂停 / 播放")
    video["buttons"]["dpad_left"] = combo("Left", "后退")
    video["buttons"]["dpad_right"] = combo("Right", "前进")
    video["sticks"]["left"]["mode"] = "off"
    return {
        "version": 1,
        "active_profile": 0,
        # 界面语言。简体是源语言（界面代码里写的就是简体），
        # 繁体和英文查 ui/i18n.js 那份词典。
        "language": "zh-CN",          # zh-CN | zh-TW | en
        "profiles": [_factory_profile(), chat, video],
        "pause_combo": ["l1", "r1", "touchpad_click"],
        "pause_dims_lightbar": True,
        "lights": lights.default_lights(),
        "keyboard_pause_hotkey": "",
        "start_minimized": False,
        "autostart": False,
        "adaptive_triggers_master": True,
        "conflict_warning": True,
        # 长按连发：SendInput 注入的按键不会触发 Windows 的自动重复，
        # 只能自己按住时补发 key-down，参数对齐键盘的典型手感
        "key_repeat": True,
        "key_repeat_delay": 400,   # 按住多久开始连发（毫秒）
        "key_repeat_rate": 25,     # 每秒补发多少次
        "poll_hz": 250,
        "pointer_hz": 240,
        "on_disconnect": "wait",       # wait | notify | quit
        "shared_hid": True,
        "block_audio_autoswitch": True,
        "audio_devices": [],
    }


# ------------------------------------------------------- 烘焙进来的出厂默认
#
# tools/bake_defaults.py 会把一份实际在用的配置写成 ps5mapper/default_config.json，
# 打包时一起塞进 exe。有它的时候，「出厂默认」就是它，而不是上面那套通用值。
# 找不到就退回上面的通用默认——源码直接跑、或者有人把这个文件删了，都还能用。

BUNDLED_NAME = "default_config.json"
_bundled_cache = {"loaded": False, "data": None}


def _bundled_candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.join(here, BUNDLED_NAME)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield os.path.join(meipass, "ps5mapper", BUNDLED_NAME)
        yield os.path.join(meipass, BUNDLED_NAME)


def bundled_default(reload: bool = False):
    """返回烘焙进来的出厂配置，没有则 None。"""
    if _bundled_cache["loaded"] and not reload:
        return _bundled_cache["data"]
    data = None
    for path in _bundled_candidates():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            break
        except (OSError, ValueError):
            continue
    if not isinstance(data, dict) or not data.get("profiles"):
        data = None                      # 半个配置比没有更糟，宁可退回通用默认
    _bundled_cache.update(loaded=True, data=data)
    return data


def default_profile(name="日常桌面", lightbar=(40, 110, 255)) -> dict:
    """新建 / 重置配置档时的模板。"""
    tpl = _factory_profile(name, lightbar)
    b = bundled_default()
    if b:
        # 用烘焙进来的第一个档当模板，但名字和提示色照传进来的走
        tpl = _merge(tpl, b["profiles"][0])
        tpl["name"] = name
        tpl["lightbar"] = list(lightbar)
    return tpl


def default_config() -> dict:
    base = _factory_config()
    b = bundled_default()
    if not b:
        return base
    cfg = _merge(base, b)
    # 配置档要逐个补默认值，不能整体覆盖——否则以后新加的键在老的烘焙文件里会缺
    cfg["profiles"] = [_merge_profile(_factory_profile(), p) for p in b["profiles"]]
    cfg["active_profile"] = 0
    return cfg


# ---------------------------------------------------------------- 读写

def _merge(base: dict, override: dict) -> dict:
    """用 override 覆盖 base，缺失的键保留 base 的默认值。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _merge_profile(tpl: dict, p: dict) -> dict:
    """合并一个配置档，但**动作字典整体替换，不逐字段合并**。

    这是个真实的 bug 源头：不同类型的动作字段不一样（combo 有 keys，
    mouse 有 button，builtin 有 name）。逐字段合并的话，模板里那个 combo 的
    keys 会留在用户改成 mouse 之后的动作里，攒成
      {"type":"mouse","keys":"Ctrl+C","label":"鼠标左键","button":"left"}
    这种东西。运行时 type 说了算所以不出错，但配置文件会越来越脏，
    读起来还让人以为绑错了。用户 2026-08-23 那份配置里就攒了一堆。
    """
    out = _merge(tpl, p)
    for cid, act in (p.get("buttons") or {}).items():
        if isinstance(act, dict):
            out["buttons"][cid] = copy.deepcopy(act)
    for tid, t in (p.get("triggers") or {}).items():
        if not isinstance(t, dict) or tid not in out.get("triggers", {}):
            continue
        for k in ("binding", "binding2"):
            if isinstance(t.get(k), dict):
                out["triggers"][tid][k] = copy.deepcopy(t[k])
    return out


def load(path: str = None) -> dict:
    path = path or config_path()
    base = default_config()
    if not os.path.exists(path):
        return base
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return base
    cfg = _merge(base, data)
    # 配置档要逐个补默认值，不能整体覆盖
    tpl = default_profile()
    cfg["profiles"] = [_merge_profile(tpl, p) for p in (data.get("profiles") or base["profiles"])]
    if not cfg["profiles"]:
        cfg["profiles"] = [default_profile()]
    cfg["active_profile"] = max(0, min(len(cfg["profiles"]) - 1, int(cfg.get("active_profile", 0))))
    _migrate_scroll_axes(cfg, data)
    return cfg


def _migrate_scroll_axes(cfg: dict, raw: dict):
    """老配置里横向滚动方向这一项不存在，让它**跟着纵向的选择走**。

    直接吃默认值的话，用户明明选了「传统」，升级后横向会莫名其妙变成「自然」——
    他没改过任何设置，方向却变了，这种事最让人摸不着头脑。
    """
    for i, p in enumerate(cfg.get("profiles") or []):
        old_p = (raw.get("profiles") or [{}] * (i + 1))[i] if i < len(raw.get("profiles") or []) else {}
        tp_old = (old_p or {}).get("touchpad") or {}
        if "natural_hscroll" not in tp_old and "natural_scroll" in tp_old:
            p["touchpad"]["natural_hscroll"] = tp_old["natural_scroll"]
        for side in ("left", "right"):
            s_old = ((old_p or {}).get("sticks") or {}).get(side) or {}
            if "invert_hscroll" not in s_old and "invert_scroll" in s_old:
                p["sticks"][side]["invert_hscroll"] = s_old["invert_scroll"]


def save(cfg: dict, path: str = None) -> bool:
    path = path or config_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def unique_name(profiles, want: str, skip: int = -1) -> str:
    """把名字改成在现有配置档里唯一的。重名会让「你现在在哪一档」变成猜谜。

    「日常桌面」→「日常桌面 2」→「日常桌面 3」…… skip 是重命名时要忽略的下标。
    """
    want = (str(want or "").strip() or "新配置档")[:40]
    taken = {p.get("name") for i, p in enumerate(profiles) if i != skip}
    if want not in taken:
        return want
    n = 2
    while "%s %d" % (want, n) in taken:
        n += 1
    return "%s %d" % (want, n)


def duplicate_profile(profiles, index: int):
    """完整复制一份配置档，插在原件后面。返回新档的下标，越界时返回 None。

    必须深拷贝：浅拷贝的话两份档会共用 buttons / triggers 那几个字典，
    改一边另一边跟着变，用户根本不会想到是这个原因。
    """
    if not (0 <= index < len(profiles)):
        return None
    copy_of = copy.deepcopy(profiles[index])
    copy_of["name"] = unique_name(profiles, (copy_of.get("name") or "配置档") + " 副本")
    profiles.insert(index + 1, copy_of)
    return index + 1


def active(cfg: dict) -> dict:
    idx = max(0, min(len(cfg["profiles"]) - 1, cfg.get("active_profile", 0)))
    return cfg["profiles"][idx]


def find_conflicts(profile: dict) -> dict:
    """返回 {控件 id: 是否与别的控件绑了同一个功能}"""
    seen = {}
    for cid, act in (profile.get("buttons") or {}).items():
        key = _action_key(act)
        if key:
            seen.setdefault(key, []).append(cid)
    for tid, tr in (profile.get("triggers") or {}).items():
        key = _action_key(tr.get("binding"))
        if key:
            seen.setdefault(key, []).append(tid)
    out = {}
    for key, ids in seen.items():
        if len(ids) > 1:
            for i in ids:
                out[i] = True
    return out


def _action_key(act):
    if not act or not act.get("type"):
        return None
    t = act["type"]
    if t == "combo":
        return "combo:" + str(act.get("keys", "")).lower()
    if t == "mouse":
        return "mouse:" + str(act.get("button", ""))
    if t == "builtin":
        return "builtin:" + str(act.get("name", ""))
    if t == "macro":
        return "macro:" + json.dumps(act.get("steps", []), sort_keys=True)
    return None
