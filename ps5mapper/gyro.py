"""陀螺仪指针 —— 把手柄的转动变成光标位移。

设计依据全部来自 probe10 在这只手柄上的实测，不是估的：

  · 刻度      每只手柄出厂单独标定，满量程约 ±2000 度/秒。
              实测 yaw 是 0.06153 度/秒每原始单位。**不能写死常数**，
              要从手柄的功能报告 0x05 里读（见 dualsense.parse_calibration）。
  · 轴的顺序  报告里三个值依次是 pitch(俯仰) / yaw(偏航) / roll(滚转)。
              实测：抬手柄前端 -> 第 0 个值 -247 度/秒；快速左右转 -> 第 1 个
              值 -206 度/秒；拧门把手 -> 第 2 个值 -364 度/秒。三段都对得上。
  · 零点      静止时读数不是零，实测 -1.12 / -1.29 / -2.75 原始单位。
              这个值随温度漂，所以不写死，运行时自动重测。
  · 噪声本底  手柄**平放桌上**时最大偏离只有 6 原始单位（0.37 度/秒），
              传感器本身很干净。但这不是「握在手里」的抖动——那个大得多，
              而 probe10 没测到（我让用户把手柄放桌上了）。所以抖动阈值
              给的是保守默认值 + 界面上可调，不是实测值。
  · 上报速率  实测 586 帧/秒，比指针线程快。所以角度在读线程里**累加**，
              指针线程再整批取走，不能在读线程里直接发鼠标事件。

符号方向没有实测出来：probe10 每一段我都写了「来回转」，两个方向都做了，
峰值只取绝对值较大的那个，正负根本分不出来。所以每个轴都有一个反向开关，
默认按常见约定，错了在界面上点一下就好。
"""
from __future__ import annotations

import math
import time

# 报告里陀螺仪三个值的下标
PITCH, YAW, ROLL = 0, 1, 2

# ---- 默认值。都可以在界面上改，这里只是出厂设定 ----------------------

# 灵敏度：每转 1 度，光标走多少像素。
# 由 probe10 的「自然横扫」反推：峰值 183.7 度/秒，6 秒累计转过 471 度，
# 一次舒服的横扫大约 60~90 度。取 75 度扫过 1920 像素 -> 约 25 像素/度。
DEFAULT_PIXELS_PER_DEGREE = 25.0

# 抖动压制阈值（度/秒）。低于它的转动按比例缩小。
# 传感器本底只有 0.37 度/秒，但人手握着会抖得多。1.5 实测偏低——
# 用户反馈「手轻轻抖一下鼠标就晃」，所以提到 3.0。
DEFAULT_TREMOR_DPS = 3.0

# 漂移地板（度/秒）。低于它**直接归零**，不是缩小。
#
# 积分对常数毫不留情：哪怕只剩 0.15 度/秒的残留，一分钟也是 9 度 ≈ 225 像素。
# 渐进压制解决不了这个——它只把信号缩小，缩小之后还是个常数。
#
# 但要说清楚分工：**对付零点偏移的主力是下面的自动校准，不是这条地板。**
# 偏 1 度/秒这种量级远在地板之上，压制后仍会漂，只能靠把零点追平。
# 地板管的是另一段：小到自动校准都懒得动、却仍然会被积分累积起来的残留。
# 0.2 度/秒小到人手根本做不出来，不会吃掉任何有意的微调。
DRIFT_FLOOR_DPS = 0.2

# 低通平滑：对最近几帧取加权平均。帧率 586Hz，取 4 帧约等于 7 毫秒，
# 足够压掉高频抖动又不会让人觉得迟滞。
DEFAULT_SMOOTHING = 4

# ---- 加速曲线 --------------------------------------------------------
#
# 这是解「又要够快、又要不抖」这个矛盾的关键。
#
# 用固定增益的话，两者是绑死的：把灵敏度从 25 拉到 80 换来了够快的大幅移动，
# 同时也把手抖放大了 3.2 倍。再怎么调抖动阈值都没用——阈值只能决定「多小算抖」，
# 决定不了「抖起来有多大」。
#
# 加速曲线把它们解开：慢速转动用低增益（手抖都落在这一段），快速转动用全增益
# （有意的大幅横扫落在这一段）。和鼠标加速是一个道理，只是这里量的是角速度。
ACCEL_SLOW_DPS = 12.0     # 低于这个角速度，用 slow_gain
ACCEL_FAST_DPS = 110.0    # 高于这个角速度，用全增益
DEFAULT_ACCEL = 60        # 0 = 关闭（固定增益），100 = 最强


