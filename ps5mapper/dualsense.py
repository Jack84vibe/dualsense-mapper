"""DualSense (PS5 手柄) HID 读写层。

支持 USB 与蓝牙两种连接：
  - USB : 输入报告 0x01（64 字节），输出报告 0x02（48 字节）
  - 蓝牙: 输入报告 0x31（78 字节，数据从偏移 2 开始），输出报告 0x31（带 CRC32）

报告结构参考 Linux 内核 hid-playstation.c 的 dualsense_input_report /
dualsense_output_report_common，以及社区整理的 DualSense HID 文档。
"""
from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Optional

try:
    import hid  # hidapi
except ImportError:  # 让纯逻辑测试能在没有 hidapi 的机器上跑
    hid = None

VENDOR_SONY = 0x054C
PRODUCT_DUALSENSE = 0x0CE6
PRODUCT_DUALSENSE_EDGE = 0x0DF2
SUPPORTED_PRODUCTS = (PRODUCT_DUALSENSE, PRODUCT_DUALSENSE_EDGE)

INPUT_REPORT_USB = 0x01
INPUT_REPORT_BT = 0x31
OUTPUT_REPORT_USB = 0x02
OUTPUT_REPORT_BT = 0x31

USB_INPUT_LEN = 64
BT_INPUT_LEN = 78
USB_OUTPUT_LEN = 63
BT_OUTPUT_LEN = 78

# 触摸板坐标上限（12 位字段，实测上限约 1920 x 1080；probe.py 会打印真实范围）
TOUCH_MAX_X = 1919
TOUCH_MAX_Y = 1079

# 运动传感器在报告里的位置（相对于 d，也就是去掉报告头之后）。
# probe10 用「静止时加速度合矢量必须等于 1G」验证过：实测 0.972G，偏差 2.8%。
GYRO_OFF = 15          # 三个 int16：pitch, yaw, roll
ACCEL_OFF = 21         # 三个 int16：x, y, z
SENSOR_TS_OFF = 27     # uint32

# 标定功能报告
CALIB_REPORT_ID = 0x05
CALIB_REPORT_LEN = 41

# 传感器时间戳每格多少微秒。这个值我们**不假设**，运行时用挂钟时间实测
# （见 gyro.TickEstimator）；这里只是估计不出来时的兜底。
# probe10 在蓝牙下实测约 0.45 微秒/格，但那次的帧率本身也存疑，所以不写死。
TICK_US_FALLBACK = 0.33

# buttons[0]
BTN0_SQUARE = 0x10
BTN0_CROSS = 0x20
BTN0_CIRCLE = 0x40
BTN0_TRIANGLE = 0x80
DPAD_NEUTRAL = 8
# buttons[1]
BTN1_L1 = 0x01
BTN1_R1 = 0x02
BTN1_L2 = 0x04
BTN1_R2 = 0x08
BTN1_CREATE = 0x10
BTN1_OPTIONS = 0x20
BTN1_L3 = 0x40
BTN1_R3 = 0x80
# buttons[2]
BTN2_PS = 0x01
BTN2_TOUCHPAD = 0x02
BTN2_MUTE = 0x04

# 十字键方向值 → (上, 下, 左, 右)
_DPAD_TABLE = {
    0: (1, 0, 0, 0), 1: (1, 0, 0, 1), 2: (0, 0, 0, 1), 3: (0, 1, 0, 1),
    4: (0, 1, 0, 0), 5: (0, 1, 1, 0), 6: (0, 0, 1, 0), 7: (1, 0, 1, 0),
    8: (0, 0, 0, 0),
}

# 引擎内部使用的控件 id，与 UI 中的 id 一一对应
BUTTON_IDS = (
    "cross", "circle", "square", "triangle",
    "dpad_up", "dpad_down", "dpad_left", "dpad_right",
    "l1", "r1", "l3", "r3",
    "create", "options", "ps", "touchpad_click", "mic",
)


@dataclass
class TouchPoint:
    active: bool = False
    ident: int = 0
    x: int = 0
    y: int = 0


@dataclass
class ControllerState:
    """一帧输入。数值均已归一化，方便上层直接使用。"""
    lx: float = 0.0        # -1..1，右为正
    ly: float = 0.0        # -1..1，下为正
    rx: float = 0.0
    ry: float = 0.0
    l2: float = 0.0        # 0..1
    r2: float = 0.0
    buttons: dict = field(default_factory=dict)
    touch: tuple = field(default_factory=lambda: (TouchPoint(), TouchPoint()))
    battery_percent: Optional[int] = None
    charging: bool = False
    raw_l2: int = 0
    raw_r2: int = 0
    # 运动传感器。都是**原始值**，没有减零点、没有换算单位；
    # 换算要用手柄自己的标定表（见 parse_calibration），每只手柄都不一样。
    gyro: tuple = (0, 0, 0)        # (pitch, yaw, roll)
    accel: tuple = (0, 0, 0)       # (x, y, z)
    sensor_timestamp: int = 0      # 手柄自己的计时器，单位见 TICK_US_FALLBACK

    def pressed(self, name: str) -> bool:
        return bool(self.buttons.get(name))


