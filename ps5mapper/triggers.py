"""自适应扳机效果的字节序列生成。

DualSense 的扳机被划分成 10 个「区」（zone 0 = 完全松开，zone 9 = 按到底）。
效果通过 11 字节的参数块下发，第一个字节是模式，后面 10 字节是参数。

算法来自社区逆向整理的 TriggerEffectGenerator（Nielk1 等），是目前
DualSenseX / DS4Windows 等工具通用的实现。
"""
from __future__ import annotations

ZONES = 10
EFFECT_LEN = 11

MODE_OFF = 0x00
MODE_FEEDBACK = 0x21          # 多区阻力
MODE_BOW = 0x22               # 弓弦：拉到底后回弹
MODE_WEAPON = 0x25            # 区间阻力，按穿后释放（我们的「阻力墙」）
MODE_VIBRATION = 0x26         # 区间震动


def _pack(mode: int, params) -> bytes:
    b = bytearray(EFFECT_LEN)
    b[0] = mode
    for i, v in enumerate(params[:EFFECT_LEN - 1]):
        b[1 + i] = v & 0xFF
    return bytes(b)


def off() -> bytes:
    return _pack(MODE_OFF, [])


def feedback(position: int, strength: int) -> bytes:
    """从 position 区开始，一直到底都保持 strength 的恒定阻力。

    position: 0..9    strength: 0..8（0 = 关闭）
    """
    position = max(0, min(9, int(position)))
    strength = max(0, min(8, int(strength)))
    if strength == 0:
        return off()
    sv = (strength - 1) & 0x07
    force = 0
    active = 0
    for i in range(position, ZONES):
        force |= sv << (3 * i)
        active |= 1 << i
    return _pack(MODE_FEEDBACK, [
        active & 0xFF, (active >> 8) & 0xFF,
        force & 0xFF, (force >> 8) & 0xFF,
        (force >> 16) & 0xFF, (force >> 24) & 0xFF,
        0, 0, 0, 0,
    ])


def multiple_position_feedback(strengths) -> bytes:
    """逐区指定阻力，长度 10 的序列，每项 0..8。用来做双段（两道墙）。"""
    force = 0
    active = 0
    for i in range(ZONES):
        s = max(0, min(8, int(strengths[i]) if i < len(strengths) else 0))
        if s > 0:
            force |= ((s - 1) & 0x07) << (3 * i)
            active |= 1 << i
    if active == 0:
        return off()
    return _pack(MODE_FEEDBACK, [
        active & 0xFF, (active >> 8) & 0xFF,
        force & 0xFF, (force >> 8) & 0xFF,
        (force >> 16) & 0xFF, (force >> 24) & 0xFF,
        0, 0, 0, 0,
    ])


def weapon(start: int, end: int, strength: int) -> bytes:
    """start..end 之间有阻力，按穿 end 之后突然释放——最像机械键盘的段落感。

    硬件要求 start >= 2 且 end > start；越界时退回 feedback()。
    """
    start = int(start)
    end = int(end)
    strength = max(0, min(8, int(strength)))
    if strength == 0:
        return off()
    if start < 2 or end > 9 or end <= start:
        return feedback(max(0, start), strength)
    zones = (1 << start) | (1 << end)
    return _pack(MODE_WEAPON, [
        zones & 0xFF, (zones >> 8) & 0xFF, (strength - 1) & 0xFF,
        0, 0, 0, 0, 0, 0, 0,
    ])


def bow(start: int, end: int, strength: int, snap: int) -> bytes:
    """弓弦：从 start 到 end 越来越紧，松手回弹。"""
    start, end = int(start), int(end)
    strength = max(0, min(8, int(strength)))
    snap = max(0, min(8, int(snap)))
    if strength == 0 or snap == 0 or start > 8 or end > 8 or end <= start:
        return off()
    zones = (1 << start) | (1 << end)
    pair = ((strength - 1) & 0x07) | (((snap - 1) & 0x07) << 3)
    return _pack(MODE_BOW, [
        zones & 0xFF, (zones >> 8) & 0xFF,
        pair & 0xFF, (pair >> 8) & 0xFF,
        0, 0, 0, 0, 0, 0,
    ])


def vibration(position: int, amplitude: int, frequency: int) -> bytes:
    """从 position 区开始震动。frequency 单位约为 Hz（0..255）。"""
    position = max(0, min(9, int(position)))
    amplitude = max(0, min(8, int(amplitude)))
    frequency = max(0, min(255, int(frequency)))
    if amplitude == 0 or frequency == 0:
        return off()
    av = (amplitude - 1) & 0x07
    amp = 0
    active = 0
    for i in range(position, ZONES):
        amp |= av << (3 * i)
        active |= 1 << i
    return _pack(MODE_VIBRATION, [
        active & 0xFF, (active >> 8) & 0xFF,
        amp & 0xFF, (amp >> 8) & 0xFF,
        (amp >> 16) & 0xFF, (amp >> 24) & 0xFF,
        0, 0, frequency, 0,
    ])


def slope(start: int, end: int, start_strength: int, end_strength: int) -> bytes:
    """渐进加重：从 start 区的 start_strength 线性变到 end 区的 end_strength。"""
    start, end = int(start), int(end)
    if end <= start:
        return off()
    strengths = [0] * ZONES
    span = end - start
    for i in range(start, min(end + 1, ZONES)):
        t = (i - start) / span
        strengths[i] = round(start_strength + (end_strength - start_strength) * t)
    return multiple_position_feedback(strengths)


# ------------------------------------------------------------------ 参数转换

def depth_to_zone(depth_percent: float) -> int:
    """触发深度百分比 → 区号 0..9（每区 10%，100% 归到最后一区）"""
    return max(0, min(9, int(depth_percent / 100.0 * ZONES)))


