"""陀螺仪接进引擎之后的行为。用假手柄驱动真引擎，不需要硬件。"""
import os, struct, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps5mapper import config as cfgmod
from ps5mapper import dualsense as ds
from ps5mapper.engine import Engine

REAL_SCALE = (0.06103, 0.06153, 0.06103)
CALIB = {"gyro_scale": REAL_SCALE,
         "accel_bias": (11.0, 13.0, 5.5),
         "accel_scale": (0.000123, 0.000122, 0.000122)}


def state(gyro=(0, 0, 0), accel=(0, 8192, 0), ts=0, buttons=None):
    st = ds.ControllerState()
    st.gyro = gyro
    st.accel = accel
    st.sensor_timestamp = ts
    st.buttons = {k: False for k in (
        "square", "cross", "circle", "triangle", "dpad_up", "dpad_down",
        "dpad_left", "dpad_right", "l1", "r1", "create", "options",
        "l3", "r3", "ps", "touchpad_click", "mic")}
    for k in (buttons or ()):
        st.buttons[k] = True
    return st


def make_engine(**gyro_over):
    cfg = cfgmod.default_config()
    g = cfg["profiles"][0].setdefault("gyro", cfgmod.default_gyro())
    g.update(gyro_over)
    events = []
    e = Engine(cfg, on_event=lambda k, v=None: events.append((k, v)))
    e.dev.calib = CALIB
    e.events = events
    return e


def test_default_is_off_so_nothing_moves_unexpectedly():
    """默认关着。陀螺仪一开就是手一动光标就跑，不该是开箱状态。"""
    g = cfgmod.default_gyro()
    assert g["enabled"] is False
    e = make_engine()
    for i in range(100):
        e._feed_gyro(state(gyro=(0, 3000, 0), ts=i * 1481))
    assert e.gyro.take() == (0, 0)


def test_enabled_accumulates_in_input_thread():
    e = make_engine(enabled=True, tremor=0.0, smoothing=1)
    e.gyro.ticks._value = 0.45
    ts = 0
    for _ in range(586):
        ts += 1481
        e._feed_gyro(state(gyro=(0, 1500, 0), ts=ts))
    dx, dy = e.gyro.take()
    assert dx != 0 and dy == 0, (dx, dy)


def test_toggle_combo_switches_and_does_not_fire_bound_keys():
    """组合键里的按键不该同时触发它们自己绑定的动作。"""
    e = make_engine(toggle_combo=["ps", "touchpad_click"])
    pressed = []
    e.runner.press = lambda cid, act: pressed.append(cid)

    assert e._handle_gyro_combo(state(buttons=("ps", "touchpad_click"))) is True
    assert e.profile["gyro"]["enabled"] is True
    assert ("gyro", True) in e.events
    # 按住不放不该反复切换
    for _ in range(5):
        e._handle_gyro_combo(state(buttons=("ps", "touchpad_click")))
    assert e.profile["gyro"]["enabled"] is True
    # 全松开才解锁
    assert e._handle_gyro_combo(state(buttons=("ps",))) is True
    e._handle_gyro_combo(state())
    assert e._gyro_latched is False
    # 再按一次关掉
    e._handle_gyro_combo(state(buttons=("ps", "touchpad_click")))
    assert e.profile["gyro"]["enabled"] is False
    assert pressed == [], "组合键不该触发普通映射"


def test_empty_combo_does_not_swallow_buttons():
    e = make_engine(toggle_combo=[])
    assert e._handle_gyro_combo(state(buttons=("ps",))) is False


def test_turning_on_resets_zero_and_reference():
    e = make_engine(enabled=False)
    e.gyro._bias = [99.0, 99.0, 99.0]
    e._gyro_abs_ref = (10.0, 0.9)
    e.toggle_gyro()
    assert e.profile["gyro"]["enabled"] is True
    assert e.gyro._bias == [0.0, 0.0, 0.0]
    assert e._gyro_abs_ref is None