def _stick(v: int) -> float:
    """0..255 → -1..1"""
    return (v - 128) / 127.0 if v >= 128 else (v - 128) / 128.0


def parse_input_report(buf: bytes, bluetooth: bool) -> Optional[ControllerState]:
    """把一帧原始报告解析成 ControllerState。数据不完整时返回 None。"""
    if not buf:
        return None
    rid = buf[0]
    if bluetooth:
        if rid != INPUT_REPORT_BT or len(buf) < 42:
            return None
        d = buf[2:]          # 蓝牙全量报告，数据从偏移 2 开始
    else:
        if rid != INPUT_REPORT_USB or len(buf) < 41:
            return None
        d = buf[1:]
    if len(d) < 41:
        return None

    st = ControllerState()
    st.lx, st.ly = _stick(d[0]), _stick(d[1])
    st.rx, st.ry = _stick(d[2]), _stick(d[3])
    st.raw_l2, st.raw_r2 = d[4], d[5]
    st.l2, st.r2 = d[4] / 255.0, d[5] / 255.0

    b0, b1, b2 = d[7], d[8], d[9]
    up, down, left, right = _DPAD_TABLE.get(b0 & 0x0F, (0, 0, 0, 0))
    st.buttons = {
        "square": bool(b0 & BTN0_SQUARE),
        "cross": bool(b0 & BTN0_CROSS),
        "circle": bool(b0 & BTN0_CIRCLE),
        "triangle": bool(b0 & BTN0_TRIANGLE),
        "dpad_up": bool(up), "dpad_down": bool(down),
        "dpad_left": bool(left), "dpad_right": bool(right),
        "l1": bool(b1 & BTN1_L1), "r1": bool(b1 & BTN1_R1),
        "create": bool(b1 & BTN1_CREATE), "options": bool(b1 & BTN1_OPTIONS),
        "l3": bool(b1 & BTN1_L3), "r3": bool(b1 & BTN1_R3),
        "ps": bool(b2 & BTN2_PS),
        "touchpad_click": bool(b2 & BTN2_TOUCHPAD),
        "mic": bool(b2 & BTN2_MUTE),
    }

    # 运动传感器。偏移已用 probe10 验证：静止时加速度合矢量 0.972G，对得上 1G。
    if len(d) >= SENSOR_TS_OFF + 4:
        st.gyro = struct.unpack_from("<3h", d, GYRO_OFF)
        st.accel = struct.unpack_from("<3h", d, ACCEL_OFF)
        (st.sensor_timestamp,) = struct.unpack_from("<I", d, SENSOR_TS_OFF)

    pts = []
    for off in (32, 36):                      # 触点 1: d[32:36]，触点 2: d[36:40]
        if len(d) >= off + 4:
            c, x_lo, mid, y_hi = d[off], d[off + 1], d[off + 2], d[off + 3]
            # x 是 12 位：低 8 位在 x_lo，高 4 位在 mid 的低半字节
            # y 是 12 位：低 4 位在 mid 的高半字节，高 8 位在 y_hi
            pts.append(TouchPoint(
                active=not (c & 0x80),        # bit7 置位表示「没有接触」
                ident=c & 0x7F,
                x=x_lo | ((mid & 0x0F) << 8),
                y=((mid & 0xF0) >> 4) | (y_hi << 4),
            ))
        else:
            pts.append(TouchPoint())
    st.touch = tuple(pts)

    # 电量：偏移 52 的低 4 位是格数（0-10），bit4/5 是充电状态
    if len(d) > 52:
        raw = d[52]
        level = raw & 0x0F
        status = (raw & 0xF0) >> 4
        st.charging = status in (1, 2)
        st.battery_percent = min(100, level * 10 + 5) if level <= 10 else None
    return st


