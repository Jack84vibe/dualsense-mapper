"""陀螺仪逻辑测试。数值全部来自 probe10 在真手柄上的实测。"""
import os, struct, sys, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps5mapper import dualsense as ds
from ps5mapper.gyro import (GyroPointer, TickEstimator, suppress, tilt_angles,
                            PITCH, YAW, ROLL)

# probe10 实测的这只手柄
REAL_SCALE = (0.06103, 0.06153, 0.06103)
CALIB = {"gyro_scale": REAL_SCALE,
         "accel_bias": (11.0, 13.0, 5.5),
         "accel_scale": (0.000123, 0.000122, 0.000122)}


def make_frame(gyro=(0, 0, 0), accel=(0, 0, 0), ts=0, bt=False):
    d = bytearray(76 if bt else 63)
    struct.pack_into("<3h", d, ds.GYRO_OFF, *gyro)
    struct.pack_into("<3h", d, ds.ACCEL_OFF, *accel)
    struct.pack_into("<I", d, ds.SENSOR_TS_OFF, ts)
    head = bytes([ds.INPUT_REPORT_BT, 0]) if bt else bytes([ds.INPUT_REPORT_USB])
    return head + bytes(d)


def test_parse_motion_usb_and_bt():
    for bt in (False, True):
        st = ds.parse_input_report(make_frame((-120, 7, 3000), (0, 8192, 40), 999, bt), bt)
        assert st is not None
        assert st.gyro == (-120, 7, 3000), (bt, st.gyro)
        assert st.accel == (0, 8192, 40)
        assert st.sensor_timestamp == 999


def test_calibration_roundtrip():
    b = bytearray(ds.CALIB_REPORT_LEN); b[0] = ds.CALIB_REPORT_ID
    put = lambda o, v: struct.pack_into("<h", b, o, v)
    put(1, 0); put(3, 0); put(5, 0)
    # denom = 2*17568 -> scale = 1080/35136 = 0.03073... 要落在合理区间
    for off, v in ((7, 8784), (9, -8784), (11, 8784), (13, -8784),
                   (15, 8784), (17, -8784)):
        put(off, v)
    put(19, 540); put(21, 540)
    for off, v in ((23, 8200), (25, -8100), (27, 8150),
                   (29, -8150), (31, 8100), (33, -8200)):
        put(off, v)
    cal = ds.parse_calibration(bytes(b))
    assert cal is not None
    full = 32767 * cal["gyro_scale"][0]
    assert 500 < full < 5000, full
    assert abs((8200 - cal["accel_bias"][0]) * cal["accel_scale"][0] - 1.0) < 0.02


def test_calibration_rejects_nonsense():
    assert ds.parse_calibration(b"") is None
    assert ds.parse_calibration(bytes([0x02]) + bytes(40)) is None
    # 全零 -> 分母为 0
    assert ds.parse_calibration(bytes([ds.CALIB_REPORT_ID]) + bytes(40)) is None
    # 刻度荒谬（这正是「1024 常数」那个错误会产生的情况）：拒绝
    b = bytearray(ds.CALIB_REPORT_LEN); b[0] = ds.CALIB_REPORT_ID
    put = lambda o, v: struct.pack_into("<h", b, o, v)
    for off in (7, 11, 15): put(off, 30000)
    for off in (9, 13, 17): put(off, -30000)
    put(19, 1); put(21, 1)                      # speed_2x 极小 -> 满量程极小
    for off, v in ((23, 8200), (25, -8100), (27, 8150),
                   (29, -8150), (31, 8100), (33, -8200)): put(off, v)
    assert ds.parse_calibration(bytes(b)) is None


def test_suppress_is_continuous_and_kills_tremor():
    t = 1.5
    assert suppress(5.0, t) == 5.0                  # 阈值以上原样通过
    assert abs(suppress(-5.0, t) + 5.0) < 1e-9
    # 阈值处两段相接，没有跳变
    assert abs(suppress(t, t) - t) < 1e-9
    assert abs(suppress(t - 1e-6, t) - (t - 1e-6)) < 1e-3
    # 手抖被压掉，有意的微调基本保留
    assert suppress(0.3, t) < 0.07
    assert suppress(1.4, t) > 1.3
    assert suppress(1.0, 0) == 1.0                  # 阈值 0 = 不压制


def test_tick_estimator_measures_instead_of_guessing():
    e = TickEstimator(window=0.5)
    assert e.us_per_tick == 0.33 and not e.measured
    # 模拟：每格 0.45 微秒，2 秒里走了 2/0.45e-6 格
    ticks_per_sec = 1_000_000.0 / 0.45
    e.feed(0, 0.0)
    e.feed(int(ticks_per_sec * 1.0), 1.0)
    assert e.measured
    assert abs(e.us_per_tick - 0.45) < 0.01, e.us_per_tick


def test_tick_estimator_rejects_wraparound():
    e = TickEstimator(window=0.5)
    e.feed(0, 0.0)
    e.feed(5, 1.0)          # 只走了 5 格却过了 1 秒 -> 200000 微秒/格，荒谬
    assert not e.measured and e.us_per_tick == 0.33