def force_to_strength(force_0_255: int) -> int:
    """0..255 的硬度 → 1..8 的强度等级"""
    if force_0_255 <= 0:
        return 0
    return max(1, min(8, int(round(force_0_255 / 255.0 * 7)) + 1))


# ------------------------------------------------------- 墙的位置 / 触发判定
#
# 这里是「阻力墙在哪」和「按键什么时候真的触发」的唯一事实来源。
# 以前两者共用 depth 一个值：depth 既是墙的起点，又是按键触发点，
# 但 weapon 模式的墙实际上要跨 3 个区（depth ~ depth+30%），
# 于是按键在「刚碰到墙」时就触发了，而手感上「按穿墙」还在后面。
#
# ui/index.html 的 wallSpan() / firePercent() 是这两个函数的镜像，
# 改这里必须同步改那边（tests/test_logic.py 里有对应的断言）。

FIRE_MIN = 5
FIRE_MAX = 98
BOTTOM_PCT = 95        # 「按到底触发」用的判定值，留 5% 余量保证够得着


def wall_span(cfg: dict, stage: int = 1):
    """这道墙实际覆盖的区间 (start_zone, end_zone)，end 为最后一个有阻力的区。"""
    cfg = cfg or {}
    z1 = depth_to_zone(cfg.get("depth", 60))
    if cfg.get("mode") == "dual":
        z2 = depth_to_zone(cfg.get("depth2", 85))
        if z2 <= z1:
            z2 = min(9, z1 + 1)
        if stage == 2:
            return (z2, min(9, z2 + 1))
        return (z1, min(z2 - 1, z1 + 1))
    preset = cfg.get("preset", "wall")
    if preset == "wall":
        return (z1, min(9, z1 + 2))
    if preset == "bow":
        return (max(0, z1 - 1), min(8, z1 + 2))
    # constant / slope / vibration：阻力从 z1 起一直到底，没有「按穿」这回事
    return (z1, z1)


def has_breakthrough(cfg: dict) -> bool:
    """这个效果有没有「按穿墙」的那一下段落感。

    constant / slope / vibration 的阻力是一直持续到底的，压到底也不会突然松脱，
    这时候「按穿墙才触发」讲不通，只能按起点触发。
    """
    cfg = cfg or {}
    if cfg.get("mode") == "dual":
        return True
    return (cfg.get("preset", "wall") or "wall") in ("wall", "bow")


def fire_percent(cfg: dict, stage: int = 1, wall_active: bool = True) -> float:
    """这一段按键真正触发的深度（%）。

    fire_mode:
      wall   —— 按穿阻力墙才触发（默认，也是最符合直觉的）
      custom —— 用户自己拿滑块定
      bottom —— 按到底才触发
    """
    cfg = cfg or {}
    if stage == 2:
        base = max(cfg.get("depth", 60) + 5, cfg.get("depth2", 85))
    else:
        base = cfg.get("depth", 60)
    base = max(FIRE_MIN, min(FIRE_MAX, float(base)))

    mode = cfg.get("fire_mode", "wall")
    if mode == "bottom":
        return float(BOTTOM_PCT)
    if mode == "custom":
        v = cfg.get("fire_depth2" if stage == 2 else "fire_depth")
        if v is None:
            return base
        return max(FIRE_MIN, min(FIRE_MAX, float(v)))
    # mode == "wall"
    if not wall_active or not cfg.get("enabled", True):
        return base                     # 没有墙的时候退回按深度触发
    if not has_breakthrough(cfg):
        return base                     # 恒定阻力类没有「按穿」，起点就是触发点
    _, end = wall_span(cfg, stage)
    return max(base, min(FIRE_MAX, float((end + 1) * 10)))


def build_from_config(cfg: dict) -> bytes:
    """把 UI 上的扳机配置转成可下发的 11 字节效果块。

    cfg 形如:
      {"enabled": true, "mode": "single"|"dual", "preset": "wall",
       "depth": 62, "depth2": 82, "force": 180}
    """
    if not cfg or not cfg.get("enabled", True):
        return off()

    preset = cfg.get("preset", "wall")
    force = force_to_strength(cfg.get("force", 180))
    z1 = depth_to_zone(cfg.get("depth", 60))

    if cfg.get("mode") == "dual":
        z2 = depth_to_zone(cfg.get("depth2", 85))
        if z2 <= z1:
            z2 = min(9, z1 + 1)
        strengths = [0] * ZONES
        # 两道窄墙：本区满力，下一区留一点余韵，中间保持轻微阻尼便于分辨
        strengths[z1] = force
        if z1 + 1 < ZONES:
            strengths[z1 + 1] = max(1, force - 3)
        strengths[z2] = force
        if z2 + 1 < ZONES:
            strengths[z2 + 1] = max(1, force - 3)
        return multiple_position_feedback(strengths)

    if preset == "wall":                       # 触发点阻力墙（默认）
        return weapon(z1, min(9, z1 + 2), force)
    if preset == "constant":                   # 全程恒定阻力
        return feedback(z1, force)
    if preset == "slope":                      # 渐进加重
        return slope(z1, 9, max(1, force - 4), force)
    if preset == "bow":                        # 触发后回弹
        return bow(max(0, z1 - 1), min(8, z1 + 2), force, force)
    if preset == "vibration":                  # 区间震动
        return vibration(z1, force, int(cfg.get("frequency", 30)))
    return weapon(z1, min(9, z1 + 2), force)


PRESETS = {
    "wall": "触发点阻力墙",
    "constant": "全程恒定阻力",
    "slope": "渐进加重",
    "bow": "触发后回弹",
    "vibration": "区间震动",
}