def parse_calibration(buf: bytes) -> Optional[dict]:
    """解析功能报告 0x05 —— 每只手柄出厂时单独标定的刻度表。

    为什么非读不可：陀螺仪报告里的原始值**不是**固定刻度。Linux 驱动里那个
    「1024 = 1 度/秒」是标定**之后**的输出单位；直接拿它去除原始值，算出来的
    满量程只有 ±32 度/秒，手腕随便一转就爆表。实测这只手柄的真实刻度是
    ±2000 度/秒，差了 60 倍。所以刻度必须从手柄自己这里问。

    换算公式照搬 hid-playstation.c：
        陀螺仪 度/秒 = 原始值 * speed_2x / (|plus - bias| + |minus - bias|)
        加速度 G     = (原始值 - bias) * 2 / (plus - minus)

    读不到或数值不合理时返回 None，调用方退回「只用原始值」的模式——
    功能照样能用，只是灵敏度那个数字没法用「度」表达。
    """
    if not buf or len(buf) < 35 or buf[0] != CALIB_REPORT_ID:
        return None

    def le16(off: int) -> int:
        v = buf[off] | (buf[off + 1] << 8)
        return v - 0x10000 if v >= 0x8000 else v

    g_bias = (le16(1), le16(3), le16(5))                    # pitch, yaw, roll
    g_plus = (le16(7), le16(11), le16(15))
    g_minus = (le16(9), le16(13), le16(17))
    speed_2x = le16(19) + le16(21)
    a_plus = (le16(23), le16(27), le16(31))
    a_minus = (le16(25), le16(29), le16(33))

    gyro_scale = []
    for i in range(3):
        denom = abs(g_plus[i] - g_bias[i]) + abs(g_minus[i] - g_bias[i])
        if not denom:
            return None
        gyro_scale.append(speed_2x / denom)

    # 合理性检查：DualSense 的陀螺仪满量程在 ±2000 度/秒上下。
    # 差太远说明这份标定表没读对，宁可退回原始值也别拿错刻度去算。
    for s in gyro_scale:
        if not (500.0 <= 32767 * s <= 5000.0):
            return None

    acc_bias, acc_scale = [], []
    for i in range(3):
        rng = a_plus[i] - a_minus[i]
        if not rng:
            return None
        acc_bias.append(a_plus[i] - rng / 2.0)
        acc_scale.append(2.0 / rng)

    return {
        "gyro_scale": tuple(gyro_scale),   # 原始值 * scale = 度/秒
        "accel_bias": tuple(acc_bias),
        "accel_scale": tuple(acc_scale),   # (原始值 - bias) * scale = G
        "speed_2x": speed_2x,
    }


def dualsense_crc32(report_bytes: bytes) -> int:
    """蓝牙输出报告要在结尾附加 CRC32，计算时前面要补一个 0xA2 种子字节。"""
    return zlib.crc32(b"\xA2" + report_bytes) & 0xFFFFFFFF


# ---------------------------------------------------------------- 输出报告

FLAG0_COMPATIBLE_VIBRATION = 0x01
FLAG0_HAPTICS_SELECT = 0x02
FLAG0_RIGHT_TRIGGER_FX = 0x04
FLAG0_LEFT_TRIGGER_FX = 0x08

FLAG1_MIC_MUTE_LED = 0x01
FLAG1_POWER_SAVE = 0x02
FLAG1_LIGHTBAR = 0x04
FLAG1_RELEASE_LEDS = 0x08
FLAG1_PLAYER_INDICATOR = 0x10

FLAG2_LIGHTBAR_SETUP = 0x02

LIGHTBAR_SETUP_LIGHT_OUT = 0x02   # 中断开机渐变动画，把灯条交给主机

COMMON_LEN = 47   # 通用输出块长度（valid_flag0 … lightbar_blue）


def build_output_common(
    *,
    rumble_left: int = 0,
    rumble_right: int = 0,
    lightbar: Optional[tuple] = None,
    right_trigger: Optional[bytes] = None,
    left_trigger: Optional[bytes] = None,
    player_leds: Optional[int] = None,
    mic_led: Optional[bool] = None,
    lightbar_setup: Optional[int] = None,
    led_brightness: int = 0,
) -> bytearray:
    """构造 47 字节通用输出块（不含报告 id / 蓝牙头 / CRC）。"""
    b = bytearray(COMMON_LEN)
    f0 = 0
    f1 = 0
    f2 = 0

    if rumble_left or rumble_right:
        f0 |= FLAG0_COMPATIBLE_VIBRATION | FLAG0_HAPTICS_SELECT
    b[2] = rumble_right & 0xFF          # 弱震（右）
    b[3] = rumble_left & 0xFF           # 强震（左）

    if mic_led is not None:
        f1 |= FLAG1_MIC_MUTE_LED
        b[8] = 1 if mic_led else 0

    if right_trigger is not None:
        f0 |= FLAG0_RIGHT_TRIGGER_FX
        b[10:21] = right_trigger[:11].ljust(11, b"\x00")
    if left_trigger is not None:
        f0 |= FLAG0_LEFT_TRIGGER_FX
        b[21:32] = left_trigger[:11].ljust(11, b"\x00")

    if lightbar is not None:
        f1 |= FLAG1_LIGHTBAR
        r, g, bl = lightbar
        b[44], b[45], b[46] = r & 0xFF, g & 0xFF, bl & 0xFF

    if player_leds is not None:
        f1 |= FLAG1_PLAYER_INDICATOR
        b[42] = led_brightness & 0xFF     # 0=最亮 1=中 2=暗
        b[43] = player_leds & 0x1F

    if lightbar_setup is not None:
        f2 |= FLAG2_LIGHTBAR_SETUP
        b[41] = lightbar_setup & 0xFF

    b[0], b[1], b[38] = f0, f1, f2
    return b