def _drive(gp, dps_yaw, seconds, hz=586.0, us_per_tick=0.45, state=None):
    """按给定角速度喂帧，返回累计像素。

    state 让连续多次调用共用同一条时间线。早先这里每次都把时间戳归零，
    于是每段的第一帧都被 GyroPointer 的「荒谬 dt」保护丢掉——那是测试脚手架
    的毛病，不是被测代码的。
    """
    if state is None:
        state = {"ts": 0, "now": 0.0}
    raw = int(dps_yaw / REAL_SCALE[YAW])
    ticks = int((1.0 / hz) * 1_000_000.0 / us_per_tick)
    for _ in range(int(seconds * hz)):
        state["ts"] += ticks
        state["now"] += 1.0 / hz
        gp.feed((0, raw, 0), state["ts"], now=state["now"])
    return gp.take()


def _expected_px(gp, dps_yaw, frames, hz=586.0, us_per_tick=0.45):
    """按被测代码实际会看到的量算期望值：原始值是整数，dt 也是整格的。"""
    raw = int(dps_yaw / REAL_SCALE[YAW])
    dt = int((1.0 / hz) * 1_000_000.0 / us_per_tick) * us_per_tick / 1_000_000.0
    return raw * REAL_SCALE[YAW] * dt * frames * gp.pixels_per_degree


def test_integration_matches_pixels_per_degree():
    gp = GyroPointer(CALIB)
    gp.ticks._value = 0.45          # 固定刻度，专测积分
    gp.enabled = True
    gp.tremor_dps = 0.0
    gp.smoothing = 1
    gp.accel = 0                    # 这条测的是积分本身，加速会掺进来
    dx, dy = _drive(gp, 90.0, 2.0)  # 90 度/秒 转 2 秒 ≈ 180 度
    expect = _expected_px(gp, 90.0, int(2.0 * 586) - 1)
    assert abs(dx - expect) < 2, (dx, expect)
    assert abs(dx - 180 * gp.pixels_per_degree) / (180 * gp.pixels_per_degree) < 0.03
    assert dy == 0


def test_disabled_produces_no_motion_but_still_tracks_zero():
    gp = GyroPointer(CALIB)
    gp.ticks._value = 0.45
    gp.enabled = False
    dx, dy = _drive(gp, 90.0, 1.0)
    assert (dx, dy) == (0, 0)
    assert abs(gp.live_dps[YAW] - 90.0) < 1.0   # 界面照样能显示当前角速度


def test_invert_flips_direction():
    for axis, inv in (("x", "invert_x"), ("y", "invert_y")):
        a = GyroPointer(CALIB); b = GyroPointer(CALIB)
        for g in (a, b):
            g.ticks._value = 0.45; g.enabled = True
            g.tremor_dps = 0.0; g.smoothing = 1; g.accel = 0
        setattr(b, inv, True)
        ra = _drive(a, 90.0, 1.0)
        rb = _drive(b, 90.0, 1.0)
        i = 0 if axis == "x" else 1
        if i == 0:
            assert ra[0] == -rb[0] and ra[0] != 0


def test_fractional_pixels_are_not_lost():
    """慢速转动时每帧不足 1 像素，截断会让光标完全不动。

    每帧只转约 0.005 度 = 0.13 像素。如果 take() 把小数丢掉，20 次取走
    全是 0，光标纹丝不动。这里验证小数被留在累加器里攒着。
    """
    gp = GyroPointer(CALIB)
    gp.ticks._value = 0.45
    gp.enabled = True; gp.tremor_dps = 0.0; gp.smoothing = 1; gp.accel = 0
    state = {"ts": 0, "now": 0.0}
    per_call = int(0.1 * 586)
    total = 0
    for _ in range(20):
        dx, _ = _drive(gp, 3.0, 0.1, state=state)
        total += dx
    # 第一帧只用来建立基准，不产生位移
    expect = _expected_px(gp, 3.0, 20 * per_call - 1)
    assert abs(total - expect) < 2, (total, expect)
    assert total > 100, "小数被丢了的话这里会远小于 100"


def test_bad_dt_frames_are_dropped():
    """时间戳绕回不该把光标甩到屏幕外。"""
    gp = GyroPointer(CALIB)
    gp.ticks._value = 0.45
    gp.enabled = True; gp.tremor_dps = 0.0; gp.smoothing = 1; gp.accel = 0
    gp.feed((0, 1000, 0), 0, now=0.0)
    gp.feed((0, 1000, 0), 0xFFFFFFF0, now=0.01)   # 巨大的 tick 差
    dx, dy = gp.take()
    assert abs(dx) < 5, dx


def test_auto_zero_removes_measured_drift():
    """用实测的零点 -1.29 喂静止数据，自动校准应该把漂移吃掉。"""
    gp = GyroPointer(CALIB)
    gp.ticks._value = 0.45
    gp.enabled = True; gp.tremor_dps = 0.0; gp.smoothing = 1; gp.accel = 0
    ts, now = 0, 0.0
    for _ in range(6000):                      # 约 10 秒静止
        ts += 1481; now += 1 / 586.0
        gp.feed((-1, -1, -3), ts, now=now)
    gp.take()
    before = abs(gp.live_dps[YAW])
    for _ in range(3000):
        ts += 1481; now += 1 / 586.0
        gp.feed((-1, -1, -3), ts, now=now)
    dx, dy = gp.take()
    assert abs(gp.live_dps[YAW]) <= before + 1e-9
    assert abs(dx) < 3 and abs(dy) < 3, (dx, dy)