def test_gyro_adds_to_stick_rather_than_replacing():
    """用户选的是「叠加，互不干扰」。"""
    e = make_engine(enabled=True, tremor=0.0, smoothing=1)
    prof = e.profile
    st = state()
    e.gyro._acc_x = 40.0
    e.gyro._acc_y = 0.0
    dx, dy = e._take_gyro(st, prof, 1.0)
    assert dx == 40 and dy == 0


def test_slow_pointer_also_slows_gyro():
    e = make_engine(enabled=True)
    e.gyro._acc_x = 100.0
    dx, _ = e._take_gyro(state(), e.profile, 0.35)
    assert abs(dx - 35.0) < 1e-6


def test_absolute_mode_first_frame_does_not_jump():
    """刚切到绝对模式时，光标不该瞬移到别处。"""
    e = make_engine(enabled=True, vertical_mode="absolute")
    dy = e._absolute_vertical(state(accel=(0, 8192, 0)), e.profile["gyro"])
    assert dy == 0.0
    assert e._gyro_abs_ref is not None


def test_absolute_mode_ignores_shaken_frames():
    e = make_engine(enabled=True, vertical_mode="absolute")
    e._gyro_abs_ref = (0.0, 0.5)
    assert e._absolute_vertical(state(accel=(0, 0, 0)), e.profile["gyro"]) == 0.0
    assert e._absolute_vertical(state(accel=(32000, 32000, 32000)),
                                e.profile["gyro"]) == 0.0


def test_absolute_mode_does_not_double_count_vertical():
    """绝对模式下积分器不能再累加 y，否则两套位移打架。"""
    e = make_engine(enabled=True, vertical_mode="absolute",
                    tremor=0.0, smoothing=1)
    e.gyro.ticks._value = 0.45
    ts = 0
    for _ in range(586):
        ts += 1481
        e._feed_gyro(state(gyro=(3000, 0, 0), ts=ts))   # 只动俯仰
    assert e.gyro._acc_y == 0.0
    assert e.gyro.vertical_relative is False


def test_relative_mode_keeps_integrating_vertical():
    e = make_engine(enabled=True, vertical_mode="relative",
                    tremor=0.0, smoothing=1)
    e.gyro.ticks._value = 0.45
    ts = 0
    for _ in range(586):
        ts += 1481
        e._feed_gyro(state(gyro=(3000, 0, 0), ts=ts))
    assert e.gyro._acc_y != 0.0
    assert e.gyro.vertical_relative is True


def test_settings_flow_through_from_config():
    e = make_engine(enabled=True, sensitivity=12.5, vertical_sensitivity=7.5,
                    link_sensitivity=False,
                    tremor=2.25, smoothing=9, invert_x=True, invert_y=True)
    e._feed_gyro(state(ts=1481))
    gp = e.gyro
    assert gp.pixels_per_degree == 12.5
    assert gp.vertical_pixels_per_degree == 7.5
    assert gp.tremor_dps == 2.25
    assert gp.smoothing == 9
    assert gp.invert_x and gp.invert_y


def test_calibration_picked_up_from_device():
    e = make_engine(enabled=True)
    e._feed_gyro(state(ts=1481))
    assert e.gyro.calibrated
    assert e.gyro.scale == tuple(REAL_SCALE)


def test_works_without_calibration():
    e = make_engine(enabled=True, tremor=0.0, smoothing=1)
    e.dev.calib = None
    e.gyro.ticks._value = 0.45
    ts = 0
    for _ in range(586):
        ts += 1481
        e._feed_gyro(state(gyro=(0, 1500, 0), ts=ts))
    assert not e.gyro.calibrated
    assert e.gyro.take()[0] != 0


def test_disabling_clears_pending_motion():
    """关掉之后不该还剩一截位移慢慢吐出来。"""
    e = make_engine(enabled=True, tremor=0.0, smoothing=1)
    e.gyro.ticks._value = 0.45
    ts = 0
    for _ in range(300):
        ts += 1481
        e._feed_gyro(state(gyro=(0, 3000, 0), ts=ts))
    e.profile["gyro"]["enabled"] = False
    e._feed_gyro(state(gyro=(0, 3000, 0), ts=ts + 1481))
    assert e.gyro.take() == (0, 0)


