"""灯光状态计算。

这个模块里全是纯函数：给它「现在几点、暂停没有、电量多少」，它算出这一帧
三盏灯该是什么样。不碰 HID，不碰线程，所以能完整单测——手柄上的灯我没法
自动验证，至少要保证算出来的数是对的。

手柄上有三盏灯：
  * 灯条    —— 触摸板两侧，24 位全彩，**没有硬件动画**，呼吸得我们逐帧发
  * 玩家灯  —— 触摸板下方 5 个白灯，位掩码，这里用来显示电量
  * 麦克风灯 —— 麦克风键上的橙灯，跟随语音输入开关

优先级（上面的压下面的）：
  1. 总开关关闭 / 亮度为 0  → 全灭
  2. 映射已暂停            → 灯条暗红常亮，另两盏熄灭
  3. 切换配置档后的提示期   → 灯条常亮该档颜色
  4. 常态                  → 全局颜色 + 全局灯效
"""
from __future__ import annotations

import math

# PS5 出厂那个蓝，退出程序时恢复成它
PS5_BLUE = (0, 0, 255)
# 暂停时的暗红。用红色是因为它和任何一个配置档的默认色都不像，不会被误认
PAUSED_COLOR = (90, 0, 0)

EFFECTS = {
    "steady": "常亮",
    "breathe": "呼吸",
    "off": "关闭",
}

BREATHE_FLOOR = 0.15      # 呼吸最暗处保留 15%，全黑会看着像坏了
BREATHE_PERIOD = 4.0      # 一次完整呼吸的秒数
PLAYER_LED_COUNT = 5


# ---------------------------------------------------------------- 基础运算

def clamp_rgb(rgb) -> tuple:
    return tuple(max(0, min(255, int(round(c)))) for c in (tuple(rgb) + (0, 0, 0))[:3])


def scale(rgb, factor: float) -> tuple:
    """按比例缩放颜色。亮度调节就是靠这个——灯条没有独立的亮度寄存器。"""
    factor = max(0.0, min(1.0, float(factor)))
    return clamp_rgb([c * factor for c in clamp_rgb(rgb)])


def breathe_factor(now: float, period: float = BREATHE_PERIOD,
                   floor: float = BREATHE_FLOOR) -> float:
    """呼吸曲线，返回 floor..1.0。

    用 (1-cos)/2 而不是 sin，是因为它在最亮和最暗处都是平的，
    观感上更像呼吸，而不是来回扫。
    """
    period = max(0.2, float(period))
    floor = max(0.0, min(1.0, float(floor)))
    phase = (now % period) / period
    wave = (1.0 - math.cos(2.0 * math.pi * phase)) / 2.0
    return floor + (1.0 - floor) * wave


# ---------------------------------------------------------------- 麦克风灯

_WIN_ALIASES = {"win", "meta", "cmd", "super", "windows", "lwin", "rwin"}


def is_voice_input_combo(keys) -> bool:
    """这个组合键是不是「唤起 Windows 语音输入」。

    麦克风灯认的是**动作**而不是**哪个键**——按键全都可以自由映射，
    用户把 Win+H 放到任何一个键上都该点亮麦克风灯。
    """
    if not keys:
        return False
    parts = [p.strip().lower() for p in str(keys).split("+") if p.strip()]
    if len(parts) != 2:
        return False
    mods = [p for p in parts if p in _WIN_ALIASES]
    rest = [p for p in parts if p not in _WIN_ALIASES]
    return len(mods) == 1 and rest == ["h"]


# ---------------------------------------------------------------- 玩家灯

# 这三个掩码是**在真手柄上一个个试出来的**（probe12），不是从位序推的。
#
# 那次实测里「哪一位对应哪一颗灯」的答案自相矛盾：第 0 位和第 4 位都报成
# 「最两边那两颗」，第 1 位和第 3 位都报成「中间两颗」。也就是说 Linux 驱动
# 注释里那句「一位一颗、从左到右」在这只手柄上对不上，位序至今没定论。
# 但下面这三个图案是当场看着亮起来、确认对称、确认好看的，所以直接用观察结果，
# 不用推论。哪天要改，先重跑 probe12，别在这里猜。
#
#   0x04  最中间一颗
#   0x0A  靠近中间的两颗          （PS5 自己「玩家 2」的图案）
#   0x1B  中间两颗 + 最外侧两颗    （PS5 自己「玩家 4」的图案）
#
# 五颗全亮（0x1F）一次都不用：这排灯物理上不是均匀排布的，中间三颗挨得更紧，
# 全亮看着就是歪的。
BATTERY_LOW = 0x04
BATTERY_MID = 0x0A
BATTERY_HIGH = 0x1B
BATTERY_LADDER = (BATTERY_LOW, BATTERY_MID, BATTERY_HIGH)

BATTERY_LOW_PCT = 10          # ≤ 这个值：只亮中间一颗
BATTERY_MID_PCT = 25          # ≤ 这个值：亮两颗
BATTERY_CHARGE_HZ = 2.0       # 充电动画每档停留 0.5 秒