def test_uncalibrated_still_works():
    gp = GyroPointer(None)
    gp.accel = 0
    assert not gp.calibrated
    assert 0.05 < 32767 * gp.scale[YAW] / 1000 < 5      # 名义满量程合理
    gp.ticks._value = 0.45; gp.enabled = True
    gp.tremor_dps = 0.0; gp.smoothing = 1
    dx, _ = _drive(gp, 90.0, 1.0)
    assert dx > 0


def test_tilt_angles_from_measured_resting_pose():
    """probe10 实测：平放时重力落在 y 轴 +0.955G。"""
    a = (int(0.008 / CALIB["accel_scale"][0] + CALIB["accel_bias"][0]),
         int(0.955 / CALIB["accel_scale"][1] + CALIB["accel_bias"][1]),
         int(0.180 / CALIB["accel_scale"][2] + CALIB["accel_bias"][2]))
    r = tilt_angles(a, CALIB)
    assert r is not None
    pitch, roll = r
    assert abs(roll) < 5, roll          # 平放，几乎没有侧倾
    assert abs(pitch) < 25, pitch


def test_tilt_angles_reject_shaken_frames():
    assert tilt_angles((0, 0, 0), CALIB) is None            # 自由落体
    big = (60000, 60000, 60000)
    assert tilt_angles(big, CALIB) is None                  # 正在被甩


def test_axis_order_matches_measurement():
    """probe10：抬前端 -> 第 0 个值；左右转 -> 第 1 个；拧门把手 -> 第 2 个。"""
    assert (PITCH, YAW, ROLL) == (0, 1, 2)
    gp = GyroPointer(CALIB)
    gp.ticks._value = 0.45; gp.enabled = True
    gp.tremor_dps = 0.0; gp.smoothing = 1
    ts, now = 0, 0.0
    for _ in range(586):                       # 只动 roll
        ts += 1481; now += 1 / 586.0
        gp.feed((0, 0, 3000), ts, now=now)
    dx, dy = gp.take()
    assert (dx, dy) == (0, 0), "滚转不该动光标"


# ---------------------------------------------------------------- 漂移回归
#
# 用户报的「用一段时间之后光标开始自己漂」。原因有两个，各写一个测试钉住。
#
# 注意每帧的时间戳增量：586 帧/秒、每格 0.45 微秒 -> 每帧 3792 格。
# 写错这个数的话 TickEstimator 会在跑到一半时把刻度**自己纠正过来**
# （它就是干这个的），于是前后两段用的 dt 不一样，期望值算不准。
# 第一版这几个测试就是这么写错的。
TICKS_PER_FRAME = 3792
HZ = 586.0


def _feed_constant(gp, dps_yaw, seconds, jitter=None):
    raw = int(dps_yaw / REAL_SCALE[YAW])
    ts, now = 0, 0.0
    for _ in range(int(seconds * HZ)):
        ts += TICKS_PER_FRAME
        now += 1.0 / HZ
        v = raw + (jitter() if jitter else 0)
        gp.feed((0, v, 0), ts, now=now)


def _fresh(tremor=3.0, accel=0):
    """默认**关掉加速**。绝大多数测试要验的是积分、零点、阈值这些，
    让加速掺进来的话，一条测试同时测两件事，失败了也说不清是谁的锅。"""
    gp = GyroPointer(CALIB)
    gp.ticks._value = 0.45
    gp.enabled = True
    gp.smoothing = 1
    gp.tremor_dps = tremor
    gp.accel = accel
    return gp


def test_auto_zero_absorbs_constant_bias():
    """零点偏移必须被自动校准吃掉，而且要收敛——不能一直漂。

    这是用户报的「用一段时间就开始漂」的正解。渐进压制救不了：
    偏 1 度/秒、阈值 3，压制后还剩 0.33 度/秒，十秒照样爬 83 像素。
    地板也救不了，1 度/秒远在地板之上。**只有把零点追平才有用。**
    """
    gp = _fresh()
    _feed_constant(gp, 1.0, 6.0)
    gp.take()                                   # 前 6 秒允许有收敛过程
    _feed_constant(gp, 1.0, 6.0)
    dx, _ = gp.take()
    assert abs(dx) <= 5, "零点已经追平之后仍在漂：%d 像素/6 秒" % dx


def test_without_auto_zero_a_constant_bias_really_drifts():
    """证明上面那个测试确实测到了东西：关掉自动校准，同样的输入会爬走。"""
    import ps5mapper.gyro as g
    old = g.AUTO_ZERO_STILL_DPS
    g.AUTO_ZERO_STILL_DPS = 0.0
    try:
        gp = _fresh()
        _feed_constant(gp, 1.0, 10.0)
        dx, _ = gp.take()
        assert abs(dx) > 50, "没有自动校准时本应明显漂移，实际只有 %d" % dx
    finally:
        g.AUTO_ZERO_STILL_DPS = old


def test_floor_kills_residue_too_small_for_auto_zero():
    """地板管的是另一段：小到自动校准都懒得动的残留。

    0.15 度/秒看着微不足道，但积分不挑食 —— 一分钟就是 9 度、225 像素。
    """
    import ps5mapper.gyro as g
    old = g.AUTO_ZERO_STILL_DPS
    g.AUTO_ZERO_STILL_DPS = 0.0                 # 只让地板起作用
    try:
        gp = _fresh()
        _feed_constant(gp, 0.15, 20.0)
        dx, _ = gp.take()
        assert dx == 0, "地板没拦住 0.15 度/秒的残留：%d 像素" % dx
    finally:
        g.AUTO_ZERO_STILL_DPS = old