def build_lightbar_reset() -> bytearray:
    """把灯条和玩家灯从固件手里抢过来。

    **不发这一条，手柄根本不理会主机设的颜色。** 手柄上电/连上之后会自己跑一段
    灯条渐变动画，动画结束后固件继续维持那个颜色，主机发的 RGB 全部被无视。
    Linux 的 hid-playstation 驱动在 probe 阶段就干这件事，注释原话是
    「必须先显式重新配置灯条，之后才能对它编程」。

    麦克风静音灯走的是另一条路（第 8 字节），不受这个限制——所以症状会是
    「麦克风灯好使，灯条和玩家灯完全没反应」。
    """
    return build_output_common(lightbar_setup=LIGHTBAR_SETUP_LIGHT_OUT)


def build_output_report(common: bytearray, bluetooth: bool, seq: int = 0) -> bytes:
    """把通用输出块包装成可直接 write 的完整报告。"""
    if not bluetooth:
        # 0x02 + 47 字节通用块 + 15 字节保留 = 63
        return bytes([OUTPUT_REPORT_USB]) + bytes(common) + bytes(USB_OUTPUT_LEN - 1 - COMMON_LEN)
    body = bytearray(BT_OUTPUT_LEN)
    body[0] = OUTPUT_REPORT_BT
    body[1] = ((seq & 0x0F) << 4) | 0x00
    body[2] = 0x10                      # DS_OUTPUT_TAG
    body[3:3 + COMMON_LEN] = common
    crc = dualsense_crc32(bytes(body[:74]))
    body[74:78] = struct.pack("<I", crc)
    return bytes(body)


# ---------------------------------------------------------------- 设备