def accel_gain(dps: float, strength: int) -> float:
    """按当前角速度算增益倍率，返回 0..1 之间的系数。

    strength=0 时恒为 1（等于关掉加速，行为和以前完全一致）。
    strength 越大，慢速段被压得越低——最强时慢速只有全速的 25%。
    两端之间用平滑插值，不会在某个速度上突然窜一下。
    """
    if strength <= 0:
        return 1.0
    slow_gain = 1.0 - 0.75 * (min(100, strength) / 100.0)
    a = abs(dps)
    if a <= ACCEL_SLOW_DPS:
        return slow_gain
    if a >= ACCEL_FAST_DPS:
        return 1.0
    t = (a - ACCEL_SLOW_DPS) / (ACCEL_FAST_DPS - ACCEL_SLOW_DPS)
    t = t * t * (3.0 - 2.0 * t)          # smoothstep，两端斜率为 0
    return slow_gain + (1.0 - slow_gain) * t

# 自动重测零点。
#
# 原来的门槛是 2 度/秒 + 连续静止 1 秒。问题是：**手握着手柄时几乎达不到**——
# 人手的抖动本来就常常超过 2 度/秒，于是自动校准一直不触发，零点漂移一路累积，
# 光标就越用越往一边爬。这正是用户报的「过一段时间就开始漂」。
#
# 改成看**一段窗口内的平均绝对值**而不是瞬时值：手抖是围绕零点的随机游走，
# 平均下来很小；有意的转动平均下来很大。这样握在手里也能正常校准。
#
# 窗口要长。短窗口里「零点偏了 3 度/秒」和「你正在慢慢转 3 度/秒」长得一模一样，
# 分不开——我第一版用 0.4 秒窗口 + 6 度/秒门槛，结果把每秒 3 度的慢速有意移动
# 当成漂移吃掉了（测试直接抓出来：本该走 146 像素，只走了 29）。
# 拉到 2.5 秒之后，有意的移动在这么长的窗口里平均值远高于门槛，不会被误伤；
# 而真正的零点偏移是持续的，照样会被追平。
AUTO_ZERO_STILL_DPS = 2.5        # 窗口内平均幅度低于它才认为「没在有意转动」
AUTO_ZERO_SECONDS = 2.5          # 窗口长度
AUTO_ZERO_BLEND = 0.02           # 每帧往新零点挪多少（586Hz，别挪太快）

# 瞬时门槛。**这一条是 probe11 实测抓出来的真凶。**
#
# 只看窗口均值有个致命缺陷：它是滞后指标。一段安静之后你猛地一甩，那一瞬间
# 窗口里装的还全是刚才的安静数据，均值仍然很低 -> 判定「你没在动」-> 校准开门
# -> 零点以 85 毫秒的时间常数扑向当前那个巨大的瞬时读数。
#
# 实测就是这么炸的：零点在半秒内从 -1 冲到 +770 原始单位（相当于 47 度/秒）。
# 之后每一帧的读数都被这个错误零点污染成恒定的 -47 度/秒，窗口均值被抬到 47，
# 再也回不到 2.5 以下 —— 校准从此永久关门，光标匀速下坠不止。
#
# 所以除了窗口均值，**当前这一帧本身也必须是安静的**，才允许动零点。
AUTO_ZERO_INSTANT_DPS = 4.0

# 零点的物理上限（原始单位）。probe10 实测这只手柄静止时是 1~3 个单位，
# 给一百倍余量。算出 770 这种数只可能是我们自己算错了，不是手柄真的偏这么多。
# 有这道闸，即使判定逻辑再出漏子，也不会演变成永久性的失控漂移。
BIAS_LIMIT_RAW = 300.0