def test_auto_zero_engages_while_held_not_only_on_a_table():
    """握在手里也要能自动校准。

    原来的判据是瞬时角速度连续 1 秒低于 2 度/秒 —— 手握着根本达不到，
    于是自动校准一次都不触发，零点一路漂。这里模拟「握着不动」：
    围绕一个偏移值的随机抖动，校准应该把偏移追平。
    """
    import random
    rnd = random.Random(7)
    gp = _fresh()
    offset = 1.5
    sigma = 1.2 / REAL_SCALE[YAW]
    _feed_constant(gp, offset, 12.0, jitter=lambda: int(rnd.gauss(0, sigma)))
    target = offset / REAL_SCALE[YAW]
    assert abs(gp._bias[YAW] - target) < abs(target) * 0.5, \
        "零点没有被追平：bias=%.1f 应接近 %.1f" % (gp._bias[YAW], target)


def test_slow_deliberate_motion_is_not_eaten_as_drift():
    """反过来也要成立：每秒 3 度的慢速有意移动不能被当成漂移吃掉。

    第一版把窗口定成 0.4 秒、门槛 6 度/秒，正好把这种移动误判成零点漂移，
    测试直接抓到了：本该走约 300 像素，只走了 29。
    """
    gp = _fresh(tremor=0.0)
    _feed_constant(gp, 3.0, 4.0)
    dx, _ = gp.take()
    expect = 3.0 * 4.0 * gp.pixels_per_degree
    assert dx > expect * 0.8, "慢速移动被吃掉了：走了 %d，本该约 %d" % (dx, expect)


def test_floor_does_not_eat_real_small_moves():
    """地板要小到人手做不出来。0.5 度/秒的微调必须留着。"""
    assert suppress(0.5, 3.0) != 0.0
    assert suppress(0.19, 3.0) == 0.0
    assert suppress(-0.19, 3.0) == 0.0
    assert suppress(1.0, 0) == 1.0


# ---------------------------------------------------------------- 加速曲线
#
# 解「又要够快、又要不抖」的矛盾。用户把灵敏度从 25 拉到 80 才觉得够快，
# 结果手抖也被放大了 3.2 倍 —— 固定增益下这两件事是绑死的。

def test_accel_off_is_a_no_op():
    """强度 0 必须和以前完全一样，不能偷偷改变手感。"""
    from ps5mapper.gyro import accel_gain
    for v in (0.0, 1.0, 12.0, 50.0, 300.0):
        assert accel_gain(v, 0) == 1.0


def test_accel_damps_slow_and_preserves_fast():
    from ps5mapper.gyro import accel_gain, ACCEL_SLOW_DPS, ACCEL_FAST_DPS
    slow = accel_gain(2.0, 60)          # 手抖的量级
    fast = accel_gain(200.0, 60)        # 大幅横扫的量级
    assert abs(slow - 0.55) < 1e-9, slow      # 1 - 0.75 * 0.60
    assert accel_gain(2.0, 100) == 0.25       # 最强时慢速只剩四分之一
    assert fast == 1.0
    assert accel_gain(ACCEL_SLOW_DPS, 60) == slow
    assert accel_gain(ACCEL_FAST_DPS, 60) == 1.0


def test_accel_curve_is_monotonic_and_smooth():
    """不能在某个速度上突然窜一下——那种手感比抖还糟。"""
    from ps5mapper.gyro import accel_gain
    xs = [i * 2.0 for i in range(120)]
    ys = [accel_gain(x, 70) for x in xs]
    for a, b in zip(ys, ys[1:]):
        assert b >= a - 1e-12, "增益出现回落"
    jumps = [abs(b - a) for a, b in zip(ys, ys[1:])]
    assert max(jumps) < 0.05, "曲线有台阶，最大跳变 %.3f" % max(jumps)


def test_accel_is_symmetric_for_negative_rates():
    from ps5mapper.gyro import accel_gain
    for v in (3.0, 40.0, 150.0):
        assert accel_gain(v, 60) == accel_gain(-v, 60)


def test_accel_uses_combined_speed_not_per_axis():
    """斜着划的时候，两个分量各自都不快，但合速度是快的。

    分轴算增益的话，斜向移动会莫名其妙比横向慢一截。
    """
    gp = _fresh(tremor=0.0, accel=80)
    raw = int(60.0 / REAL_SCALE[YAW])          # 每轴 60 度/秒，合成约 85
    ts, now = 0, 0.0
    for _ in range(int(HZ)):
        ts += TICKS_PER_FRAME
        now += 1.0 / HZ
        gp.feed((raw, raw, 0), ts, now=now)
    dx, dy = gp.take()

    gp2 = _fresh(tremor=0.0, accel=80)
    ts, now = 0, 0.0
    for _ in range(int(HZ)):
        ts += TICKS_PER_FRAME
        now += 1.0 / HZ
        gp2.feed((0, raw, 0), ts, now=now)         # 只有横向，同样每轴 60
    dx2, _ = gp2.take()
    assert dx > dx2 * 1.2, "斜向的增益没有按合速度算：%d vs %d" % (dx, dx2)