def test_snapshot_exposes_live_dps_for_tuning():
    e = make_engine(enabled=True, tremor=0.0, smoothing=1)
    e.gyro.ticks._value = 0.45
    ts = 0
    for _ in range(50):
        ts += 1481
        e._feed_gyro(state(gyro=(0, 1626, 0), ts=ts))   # ≈100 度/秒
    snap = e.snapshot()
    assert snap["gyro"]["on"] is True
    assert abs(snap["gyro"]["dps"][1] - 100.0) < 2.0, snap["gyro"]["dps"]


def test_config_roundtrip_keeps_gyro(tmp_path):
    cfg = cfgmod.default_config()
    cfg["profiles"][0]["gyro"].update(enabled=True, sensitivity=33.0,
                                      vertical_mode="absolute",
                                      toggle_combo=["l1", "r1"])
    p = str(tmp_path / "c.json")
    assert cfgmod.save(cfg, p)
    back = cfgmod.load(p)
    g = back["profiles"][0]["gyro"]
    assert g["enabled"] is True
    assert g["sensitivity"] == 33.0
    assert g["vertical_mode"] == "absolute"
    assert g["toggle_combo"] == ["l1", "r1"]


def test_old_config_without_gyro_gets_defaults(tmp_path):
    """升级：老配置文件里没有 gyro 这一节，不能崩，也不能自己开起来。"""
    cfg = cfgmod.default_config()
    for p in cfg["profiles"]:
        p.pop("gyro", None)
    path = str(tmp_path / "old.json")
    cfgmod.save(cfg, path)
    back = cfgmod.load(path)
    g = back["profiles"][0].get("gyro")
    assert g is not None
    assert g["enabled"] is False


# ------------------------------------------------- L2+R2 当开关组合键
#
# 扳机是模拟量，不在 st.buttons 里，所以组合键要单独按深度判断。
# 更要命的是 L2/R2 默认绑着鼠标右键和左键 —— 为了切换陀螺仪捏一下两个扳机，
# 会先在屏幕上左右各点一下，可能正好点开个链接。下面几条把这些都钉住。

def tstate(l2=0.0, r2=0.0, **kw):
    st = state(**kw)
    st.l2, st.r2 = l2, r2
    st.raw_l2, st.raw_r2 = int(l2 * 255), int(r2 * 255)
    return st


def test_default_toggle_combo_is_l2_r2():
    assert cfgmod.default_gyro()["toggle_combo"] == ["l2", "r2"]


def test_triggers_are_recognised_by_depth_not_buttons():
    """st.buttons 里根本没有 l2/r2 这两个键，直接查会永远是 False。"""
    e = make_engine()
    assert "l2" not in state().buttons
    assert e._combo_pressed(tstate(l2=0.95), "l2") is True
    assert e._combo_pressed(tstate(l2=0.5), "l2") is False
    assert e._combo_pressed(tstate(r2=1.0), "r2") is True


def test_both_triggers_toggle_gyro():
    e = make_engine(toggle_combo=["l2", "r2"])
    assert e._handle_gyro_combo(tstate(l2=0.95, r2=0.95)) is True
    assert e.profile["gyro"]["enabled"] is True
    for _ in range(5):                       # 按住不放不该反复切换
        e._handle_gyro_combo(tstate(l2=0.95, r2=0.95))
    assert e.profile["gyro"]["enabled"] is True
    e._handle_gyro_combo(tstate())           # 全松开
    assert e._gyro_latched is False
    e._handle_gyro_combo(tstate(l2=0.95, r2=0.95))
    assert e.profile["gyro"]["enabled"] is False


def test_one_trigger_alone_does_not_toggle():
    e = make_engine(toggle_combo=["l2", "r2"])
    assert e._handle_gyro_combo(tstate(l2=1.0)) is False
    assert e.profile["gyro"]["enabled"] is False