# 锁死自救：人的手做不出「持续几秒、速率恒定到小数点后一位」的转动。
# 读数很大但方差极小，那就不是在转手柄，是零点坏了。这时强制重新校准，
# 把上面那种「一旦锁死就永远锁死」的死局打开。
STUCK_SECONDS = 3.0              # 持续这么久
STUCK_STD_DPS = 1.0              # 而标准差小于这个值

# ---- 加速度计当裁判 --------------------------------------------------
#
# 上面那一整套靠陀螺仪自己的统计特征去判断「你到底有没有在转」，无论怎么调
# 都堵不严，因为**这件事用陀螺仪数据原理上就分不出来**：在一段有限时间里，
# 「零点偏了 3 度/秒」和「你正在以 3 度/秒匀速转」产生的数字完全相同。
# 阈值往哪边挪，都只是在两种错误之间换一种错法。
#
# 加速度计能打破这个僵局。它看得到重力方向：
#   陀螺仪说「正在以 3 度/秒俯仰」 -> 两秒后手柄应该低头 6 度
#   -> 如果重力方向纹丝不动，那 3 度/秒**就是零点偏差**，可以放心减掉。
#
# 限制要说清楚：偏航享受不到这个待遇。绕垂直轴转时重力方向不变，加速度计
# 什么都感觉不到。所以偏航要更久的确认时间——持续 2.5 秒的匀速偏航是一次
# 很大的横扫，不常见；俯仰和滚转则是直接可验证的，确认 0.8 秒就够。
# 门槛越低，盲区越小。**盲区是有代价的**：落在盲区里的转动会被裁判判成
# 「没在转」，然后被当成零点偏差吸收掉——你一停手，零点就偏了那么多，反方向漂。
# 用户实测复现：静止 -> 非常缓慢地转 -> 停住 -> 开始漂。1.2 度/秒的门槛太宽了。
#
# 门槛压不到零，因为加速度计本身有噪声。但噪声可以靠**拉长测量基线**压下去：
# 用 1 秒的基线而不是 0.25 秒，角度噪声除以 4，门槛就能跟着降到 0.6。
STATIC_TILT_DPS = 0.6        # 重力方向的变化率低于它，才算「没在转」
GRAV_MARK_S = 1.0            # 测量基线。越长越准，但对「刚停下」的反应越慢
STATIC_G_TOLERANCE = 0.12    # 合矢量偏离 1G 超过这个比例 = 正在平移/被甩，数据不可信
STATIC_CONFIRM_S = 0.8       # 俯仰/滚转的确认时间
STATIC_CONFIRM_YAW_S = 2.5   # 偏航的确认时间（长得多，理由见上）
STATIC_BLEND = 0.01          # 日常维护时零点往真值靠的速度
GRAVITY_LPF = 0.02           # 重力方向的低通系数；快速甩动时加速度计会读到
                             # 手的线性加速度，不滤一下正好在最需要它准的时候失真

# 第二道保险：**绝不吸收任何正在被当作信号输出的读数**。
#
# 上面的门槛把盲区缩小了，但缩不到零。所以再加一条自洽的规矩：日常维护只从
# 「我们本来就当它不存在」的那些读数里学零点。正在推动光标的转动，一律不学。
# 这样即使裁判判错，损失也被限制在「本来就被压掉的那一点点」。
MAINTAIN_MAX_DPS = 1.0       # 日常维护只吃这个幅度以下的读数
#
# 那零点真的坏掉了（偏差远大于这个值）怎么办？走另一条路：加速度计**持续**
# 确认没在转、而陀螺仪却一直读到大数值 —— 这时候偏差是确凿的，快速拉回。
# 有了 0.6 度/秒的门槛，真实的慢速转动会被看见，不会误入这条路。
CORRECT_CONFIRM_S = 2.0      # 持续这么久才认定「零点确实坏了」
CORRECT_BLEND = 0.02         # 认定之后拉回的速度

# 时间戳每格多少微秒——实测得到，不假设。估不出来之前用这个。
TICK_US_FALLBACK = 0.33