def test_high_sensitivity_with_accel_beats_flat_gain_on_tremor():
    """同样是灵敏度 80，开加速之后手抖造成的位移应该明显变小，
    而大幅横扫的位移基本不受影响 —— 这就是这条曲线存在的理由。"""
    import random

    def run(accel, dps, seconds, jitter=False):
        gp = _fresh(tremor=3.0, accel=accel)
        gp.pixels_per_degree = 80.0
        gp.vertical_pixels_per_degree = 80.0
        rnd = random.Random(11)
        ts, now, moved = 0, 0.0, 0
        for _ in range(int(seconds * HZ)):
            ts += TICKS_PER_FRAME
            now += 1.0 / HZ
            v = dps + (rnd.gauss(0, 4.0) if jitter else 0)
            gp.feed((0, int(v / REAL_SCALE[YAW]), 0), ts, now=now)
        dx, _ = gp.take()
        return abs(dx)

    tremor_flat = run(0, 0.0, 2.0, jitter=True)
    tremor_accel = run(80, 0.0, 2.0, jitter=True)
    sweep_flat = run(0, 150.0, 1.0)
    sweep_accel = run(80, 150.0, 1.0)
    assert tremor_accel < tremor_flat * 0.6, (tremor_flat, tremor_accel)
    assert sweep_accel > sweep_flat * 0.95, (sweep_flat, sweep_accel)


# -------------------------------------------------- probe11 抓到的真实故障
#
# 2026-09-12 实测报告里的原始时序：
#   72.2s  角速度 +10.39  零点   +68.2  窗口均值 0.34  校准 动
#   72.7s  角速度 -78.63  零点  +770.4  窗口均值 6.82  校准 动
#   73.2s  角速度 -47.00  零点  +770.4  窗口均值 19.95 校准 停 ← 从此永远停
# 之后手柄**放在桌上没人碰**，20 秒照样漂了 15794 像素。