def battery_level(percent) -> int:
    """电量 → 0 / 1 / 2 三档。读不到电量返回 -1。

    只分三档，不按 20% 一格细分，理由是灯本来就不该承担「精确读数」这件事：
    看得越细越容易盯着数字焦虑，而真想知道确切电量，应用界面里一直写着百分比。
    灯只需要回答「还够用 / 该找线了 / 快没电了」。
    """
    if percent is None:
        return -1
    pct = max(0, min(100, int(percent)))
    if pct <= BATTERY_LOW_PCT:
        return 0
    if pct <= BATTERY_MID_PCT:
        return 1
    return 2


def battery_leds(percent, charging: bool = False, now: float = 0.0) -> int:
    """电量 → 5 个玩家灯的位掩码，三档。

    充电时不按档位停住，而是整条梯子从下往上循环点（一颗 → 两颗 → 四颗），
    这样「在充电」这件事在任何电量下都看得出来。充电时电量本来就在往上走，
    灯显示得再准也没意义，倒是「它在涨」这个动作本身信息量最大。
    电量读不到（percent is None）时全灭，不要瞎猜。
    """
    level = battery_level(percent)
    if level < 0:
        return 0
    if charging:
        step = int(now * BATTERY_CHARGE_HZ) % len(BATTERY_LADDER)
        return BATTERY_LADDER[step] & 0x1F
    return BATTERY_LADDER[level] & 0x1F


# ---------------------------------------------------------------- 总装

def default_lights() -> dict:
    return {
        "enabled": True,
        "color": [40, 110, 255],       # 全局常驻颜色，和配置档无关
        # 常驻颜色跟不跟当前配置档走。
        # 开着的话，灯条**一直**是当前档的颜色，一眼就知道现在在哪个档；
        # 关着就一直是上面那个全局颜色，切档只闪一下就跳回去。
        "color_follows_profile": False,
        "effect": "steady",            # steady | breathe | off
        "breathe_period": 4.0,
        "brightness": 80,              # 0..100，缩放所有灯的输出
        "profile_flash": True,         # 切换配置档时闪该档颜色
        "profile_flash_secs": 3.0,
        "battery_on_player_leds": True,
        "mic_led": True,
        # system  = 读 Windows 真实的「麦克风正在被使用」状态（准，但别的应用占麦也会亮）
        # keypress= 只数你按了几次 Win+H（会和自动停止对不上）
        "mic_led_source": "system",
    }


def needs_animation(lights: dict, paused: bool = False, flashing: bool = False,
                    charging: bool = False) -> bool:
    """这一帧之后还需不需要高频刷新。

    常亮 + 没在充电 + 没在闪 = 画面是静止的，输出线程可以躺平省电，
    别为了一个不动的颜色去占蓝牙带宽。
    """
    lights = lights or {}
    if not lights.get("enabled", True):
        return False
    if paused:
        return False
    if flashing:
        return True
    if charging and lights.get("battery_on_player_leds", True):
        return True             # 充电时那一格要闪
    return lights.get("effect", "steady") == "breathe"


def resting_color(lights: dict, profile_color=None):
    """常驻颜色到底用哪个。

    默认用全局颜色（和配置档无关）；打开「跟随配置档」之后改用当前档的颜色，
    这样灯条**一直**指示着你在哪个档，而不是切档时闪三秒就跳回去。
    """
    lights = lights or {}
    if lights.get("color_follows_profile") and profile_color is not None:
        return tuple(profile_color)
    return tuple(lights.get("color", (40, 110, 255)))


def resolve(lights: dict, now: float = 0.0, *, paused: bool = False,
            flash_color=None, flash_until: float = 0.0,
            battery=None, charging: bool = False, mic_on: bool = False,
            profile_color=None) -> dict:
    """算出这一帧三盏灯的最终状态。

    返回 {"lightbar": (r,g,b), "player_leds": int, "mic_led": bool}
    """
    lights = lights or {}
    bright = max(0, min(100, int(lights.get("brightness", 80)))) / 100.0
    off = {"lightbar": (0, 0, 0), "player_leds": 0, "mic_led": False}

    # 1. 总开关 / 亮度归零
    if not lights.get("enabled", True) or bright <= 0.0:
        return off

    # 2. 暂停压过一切显示。另两盏灯也熄掉，让「停了」这件事不可能被看错
    if paused:
        return {"lightbar": scale(PAUSED_COLOR, bright),
                "player_leds": 0, "mic_led": False}

    # 玩家灯 / 麦克风灯在下面两种情况里是一样的，先算好
    leds = 0
    if lights.get("battery_on_player_leds", True):
        leds = battery_leds(battery, charging, now)
    mic = bool(mic_on) and lights.get("mic_led", True)

    base_color = resting_color(lights, profile_color)

    # 3. 切档提示期：常亮该档颜色，不呼吸——闪的时候还呼吸就看不清是什么色了
    #    常驻颜色已经跟着档走的话，闪和不闪长得一样，这一步就没意义了，跳过。
    if (lights.get("profile_flash", True) and flash_color is not None
            and now < flash_until and not lights.get("color_follows_profile")):
        return {"lightbar": scale(flash_color, bright),
                "player_leds": leds, "mic_led": mic}

    # 4. 常态
    effect = lights.get("effect", "steady")
    if effect == "off":
        base = (0, 0, 0)
    elif effect == "breathe":
        f = breathe_factor(now, lights.get("breathe_period", BREATHE_PERIOD))
        base = scale(base_color, bright * f)
    else:
        base = scale(base_color, bright)
    return {"lightbar": base, "player_leds": leds, "mic_led": mic}