def world_space(w, up):
    """把手柄自己的转动拆到**屏幕的坐标系**里，消掉侧倾的影响。

    问题：偏航和俯仰是相对**手柄自己**定义的。手柄端得正时它们正好对应屏幕的
    左右和上下；一旦侧倾（握着的时候很难完全端平，而且转手腕时侧倾角一直在变），
    手柄的「偏航轴」就不再对着屏幕的水平方向了。侧倾 30 度就已经能明显感觉到
    「我明明往右上转，光标却偏右」。

    解法是用重力方向把坐标系转回来。加速度计静止时读到的方向就是「天」：

        水平分量 = 角速度 · 天轴          （绕世界竖直轴转 = 光标左右走）
        前方     = 手柄的前向，去掉竖直成分之后归一化
        垂直分量 = 角速度 · (天轴 × 前方)  （绕这条水平轴转 = 光标上下走）

    手柄端正时这两条会**精确退化成**原来的偏航和俯仰，所以开不开这个功能，
    端正状态下的手感完全一样，已经调好的灵敏度和反向设置都不用重调。

    手柄指向正上或正下时前向量退化（叉积接近零），这时返回 None，调用方退回
    手柄自身的坐标系——那个姿势下本来也没有「屏幕水平」可言。
    """
    ux, uy, uz = up
    # 前向 = 手柄的 +Z，去掉沿天轴的成分
    fx, fy, fz = -ux * uz, -uy * uz, 1.0 - uz * uz
    fn = math.sqrt(fx * fx + fy * fy + fz * fz)
    if fn < 0.2:                     # 指向天顶/地心，退化
        return None
    fx, fy, fz = fx / fn, fy / fn, fz / fn
    # 右手轴 = 天 × 前
    rx = uy * fz - uz * fy
    ry = uz * fx - ux * fz
    rz = ux * fy - uy * fx
    horiz = w[0] * ux + w[1] * uy + w[2] * uz
    vert = w[0] * rx + w[1] * ry + w[2] * rz
    return horiz, vert


def suppress(dps: float, threshold: float) -> float:
    """渐进压制：阈值以下按比例缩小，阈值以上原样通过。

    用平方曲线而不是硬截断。在阈值处两段的值连续（都等于阈值），
    所以不会有「跨过某个点突然跳一下」的感觉。

        |v| >= t  ->  v
        |v| <  t  ->  v * (|v| / t)

    举例：阈值 1.5 度/秒时，0.3 度/秒的手抖只剩 0.06，几乎看不见；
    而 1.4 度/秒的有意微调还留着 1.31，不会被吃掉。
    """
    a = abs(dps)
    if a < DRIFT_FLOOR_DPS:
        return 0.0              # 地板以下直接归零，否则残留会被积分成漂移
    if threshold <= 0:
        return dps
    if a >= threshold:
        return dps
    return dps * (a / threshold)


class TickEstimator:
    """实测「传感器时间戳每格等于多少微秒」。

    手柄的时间戳单位没有公开保证，而且 probe10 在蓝牙下量到的帧率本身就存疑
    （586 帧/秒，比常见的 250 高不少）。与其查一个可能过时的数字写死，不如
    拿挂钟时间去比：时间戳走过多少格，挂钟走过多少微秒，一除就知道。

    估不出来（时间戳不动、或样本太少）时返回兜底值，绝不返回 0 或负数。
    """

    def __init__(self, window: float = 2.0):
        self.window = window
        self._t0 = None
        self._ts0 = None
        self._value = TICK_US_FALLBACK
        self.measured = False

    def feed(self, sensor_ts: int, now: float) -> None:
        if self._t0 is None:
            self._t0, self._ts0 = now, sensor_ts
            return
        elapsed = now - self._t0
        if elapsed < self.window:
            return
        ticks = (sensor_ts - self._ts0) & 0xFFFFFFFF
        if ticks > 0:
            us = elapsed * 1_000_000.0
            v = us / ticks
            # 合理性检查：0.05~50 微秒/格之外的值不信，多半是时间戳绕回了
            if 0.05 <= v <= 50.0:
                self._value = v
                self.measured = True
        self._t0, self._ts0 = now, sensor_ts

    @property
    def us_per_tick(self) -> float:
        return self._value