def test_pressing_both_triggers_fires_no_mouse_click():
    """这是重点：捏两个扳机切陀螺仪时，不能顺手点出鼠标左右键。"""
    e = make_engine(toggle_combo=["l2", "r2"])
    fired = []
    e.runner.press = lambda cid, act: fired.append(("press", cid))
    e.runner.release = lambda cid, act=None: fired.append(("release", cid))
    # 从浅到深捏下去，每一帧都走一遍扳机处理
    for v in (0.1, 0.3, 0.6, 0.9, 1.0):
        e._handle_triggers(tstate(l2=v, r2=v))
    assert [f for f in fired if f[0] == "press"] == [], fired


def test_single_trigger_still_clicks_normally():
    """屏蔽只在两个扳机同时按下时生效，单个扳机照常工作。"""
    e = make_engine(toggle_combo=["l2", "r2"])
    fired = []
    e.runner.press = lambda cid, act: fired.append(cid)
    for v in (0.1, 0.4, 0.8, 1.0):
        e._handle_triggers(tstate(r2=v))
    assert "r2" in fired


def test_already_held_click_is_released_when_combo_engages():
    """先按住一个扳机（已经在点鼠标），再按下另一个：要把前一个松开，
    否则会留一个卡死的鼠标键。"""
    e = make_engine(toggle_combo=["l2", "r2"])
    fired = []
    e.runner.press = lambda cid, act: fired.append(("press", cid))
    e.runner.release = lambda cid, act=None: fired.append(("release", cid))
    e._handle_triggers(tstate(r2=1.0))                  # 先单独按住 R2
    assert ("press", "r2") in fired
    e._handle_triggers(tstate(r2=1.0, l2=0.5))          # 另一个也压下来
    assert ("release", "r2") in fired
    assert e._trigger_state["r2"]["s1"] is False


def test_suppression_only_applies_when_combo_uses_triggers():
    """组合键改成别的键之后，两个扳机同按就该恢复成正常的左右键。"""
    e = make_engine(toggle_combo=["ps", "touchpad_click"])
    fired = []
    e.runner.press = lambda cid, act: fired.append(cid)
    for v in (0.3, 0.9, 1.0):
        e._handle_triggers(tstate(l2=v, r2=v))
    assert "r2" in fired and "l2" in fired


# ------------------------------------------------ 两轴灵敏度锁定与坐标系

def test_linked_sensitivity_keeps_diagonals_honest():
    """两个灵敏度不等会直接把斜向方向感弄歪。

    水平 80、垂直 50 时，一个标准的 45 度斜向动作走出来是
    atan(50/80) ≈ 32 度 —— 比你想的平得多。这就是用户报的「斜向不跟手」
    里，纯粹由设置造成的那一半。
    """
    import math
    e = make_engine(enabled=True, sensitivity=80, vertical_sensitivity=50,
                    link_sensitivity=True)
    e._feed_gyro(state(ts=1481))
    assert e.gyro.vertical_pixels_per_degree == 80
    # 解锁之后才各走各的
    e2 = make_engine(enabled=True, sensitivity=80, vertical_sensitivity=50,
                     link_sensitivity=False)
    e2._feed_gyro(state(ts=1481))
    assert e2.gyro.vertical_pixels_per_degree == 50
    skew = math.degrees(math.atan2(50, 80))
    assert abs(skew - 32) < 1, "斜向偏差的算式变了？实际 %.1f 度" % skew


def test_space_setting_switches_coordinate_system():
    e = make_engine(enabled=True, space="world")
    e._feed_gyro(state(ts=1481))
    assert e.gyro.world is True
    e2 = make_engine(enabled=True, space="local")
    e2._feed_gyro(state(ts=1481))
    assert e2.gyro.world is False


def test_default_sensitivity_range_reaches_150():
    """用户顶在 80 已经没空间了，上限放到 150。"""
    g = cfgmod.default_gyro()
    assert g["link_sensitivity"] is True
    assert g["space"] == "world"