class DualSense:
    """一只手柄的连接。open() 会自动判断 USB 还是蓝牙。"""

    def __init__(self):
        self.dev = None
        self.bluetooth = False
        self.path = None
        self._seq = 0
        self._blocking = False
        self.calib = None          # 标定表，open() 时填；读不到就一直是 None

    # -- 连接 ---------------------------------------------------------
    @staticmethod
    def enumerate() -> list:
        """列出所有 DualSense 接口，把最可能是「游戏手柄」的那个排在前面。

        Windows 会为设备的每个顶层集合各建一个 HID 接口，同一只手柄常常出现
        两三条。只有 Generic Desktop(0x01) / Game Pad(0x05) 那条才送游戏数据，
        其它接口读出来全是零。
        """
        if hid is None:
            return []
        out = []
        for info in hid.enumerate(VENDOR_SONY, 0):
            if info.get("product_id") in SUPPORTED_PRODUCTS:
                out.append(info)

        def usage_rank(i):
            up, us = i.get("usage_page"), i.get("usage")
            if up == 0x01 and us == 0x05:
                return 0                     # Game Pad，首选
            if up == 0x01 and us == 0x04:
                return 1                     # Joystick
            if not up:
                return 2                     # 有些平台不报 usage，仍值得一试
            return 3

        # 手柄可能同时挂着 USB 和蓝牙两条连接，两条的 usage 完全一样，
        # 只能靠设备路径区分。优先 USB：只有 USB 下麦克风才可用，数据也更稳。
        out.sort(key=lambda i: (DualSense.is_bluetooth_path(i), usage_rank(i)))
        return out

    @staticmethod
    def path_str(info: dict) -> str:
        p = info.get("path")
        if isinstance(p, bytes):
            p = p.decode("utf-8", "replace")
        return p or ""

    @staticmethod
    def is_bluetooth_path(info: dict) -> bool:
        """Windows 蓝牙 HID 的设备路径里带蓝牙 HID 服务的 UUID 00001124-…；
        USB 的路径长 HID#VID_054C&PID_0CE6&MI_03 这样。"""
        return "00001124" in DualSense.path_str(info).lower()

    @staticmethod
    def _looks_alive(state: "ControllerState") -> bool:
        """判断这一帧是不是真数据。全零报告会被解读成「十字键一直按着上」，
        所以拿静息状态的特征来验：摇杆应该在中位附近，十字键应该是「松开」。"""
        if state is None:
            return False
        centered = abs(state.lx) < 0.9 and abs(state.ly) < 0.9
        dpad_all_released = not any(
            state.buttons.get(k) for k in
            ("dpad_up", "dpad_down", "dpad_left", "dpad_right"))
        return centered and dpad_all_released

    def open(self) -> bool:
        """逐个尝试候选接口，读到真数据才算成功。"""
        if hid is None:
            raise RuntimeError("缺少 hidapi，请先 pip install hidapi")
        for info in self.enumerate():
            dev = hid.device()
            try:
                dev.open_path(info["path"])
                dev.set_nonblocking(1)
            except Exception:
                continue
            self.dev = dev
            self.path = info["path"]
            self.bluetooth = self._probe_mode(dev)
            if self._validate(dev):
                return True
            try:
                dev.close()
            except Exception:
                pass
            self.dev = None
        self.calib = None          # 一个都没成功，别把上一个候选的标定表留下
        return False

    def _probe_mode(self, dev) -> bool:
        """USB 下报告 id 是 0x01；蓝牙下要先请求一次特性报告 0x05，
        手柄才会从精简报告切到 0x31 全量报告。

        这一次请求顺便就是标定表，所以直接把结果收下，不用再问第二次。
        """
        try:
            raw = dev.get_feature_report(CALIB_REPORT_ID, CALIB_REPORT_LEN)
            self.calib = parse_calibration(bytes(raw)) if raw else None
        except Exception:
            self.calib = None
        deadline = time.time() + 1.0
        while time.time() < deadline:
            data = dev.read(BT_INPUT_LEN)
            if data:
                if data[0] == INPUT_REPORT_BT:
                    return True
                if data[0] == INPUT_REPORT_USB:
                    return False
            time.sleep(0.004)
        return False

    def _validate(self, dev, seconds: float = 1.2) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            data = dev.read(BT_INPUT_LEN if self.bluetooth else USB_INPUT_LEN)
            if data:
                st = parse_input_report(bytes(data), self.bluetooth)
                if self._looks_alive(st):
                    return True
            time.sleep(0.004)
        return False

    def close(self):
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None
        self.calib = None

    @property
    def connected(self) -> bool:
        return self.dev is not None

    # -- 读写 ---------------------------------------------------------
    def set_blocking(self, blocking: bool = True):
        """主循环用阻塞读取：跟着手柄的原生上报速率走，不用 sleep 掐时间。

        Windows 的 time.sleep 精度只有 15.6ms，靠轮询 + sleep 会把 250Hz 压到 64Hz，
        指针明显发卡。阻塞读取由 hidapi 在有数据时立刻返回，没有这个问题。
        """
        if self.dev:
            try:
                self.dev.set_nonblocking(0 if blocking else 1)
                self._blocking = blocking
            except Exception:
                pass

    def read_state(self, timeout_ms: int = 0) -> Optional[ControllerState]:
        if not self.dev:
            return None
        size = BT_INPUT_LEN if self.bluetooth else USB_INPUT_LEN
        try:
            if timeout_ms and getattr(self, "_blocking", False):
                data = self.dev.read(size, timeout_ms)
            else:
                data = self.dev.read(size)
        except OSError:
            self.close()
            return None
        if not data:
            return None
        return parse_input_report(bytes(data), self.bluetooth)

    def reset_leds(self) -> bool:
        """连上之后必须先发这一条，否则灯条和玩家灯不听主机的。"""
        return self.send(build_lightbar_reset())

    def send_neutral(self) -> bool:
        """退出前恢复原样：清掉扳机阻力和震动，灯条回到 PS5 出厂那个蓝，
        玩家灯和麦克风灯熄灭。别让手柄卡在有阻力或者亮着自定义颜色的状态。"""
        from .lights import PS5_BLUE
        return self.send(build_output_common(
            left_trigger=b"\x00" * 11, right_trigger=b"\x00" * 11,
            lightbar=PS5_BLUE, player_leds=0, mic_led=False))

    def send(self, common: bytearray) -> bool:
        if not self.dev:
            return False
        self._seq = (self._seq + 1) & 0x0F
        try:
            self.dev.write(build_output_report(common, self.bluetooth, self._seq))
            return True
        except OSError:
            self.close()
            return False