class GyroPointer:
    """把一帧帧陀螺仪读数累加成待发送的光标位移。

    用法分两边：
      · 读线程每收到一帧调 feed()，它只做累加，不碰鼠标
      · 指针线程调 take()，把累计的位移取走并清零

    这样 586Hz 的输入不会变成 586Hz 的鼠标事件，也不会因为指针线程慢而丢掉
    转动——转动是积分出来的，累加过程中一点都不会少。
    """

    def __init__(self, calib=None):
        self.set_calibration(calib)
        self.enabled = False
        self.pixels_per_degree = DEFAULT_PIXELS_PER_DEGREE
        self.vertical_pixels_per_degree = DEFAULT_PIXELS_PER_DEGREE
        self.tremor_dps = DEFAULT_TREMOR_DPS
        self.smoothing = DEFAULT_SMOOTHING
        self.accel = DEFAULT_ACCEL
        self.invert_x = False
        self.invert_y = False
        # 垂直方向走不走积分。绝对模式下由引擎按俯仰角直接定位，
        # 这里就不能再累加，否则两套位移会打架。
        self.vertical_relative = True
        # 用屏幕坐标系还是手柄自己的坐标系。见 world_space()。
        self.world = True

        self._bias = [0.0, 0.0, 0.0]
        self._hist = []
        self._acc_x = 0.0
        self._acc_y = 0.0
        self._last_ts = None
        self._still_since = None
        self._still_win = []
        self._stuck_since = None
        self.stuck_recoveries = 0        # 自救了几次，界面上显示出来
        self._grav = None                # 低通之后的重力方向
        self._grav_mark = None           # (时刻, 当时的重力方向)，用来算变化率
        self._static_since = None        # 加速度计判定「静止」从什么时候开始
        self._big_since = None           # 大读数从什么时候开始持续存在
        self.tilt_dps = 0.0              # 重力方向的变化率，界面上显示
        self.static = False              # 此刻加速度计认为手柄静止吗
        self.ticks = TickEstimator()
        # 界面上要显示的实时角速度（度/秒），方便用户自己看着调阈值
        self.live_dps = (0.0, 0.0, 0.0)

    # -- 标定 ---------------------------------------------------------
    def set_calibration(self, calib) -> None:
        """没有标定表时退回一个名义刻度，功能不瘫，只是「度」这个单位不准。"""
        self.scale_source = calib        # 引擎靠它判断要不要重新设置
        if calib and calib.get("gyro_scale"):
            self.scale = tuple(calib["gyro_scale"])
            self.calibrated = True
        else:
            self.scale = (2000.0 / 32767,) * 3
            self.calibrated = False

    def reset_zero(self) -> None:
        """手动校准：下一段静止读数会被当成新零点。"""
        self._bias = [0.0, 0.0, 0.0]
        self._still_since = None
        self._still_win.clear()
        self._stuck_since = None
        self._static_since = None
        self._big_since = None
        self._hist.clear()

    # -- 输入侧 -------------------------------------------------------
    def feed(self, gyro, sensor_ts: int, accel=None, now: float = None) -> None:
        if now is None:
            now = time.monotonic()
        self.ticks.feed(sensor_ts, now)
        self._update_gravity(accel, now)

        dt = self._delta_seconds(sensor_ts, now)
        if dt is None:
            return

        raw = [gyro[i] - self._bias[i] for i in range(3)]
        dps = [raw[i] * self.scale[i] for i in range(3)]
        self.live_dps = tuple(dps)
        self._update_zero(gyro, dps, now)

        if not self.enabled:
            return

        yaw, pitch = self._smoothed(self._to_screen(dps))
        yaw = suppress(yaw, self.tremor_dps)
        pitch = suppress(pitch, self.tremor_dps)

        # 增益按**合成角速度**算，不是各轴分开算。分开算的话斜着画一条线，
        # 两个分量各自都不快，增益就掉下去了，斜向移动会莫名其妙变慢。
        gain = accel_gain(math.hypot(yaw, pitch), self.accel)

        dx = yaw * dt * self.pixels_per_degree * gain
        self._acc_x += -dx if self.invert_x else dx
        if self.vertical_relative:
            dy = pitch * dt * self.vertical_pixels_per_degree * gain
            self._acc_y += -dy if self.invert_y else dy

    def _delta_seconds(self, sensor_ts: int, now: float):
        """两帧之间过了多久。优先用手柄自己的时间戳，它比挂钟稳得多。"""
        if self._last_ts is None:
            self._last_ts = sensor_ts
            return None
        ticks = (sensor_ts - self._last_ts) & 0xFFFFFFFF
        self._last_ts = sensor_ts
        if ticks == 0:
            return None
        dt = ticks * self.ticks.us_per_tick / 1_000_000.0
        # 掉线重连、时间戳绕回之类会算出荒谬的 dt，直接丢掉这一帧，
        # 否则一帧就能把光标甩到屏幕外面去。
        if dt <= 0 or dt > 0.2:
            return None
        return dt

    def _to_screen(self, dps):
        """按设置把角速度换到屏幕坐标系。拿不到重力方向时原样返回。"""
        if not self.world or self._grav is None:
            return dps
        r = world_space((dps[PITCH], dps[YAW], dps[ROLL]), self._grav)
        if r is None:
            return dps
        horiz, vert = r
        return (vert, horiz, dps[ROLL])

    def _smoothed(self, dps):
        """对最近几帧做加权平均，新帧权重高。"""
        self._hist.append((dps[YAW], dps[PITCH]))
        n = max(1, int(self.smoothing))
        while len(self._hist) > n:
            self._hist.pop(0)
        if len(self._hist) == 1:
            return self._hist[0]
        total = 0.0
        sx = sy = 0.0
        for i, (x, y) in enumerate(self._hist):
            w = i + 1
            sx += x * w
            sy += y * w
            total += w
        return sx / total, sy / total

    def _update_gravity(self, accel, now: float) -> None:
        """跟踪重力方向，算出「手柄实际上在以多快的角速度倾斜」。

        这个数字是独立于陀螺仪的第二个信息源，用来给陀螺仪的读数做裁判。
        """
        if not accel:
            self.static = False
            self._static_since = None
            return
        cal = self.scale_source
        if cal and cal.get("accel_scale"):
            g = [(accel[i] - cal["accel_bias"][i]) * cal["accel_scale"][i]
                 for i in range(3)]
        else:
            g = [accel[i] / 8192.0 for i in range(3)]

        mag = math.sqrt(sum(v * v for v in g))
        # 合矢量明显不等于 1G = 手柄正在被平移或甩动，这一帧的重力方向不可信
        if abs(mag - 1.0) > STATIC_G_TOLERANCE or mag < 1e-6:
            self.static = False
            self._static_since = None
            self._grav_mark = None
            return
        unit = [v / mag for v in g]

        if self._grav is None:
            self._grav = unit
        else:
            a = GRAVITY_LPF
            self._grav = [self._grav[i] * (1 - a) + unit[i] * a for i in range(3)]
            n = math.sqrt(sum(v * v for v in self._grav)) or 1.0
            self._grav = [v / n for v in self._grav]

        if self._grav_mark is None:
            self._grav_mark = (now, list(self._grav))
            return
        t0, g0 = self._grav_mark
        dt = now - t0
        if dt < GRAV_MARK_S:               # 基线太短，角度差全是噪声
            return
        dot = max(-1.0, min(1.0, sum(self._grav[i] * g0[i] for i in range(3))))
        self.tilt_dps = math.degrees(math.acos(dot)) / dt
        self._grav_mark = (now, list(self._grav))

        if self.tilt_dps < STATIC_TILT_DPS:
            self.static = True
            if self._static_since is None:
                self._static_since = now
        else:
            self.static = False
            self._static_since = None

    def _accel_says_static(self, now: float, seconds: float) -> bool:
        return (self._static_since is not None
                and now - self._static_since >= seconds)

    def _zero_by_accelerometer(self, gyro, dps, now: float) -> bool:
        """加速度计确认手柄没在转 —— 那陀螺仪此刻读到的**就是**零点偏差。

        这条路不看陀螺仪读数有多大，所以前面那个「零点偏大就锁死自己」的
        死循环在这里根本不存在；也不会像纯陀螺仪判据那样，把 3 度/秒的
        慢速有意转动误当成偏差吃掉——真的在转的话，重力方向会跟着变。
        """
        if not self._accel_says_static(now, STATIC_CONFIRM_S):
            return False
        # 俯仰和滚转是加速度计直接看得见的，可以放心校
        axes = [PITCH, ROLL]
        # 偏航看不见，要更长的确认时间：持续 2.5 秒的匀速偏航是一次很大的
        # 横扫，不常见；而零点偏差是一直都在的
        if self._accel_says_static(now, STATIC_CONFIRM_YAW_S):
            axes.append(YAW)
        # 「零点确实坏了」要同时满足两件事，而且都得**持续**够久：
        # 裁判持续确认静止，而且大读数本身也持续存在。
        #
        # 少了后半句就有个洞：安静一段之后开始转动，裁判要过一个测量基线
        # （1 秒）才反应过来说「在转」，而 _static_since 还记着刚才那段安静。
        # 于是转动刚开始的那一秒里，「已静止 3 秒」成立、读数又很大，
        # 正好撞进这条快速拉回的路 —— 零点一秒之内就被甩出去几十个单位。
        # 测试抓到的就是这个：本该纹丝不动，实际动了 39.9。
        big = max(abs(dps[i]) for i in axes) > MAINTAIN_MAX_DPS
        if big and self._accel_says_static(now, 0.0):
            if self._big_since is None:
                self._big_since = now
        else:
            self._big_since = None
        corrective = (self._accel_says_static(now, CORRECT_CONFIRM_S)
                      and self._big_since is not None
                      and now - self._big_since >= CORRECT_CONFIRM_S)
        for i in axes:
            err = abs(dps[i])
            if err <= MAINTAIN_MAX_DPS:
                rate = STATIC_BLEND          # 日常维护：本来就被压掉的那点残留
            elif corrective:
                rate = CORRECT_BLEND         # 零点确实坏了，快速拉回
            else:
                continue                     # 正在输出的读数，绝不吸收
            b = self._bias[i] + (gyro[i] - self._bias[i]) * rate
            self._bias[i] = max(-BIAS_LIMIT_RAW, min(BIAS_LIMIT_RAW, b))
        return True

    def _update_zero(self, gyro, dps, now: float) -> None:
        """零点会随温度慢慢漂，这里持续把它追回来。

        判据是**一段窗口内的平均绝对值**，不是瞬时值。原因：手握着手柄时
        瞬时角速度经常蹦到几度每秒，用瞬时值判断的话「静止」永远不成立，
        自动校准一次都不会触发，零点漂移就一路累积——光标越用越往一边爬。

        而手抖是围绕零点的随机游走，一段窗口平均下来接近零；有意的转动
        平均下来很大。所以用平均值判断，握在手里也能正常校准。
        """
        self._still_win.append((now, dps[YAW], dps[PITCH], dps[ROLL]))
        cutoff = now - AUTO_ZERO_SECONDS
        while self._still_win and self._still_win[0][0] < cutoff:
            self._still_win.pop(0)
        if len(self._still_win) < 64:
            return
        if now - self._still_win[0][0] < AUTO_ZERO_SECONDS * 0.8:
            return

        # 加速度计说了算 —— 而且是**完全**说了算。
        #
        # 之前这里写成了「优先路径」：裁判说静止就走它，说在转就**掉下去**
        # 接着跑旧的纯陀螺仪判据。结果 probe11 实测拍到了这一幕：
        #
        #   26.9s  裁判 在转 2.37  零点  -0.9     <- 正常
        #   27.9s  裁判 在转 1.97  零点 +27.3     <- 零点开始被带跑
        #   28.9s  裁判 在转 2.06  零点 +56.7
        #
        # 裁判明明判定「在转」，零点还是飞了——因为它掉进了旧路径，而旧路径
        # 只看陀螺仪自己的统计：当时读数才 1~3 度/秒，没超过 4 度/秒的瞬时
        # 门槛，窗口均值也不高，于是照样动手了。
        #
        # 问题不在旧路径的阈值调得对不对，而在于**它根本不该在这时候有发言权**。
        # 有加速度计数据时，这里必须直接结束；旧路径只在完全没有加速度计
        # 数据时才作为后备。
        if self._grav is not None:
            self._zero_by_accelerometer(gyro, dps, now)
            self._stuck_since = None
            return

        n = len(self._still_win)
        means = [sum(abs(w[k]) for w in self._still_win) / n for k in (1, 2, 3)]

        if self._unstick(means, now):
            return

        # 窗口均值和**当前这一帧**都要安静。少了后半句，一段安静之后的猛然
        # 甩动会在窗口还没反应过来的那一瞬间把零点带跑——实测就是这么炸的。
        if max(abs(v) for v in dps) > AUTO_ZERO_INSTANT_DPS:
            return
        for m in means:
            if m > AUTO_ZERO_STILL_DPS:
                return                      # 正在有意转动，别动零点

        for i in range(3):
            b = self._bias[i] + (gyro[i] - self._bias[i]) * AUTO_ZERO_BLEND
            # 零点不可能偏这么多。夹住它，别让一次误判滚成永久失控。
            self._bias[i] = max(-BIAS_LIMIT_RAW, min(BIAS_LIMIT_RAW, b))

    def _unstick(self, means, now: float) -> bool:
        """检测「零点已经坏掉」并自救，返回 True 表示这一帧已经处理完。

        判据是人做不到的事：读数持续很大，但**方差极小**。
        实测锁死时读数稳定在 -47.0 上下浮动不到 0.2 度/秒，连续几十秒。
        真人的快速转动方差是几十，慢速转动均值又不会这么大。两者分得很开。
        """
        if max(means) <= AUTO_ZERO_STILL_DPS:
            self._stuck_since = None
            return False
        n = len(self._still_win)
        for k in (1, 2, 3):
            vals = [w[k] for w in self._still_win]
            mu = sum(vals) / n
            var = sum((v - mu) ** 2 for v in vals) / n
            if abs(mu) > AUTO_ZERO_STILL_DPS and var < STUCK_STD_DPS ** 2:
                if self._stuck_since is None:
                    self._stuck_since = now
                elif now - self._stuck_since >= STUCK_SECONDS:
                    self.reset_zero()
                    self.stuck_recoveries += 1
                    self._stuck_since = None
                    return True
                return False
        self._stuck_since = None
        return False

    # -- 输出侧 -------------------------------------------------------
    def take(self):
        """取走累计位移并清零，返回整数像素。

        小数部分留在累加器里，不丢——否则慢速转动时每次都被截断成 0，
        光标会完全不动。这跟触摸板那边的处理是同一个道理。
        """
        ix = int(self._acc_x)
        iy = int(self._acc_y)
        self._acc_x -= ix
        self._acc_y -= iy
        return ix, iy

    def clear(self) -> None:
        self._acc_x = self._acc_y = 0.0
        self._hist.clear()


def tilt_angles(accel, calib=None):
    """用重力算出手柄的俯仰角和滚转角（度）——这是「绝对模式」的基础。

    加速度计静止时量到的就是重力方向，所以俯仰和滚转有绝对参照，不会漂。
    偏航没有：绕垂直轴转的时候重力方向不变，加速度计什么都感觉不到。
    这就是为什么水平方向只能用相对模式，而垂直方向可以两种都提供。

    实测验证：手柄平放桌上时重力落在 y 轴 +0.955G，合矢量 0.972G。
    """
    if calib and calib.get("accel_scale"):
        g = [(accel[i] - calib["accel_bias"][i]) * calib["accel_scale"][i]
             for i in range(3)]
    else:
        g = [accel[i] / 8192.0 for i in range(3)]
    x, y, z = g
    mag = math.sqrt(x * x + y * y + z * z)
    if mag < 0.3 or mag > 2.5:
        # 正在被甩动，这一帧的重力方向不可信
        return None
    pitch = math.degrees(math.atan2(z, math.sqrt(x * x + y * y) or 1e-9))
    roll = math.degrees(math.atan2(x, y if abs(y) > 1e-9 else 1e-9))
    return pitch, roll