def _quiet_then_swing(gp, quiet_s=4.0, swing_dps=200.0, swing_s=1.0):
    """先安静一段（让窗口装满安静数据），再突然猛甩——就是实测炸掉的那个序列。"""
    st = {"ts": 0, "now": 0.0}
    _feed_constant(gp, 0.0, quiet_s)
    raw = int(swing_dps / REAL_SCALE[PITCH])
    ts, now = int(quiet_s * HZ) * TICKS_PER_FRAME, quiet_s
    for i in range(int(swing_s * HZ)):
        ts += TICKS_PER_FRAME
        now += 1.0 / HZ
        # 来回甩，模拟真实动作
        v = raw if (i // 40) % 2 == 0 else -raw
        gp.feed((v, 0, 0), ts, now=now)
    return ts, now


def test_sudden_swing_after_quiet_does_not_poison_the_zero():
    """真凶：窗口均值是滞后指标，安静之后猛甩的那一瞬间它还没反应过来。"""
    gp = _fresh()
    _quiet_then_swing(gp)
    assert abs(gp._bias[PITCH]) < 50, \
        "零点被猛甩带跑了：%.1f（实测炸到过 +770）" % gp._bias[PITCH]


def test_bias_can_never_reach_absurd_values():
    """即使判定逻辑再出漏子，零点也不该越过物理上限。"""
    import ps5mapper.gyro as g
    gp = _fresh()
    # 强行把门槛开到最大，模拟「判定完全失效」的最坏情况
    old_i, old_s = g.AUTO_ZERO_INSTANT_DPS, g.AUTO_ZERO_STILL_DPS
    g.AUTO_ZERO_INSTANT_DPS = 1e9
    g.AUTO_ZERO_STILL_DPS = 1e9
    try:
        _feed_constant(gp, 500.0, 6.0)
        assert abs(gp._bias[PITCH]) <= g.BIAS_LIMIT_RAW + 1e-6, gp._bias
    finally:
        g.AUTO_ZERO_INSTANT_DPS, g.AUTO_ZERO_STILL_DPS = old_i, old_s


def test_stuck_zero_recovers_by_itself():
    """锁死自救：读数很大但方差极小 —— 人做不出这种动作，只能是零点坏了。

    直接把实测的坏零点塞进去（+770 原始单位 = 47 度/秒），
    然后喂完全静止的数据，看它能不能自己爬出来。
    """
    gp = _fresh()
    gp._bias = [770.4, 35.6, -79.7]          # 实测报告里的那组数
    _feed_constant(gp, 0.0, 10.0)
    assert gp.stuck_recoveries >= 1, "没有自救"
    assert abs(gp.live_dps[PITCH]) < 2.0, \
        "自救之后读数仍然不正常：%.2f" % gp.live_dps[PITCH]


def test_stuck_zero_recovery_stops_the_runaway_drift():
    """自救的意义在于位移真的停下来，不只是数字好看。"""
    gp = _fresh()
    gp._bias = [770.4, 35.6, -79.7]
    _feed_constant(gp, 0.0, 10.0)
    gp.take()                                 # 丢掉自救之前那一截
    _feed_constant(gp, 0.0, 10.0)
    dx, dy = gp.take()
    assert abs(dy) <= 3, "自救之后仍在漂：%d 像素/10 秒" % dy


def test_real_fast_motion_is_not_mistaken_for_a_stuck_zero():
    """反过来不能误伤：真人的快速转动方差很大，不该被当成坏零点。"""
    import random
    rnd = random.Random(3)
    gp = _fresh()
    raw = 150.0 / REAL_SCALE[PITCH]
    ts, now = 0, 0.0
    for _ in range(int(HZ * 8)):
        ts += TICKS_PER_FRAME
        now += 1.0 / HZ
        gp.feed((int(raw * rnd.uniform(-1, 1)), 0, 0), ts, now=now)
    assert gp.stuck_recoveries == 0, "把真实的快速转动误判成了坏零点"


def test_slow_steady_turn_is_not_mistaken_for_a_stuck_zero():
    """更刁钻的一条：匀速慢转方差也小，但幅度不大，不该触发自救。"""
    gp = _fresh()
    _feed_constant(gp, 2.0, 10.0)             # 低于 AUTO_ZERO_STILL_DPS
    assert gp.stuck_recoveries == 0


# ------------------------------------------------ 加速度计当裁判
#
# 纯陀螺仪判据有个原理上堵不住的洞：在一段有限时间里，「零点偏了 3 度/秒」和
# 「你正在以 3 度/秒匀速转」产生的数字完全相同。阈值往哪边挪都只是换一种错法。
# 加速度计看得见重力方向，能把这两件事分开。

G_DOWN = (0, 8192, 0)          # 手柄平放时重力落在 y 轴（probe10 实测）


def _accel_at(pitch_deg):
    """把手柄绕 x 轴倾斜若干度之后，加速度计该读到什么。"""
    r = math.radians(pitch_deg)
    return (0, int(8192 * math.cos(r)), int(8192 * math.sin(r)))


def _feed_with_accel(gp, dps_pitch, seconds, accel_fn, start=None):
    """喂数据，同时按 accel_fn(t) 给出对应的加速度计读数。"""
    st = start or {"ts": 0, "now": 0.0}
    raw = int(dps_pitch / REAL_SCALE[PITCH])
    for _ in range(int(seconds * HZ)):
        st["ts"] += TICKS_PER_FRAME
        st["now"] += 1.0 / HZ
        gp.feed((raw, 0, 0), st["ts"], accel_fn(st["now"]), now=st["now"])
    return st


def test_static_controller_gets_its_zero_corrected_fast():
    """手柄没动（重力方向不变），陀螺仪却读到 3 度/秒 —— 那就是零点偏差。

    纯陀螺仪判据在这里会犹豫（3 度/秒超过 2.5 的门槛，看着像在转），
    加速度计一票定案。
    """
    gp = _fresh()
    _feed_with_accel(gp, 3.0, 6.0, lambda t: G_DOWN)
    target = 3.0 / REAL_SCALE[PITCH]
    assert abs(gp._bias[PITCH] - target) < abs(target) * 0.4, \
        "静止时零点没被追平：%.1f 应接近 %.1f" % (gp._bias[PITCH], target)
    assert gp.static is True


def test_real_slow_rotation_is_not_absorbed():
    """反过来：真的在以 3 度/秒慢慢转，重力方向会跟着变，不能把它当零点吃掉。

    这正是上一版修不掉的那个洞 —— 它靠纯陀螺仪统计，分不出这两种情况。
    """
    gp = _fresh(tremor=0.0)
    _feed_with_accel(gp, 3.0, 6.0, lambda t: _accel_at(3.0 * t))
    assert abs(gp._bias[PITCH]) < 20, \
        "真实的慢速转动被当成零点吃掉了：bias=%.1f" % gp._bias[PITCH]
    dx, dy = gp.take()
    assert abs(dy) > 3.0 * 5.0 * gp.vertical_pixels_per_degree * 0.5, \
        "慢速转动的位移被吃掉了：%d" % dy


def test_shaking_does_not_count_as_static():
    """被甩动时加速度计读到的是手的线性加速度，合矢量不等于 1G，数据不可信。"""
    gp = _fresh()
    _feed_with_accel(gp, 0.0, 3.0,
                     lambda t: (int(8192 * 2.5), int(8192 * 2.0), 0))
    assert gp.static is False
    assert gp._static_since is None


def test_stuck_zero_recovers_with_accelerometer():
    """实测那组坏零点要能被拉回来。

    **恢复不是瞬间的，大约要 4~5 秒**，这是为了安全付的代价：
    快速拉回要求「裁判确认静止」和「大读数持续存在」各自都攒够 2 秒，
    而裁判本身还要 1 秒的测量基线才能建立。少了这些确认，转动刚开始的
    那一秒就会被误判成坏零点（测试 test_zero_is_untouched... 抓的就是这个）。
    这里把这个时间成本写进断言，将来谁想缩短它，会先看到这条。
    """
    gp = _fresh()
    gp._bias = [770.4, 35.6, -79.7]
    _feed_with_accel(gp, 0.0, 4.0, lambda t: G_DOWN)
    assert abs(gp._bias[PITCH]) > 200, "4 秒就恢复了？确认时间是不是被改短了"
    _feed_with_accel(gp, 0.0, 4.0, lambda t: G_DOWN,
                     start={"ts": int(4.0 * HZ) * TICKS_PER_FRAME, "now": 4.0})
    assert abs(gp._bias[PITCH]) < 20, \
        "8 秒之后坏零点仍未拉回：%.1f" % gp._bias[PITCH]


def test_yaw_needs_longer_confirmation_than_pitch():
    """偏航在加速度计里是看不见的（绕垂直轴转时重力方向不变），
    所以它的确认时间要长得多，免得把一次大幅横扫误当成零点。"""
    import ps5mapper.gyro as g
    assert g.STATIC_CONFIRM_YAW_S > g.STATIC_CONFIRM_S * 2

    gp = _fresh()
    st = {"ts": 0, "now": 0.0}
    raw = int(5.0 / REAL_SCALE[YAW])
    # 只跑 1.5 秒：够俯仰确认，不够偏航确认
    for _ in range(int(1.5 * HZ)):
        st["ts"] += TICKS_PER_FRAME
        st["now"] += 1.0 / HZ
        gp.feed((0, raw, 0), st["ts"], G_DOWN, now=st["now"])
    assert abs(gp._bias[YAW]) < 10, "偏航零点确认得太急：%.1f" % gp._bias[YAW]


def test_works_without_accelerometer_data():
    """没有加速度计数据时退回原来那套，不能崩。"""
    gp = _fresh()
    _feed_constant(gp, 0.0, 4.0)          # 这个辅助函数不传 accel
    assert gp.static is False
    dx, dy = gp.take()
    assert (dx, dy) == (0, 0)


# ----------------------------------------- 用户报的「极慢移动后漂移」
#
# 用户的稳定复现：先静止 -> 非常非常缓慢地转 -> 停住 -> 开始漂。
#
# 我怀疑是加速度计裁判的门槛造成的盲区：极慢转动低于门槛 -> 判定「没在转」
# -> 这段真实转动被当成零点偏差吸收 -> 一停手零点就偏了，反向漂。
#
# **但我没能在模拟里复现出来。** 我写了一条反向验证（把门槛调回出问题的
# 1.2、并解除新加的保险），期望它重新漂起来 —— 结果位移是 0。既然连「坏的
# 版本」在我的模拟里都不漂，那说明我的模拟根本没抓住真实情况，基于它写的
# 「已修复」测试全都不作数，所以删掉了，免得给人一种已经验证过的错觉。
#
# 下面两条不一样：它们直接检验新代码自己的行为，不依赖那个没验证成功的复现。


def test_signal_sized_readings_are_never_absorbed():
    """正在推动光标的读数，一律不许学进零点。"""
    gp = _fresh()
    # 裁判误判成静止（重力不动），但读数有 5 度/秒 —— 远超日常维护的上限。
    # 只有持续确认 2 秒之后才允许当成坏零点拉回，1 秒内不该动。
    _feed_with_accel(gp, 5.0, 1.0, lambda t: G_DOWN)
    assert abs(gp._bias[PITCH]) < 10, \
        "1 秒内就把信号量级的读数吸收进零点了：%.1f" % gp._bias[PITCH]


def test_genuinely_broken_zero_is_still_corrected():
    """但零点真坏了的时候还是要能拉回来 —— 别把保险做成新的死锁。"""
    gp = _fresh()
    gp._bias = [770.4, 35.6, -79.7]
    _feed_with_accel(gp, 0.0, 8.0, lambda t: G_DOWN)
    assert abs(gp._bias[PITCH]) < 200, "坏零点没被拉回：%.1f" % gp._bias[PITCH]


# ------------------------------------ probe11 第 3 轮拍到的「裁判说了不算」
#
# 报告里的原始时序（B 段，用户正在极慢地转动手柄）：
#
#   26.9s  裁判 在转 2.37  零点  -0.9     <- 正常
#   27.9s  裁判 在转 1.97  零点 +27.3     <- 零点开始被带跑
#   28.9s  裁判 在转 2.06  零点 +56.7
#   30.9s  裁判 在转 2.45  零点 +52.4
#
# 加速度计明明判定「在转」，零点还是飞了 52 个原始单位（约 3.2 度/秒）。
# 原因：我把裁判做成了「优先路径」，裁判说在转时代码会**掉下去**继续跑旧的
# 纯陀螺仪判据，而旧判据只看陀螺仪自己的统计，照样动手。
#
# 这批测试盯的是「谁有发言权」，不是阈值调得对不对。

def _slow_real_rotation(gp, dps=2.5, seconds=8.0):
    """真实的慢速转动：陀螺仪读到 dps，重力方向也确实在以 dps 变化。"""
    st = {"ts": 0, "now": 0.0}
    _feed_with_accel(gp, 0.0, 3.0, lambda t: G_DOWN, start=st)   # 先安静，装满窗口
    t0 = st["now"]
    _feed_with_accel(gp, dps, seconds,
                     lambda t: _accel_at(dps * (t - t0)), start=st)
    return st


def test_zero_is_untouched_while_the_referee_says_rotating():
    """实测那一幕：裁判说在转的整段时间里，零点必须纹丝不动。"""
    gp = _fresh()
    before = list(gp._bias)
    _slow_real_rotation(gp, dps=2.5)
    assert gp.static is False, "这段本该被判成「在转」"
    moved = abs(gp._bias[PITCH] - before[PITCH])
    assert moved < 5, "裁判说在转，零点却动了 %.1f（实测炸到过 +52）" % moved


def test_the_legacy_path_really_was_the_culprit():
    """反向验证：恢复成「掉下去跑旧判据」的写法，同样的输入应该重新把零点带跑。

    上一轮我写的反向验证没能复现，于是那批「已修复」测试全部作废。
    这一条是真的能复现 —— 它直接调用旧路径，不依赖任何对真实手感的模拟。
    """
    gp = _fresh()
    st = {"ts": 0, "now": 0.0}
    _feed_with_accel(gp, 0.0, 3.0, lambda t: G_DOWN, start=st)
    t0 = st["now"]
    dps = 2.5
    raw = int(dps / REAL_SCALE[PITCH])
    # 手工重放旧逻辑：裁判说在转 -> 不走加速度计 -> 直接跑旧的纯陀螺仪判据
    for _ in range(int(8.0 * HZ)):
        st["ts"] += TICKS_PER_FRAME
        st["now"] += 1.0 / HZ
        gp._update_gravity(_accel_at(dps * (st["now"] - t0)), st["now"])
        d = [(raw - gp._bias[PITCH]) * REAL_SCALE[PITCH], 0.0, 0.0]
        gp._still_win.append((st["now"], d[1], d[0], d[2]))
        cutoff = st["now"] - 2.5
        while gp._still_win and gp._still_win[0][0] < cutoff:
            gp._still_win.pop(0)
        if len(gp._still_win) < 64:
            continue
        # 这就是被删掉的那几行
        if max(abs(v) for v in d) > 4.0:
            continue
        n = len(gp._still_win)
        if any(sum(abs(w[k]) for w in gp._still_win) / n > 2.5 for k in (1, 2, 3)):
            continue
        gp._bias[PITCH] += (raw - gp._bias[PITCH]) * 0.02
    assert gp._bias[PITCH] > 20, \
        "旧路径本该把零点带跑，实际只到 %.1f" % gp._bias[PITCH]


def test_referee_still_maintains_the_zero_when_truly_still():
    """把旧路径关掉之后，真正静止时的零点维护不能跟着失效。"""
    gp = _fresh()
    _feed_with_accel(gp, 0.8, 8.0, lambda t: G_DOWN)   # 静止，但读数偏 0.8 度/秒
    target = 0.8 / REAL_SCALE[PITCH]
    assert abs(gp._bias[PITCH] - target) < abs(target) * 0.5, \
        "静止时零点没被维护：%.1f 应接近 %.1f" % (gp._bias[PITCH], target)


def test_gyro_only_path_survives_without_accelerometer():
    """没有加速度计数据时，旧路径仍然是唯一的后备，不能被误删。"""
    gp = _fresh()
    _feed_constant(gp, 0.5, 8.0)          # 这个辅助函数喂的是**偏航**，且不传 accel
    assert gp._grav is None
    assert abs(gp._bias[YAW]) > 1, "没有加速度计时零点完全不动了"


# ------------------------------------------------ 屏幕坐标系（消侧倾影响）

def test_world_space_reduces_exactly_to_local_when_level():
    """端正时必须**精确**退化成原来的偏航/俯仰。

    这条是整个设计的前提：否则开关这个功能会让已经调好的灵敏度、
    反向设置全部失准，用户得重调一遍。
    """
    from ps5mapper.gyro import world_space
    h, v = world_space((2.0, 5.0, 1.0), (0.0, 1.0, 0.0))
    assert abs(h - 5.0) < 1e-12 and abs(v - 2.0) < 1e-12


def test_world_space_compensates_roll():
    """手柄侧倾 90 度立起来：原来的「俯仰」现在应该推动光标左右走。"""
    from ps5mapper.gyro import world_space
    up = (-1.0, 0.0, 0.0)                     # 侧倾 90°，天轴变成手柄的 -X
    h, v = world_space((2.0, 5.0, 1.0), up)
    assert abs(h + 2.0) < 1e-9, h             # 水平 = -俯仰
    assert abs(v - 5.0) < 1e-9, v             # 垂直 = 偏航


def test_world_space_is_continuous_across_roll():
    """绕侧倾角扫一圈，输出不能有跳变 —— 那种手感比不补偿还糟。"""
    from ps5mapper.gyro import world_space
    prev = None
    for deg in range(0, 361):
        r = math.radians(deg)
        up = (-math.sin(r), math.cos(r), 0.0)
        out = world_space((2.0, 5.0, 1.0), up)
        assert out is not None
        if prev is not None:
            assert abs(out[0] - prev[0]) < 0.3 and abs(out[1] - prev[1]) < 0.3, \
                "侧倾 %d 度处出现跳变" % deg
        prev = out


def test_world_space_degrades_when_pointing_at_the_sky():
    """指向正上或正下时没有「屏幕水平」可言，要退回手柄自身坐标系。"""
    from ps5mapper.gyro import world_space
    assert world_space((1, 1, 1), (0.0, 0.0, 1.0)) is None
    assert world_space((1, 1, 1), (0.0, 0.0, -1.0)) is None


def test_pointer_uses_world_space_only_when_gravity_known():
    gp = _fresh(tremor=0.0)
    assert gp.world is True
    assert gp._to_screen((2.0, 5.0, 1.0)) == (2.0, 5.0, 1.0)   # 还没有重力方向
    gp._grav = (0.0, 1.0, 0.0)
    assert gp._to_screen((2.0, 5.0, 1.0)) == (2.0, 5.0, 1.0)   # 端正 -> 一样
    gp._grav = (-1.0, 0.0, 0.0)                                # 侧倾 90°
    out = gp._to_screen((2.0, 5.0, 1.0))
    assert abs(out[YAW] + 2.0) < 1e-9 and abs(out[PITCH] - 5.0) < 1e-9


def test_local_mode_ignores_roll():
    gp = _fresh(tremor=0.0)
    gp.world = False
    gp._grav = (-1.0, 0.0, 0.0)
    assert gp._to_screen((2.0, 5.0, 1.0)) == (2.0, 5.0, 1.0)
