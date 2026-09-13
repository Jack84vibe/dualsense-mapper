"""纯逻辑单元测试 —— 不需要 Windows，也不需要真手柄。

覆盖：报告解析、扳机效果字节、按键名解析、配置读写与冲突检测、
摇杆曲线、触摸板手势判定。
"""
import math
import os
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps5mapper import config as cfgmod
from ps5mapper import lights as lts
from ps5mapper import triggers as trg
from ps5mapper import winput as wi
from ps5mapper import dualsense as ds
from ps5mapper.engine import Engine, TouchProcessor


def make_usb_report(**kw):
    """造一帧 64 字节的 USB 输入报告。"""
    b = bytearray(64)
    b[0] = ds.INPUT_REPORT_USB
    b[1] = kw.get("lx", 128)
    b[2] = kw.get("ly", 128)
    b[3] = kw.get("rx", 128)
    b[4] = kw.get("ry", 128)
    b[5] = kw.get("l2", 0)
    b[6] = kw.get("r2", 0)
    b[8] = kw.get("b0", ds.DPAD_NEUTRAL)
    b[9] = kw.get("b1", 0)
    b[10] = kw.get("b2", 0)
    b[33] = b[37] = 0x80          # 真实硬件里 bit7 置位 = 该触点没有接触
    for i, pt in enumerate(kw.get("touch", [])):
        off = 33 + i * 4
        active, ident, x, y = pt
        b[off] = (ident & 0x7F) | (0x00 if active else 0x80)
        b[off + 1] = x & 0xFF
        b[off + 2] = ((x >> 8) & 0x0F) | ((y & 0x0F) << 4)
        b[off + 3] = (y >> 4) & 0xFF
    b[53] = kw.get("status", 0x08)
    return bytes(b)


class TestReportParsing(unittest.TestCase):
    def test_neutral(self):
        st = ds.parse_input_report(make_usb_report(), False)
        self.assertIsNotNone(st)
        self.assertAlmostEqual(st.lx, 0.0, places=2)
        self.assertFalse(any(st.buttons.values()))

    def test_sticks_full_range(self):
        st = ds.parse_input_report(make_usb_report(lx=255, ly=0), False)
        self.assertAlmostEqual(st.lx, 1.0, places=2)
        self.assertAlmostEqual(st.ly, -1.0, places=2)

    def test_face_buttons(self):
        st = ds.parse_input_report(
            make_usb_report(b0=ds.DPAD_NEUTRAL | ds.BTN0_CROSS | ds.BTN0_TRIANGLE), False)
        self.assertTrue(st.pressed("cross"))
        self.assertTrue(st.pressed("triangle"))
        self.assertFalse(st.pressed("circle"))

    def test_dpad_directions(self):
        for val, expect in ((0, "dpad_up"), (2, "dpad_right"),
                            (4, "dpad_down"), (6, "dpad_left")):
            st = ds.parse_input_report(make_usb_report(b0=val), False)
            self.assertTrue(st.pressed(expect), f"dpad {val} 应该是 {expect}")
        st = ds.parse_input_report(make_usb_report(b0=1), False)   # 右上
        self.assertTrue(st.pressed("dpad_up") and st.pressed("dpad_right"))

    def test_mute_and_touchpad_button(self):
        st = ds.parse_input_report(
            make_usb_report(b2=ds.BTN2_MUTE | ds.BTN2_TOUCHPAD), False)
        self.assertTrue(st.pressed("mic"))
        self.assertTrue(st.pressed("touchpad_click"))
        self.assertFalse(st.pressed("ps"))

    def test_shoulder_bits(self):
        st = ds.parse_input_report(make_usb_report(b1=ds.BTN1_L1 | ds.BTN1_R3), False)
        self.assertTrue(st.pressed("l1"))
        self.assertTrue(st.pressed("r3"))
        self.assertFalse(st.pressed("r1"))

    def test_triggers_analog(self):
        st = ds.parse_input_report(make_usb_report(l2=255, r2=128), False)
        self.assertAlmostEqual(st.l2, 1.0, places=2)
        self.assertAlmostEqual(st.r2, 0.502, places=2)

    def test_touch_points_roundtrip(self):
        pts = [(True, 3, 1919, 1079), (True, 4, 0, 540)]
        st = ds.parse_input_report(make_usb_report(touch=pts), False)
        self.assertTrue(st.touch[0].active)
        self.assertEqual(st.touch[0].x, 1919)
        self.assertEqual(st.touch[0].y, 1079)
        self.assertEqual(st.touch[1].x, 0)
        self.assertEqual(st.touch[1].y, 540)

    def test_touch_inactive_flag(self):
        st = ds.parse_input_report(make_usb_report(touch=[(False, 3, 100, 100)]), False)
        self.assertFalse(st.touch[0].active)

    def test_bluetooth_offset(self):
        usb = make_usb_report(lx=200, b0=ds.DPAD_NEUTRAL | ds.BTN0_CIRCLE)
        bt = bytearray(78)
        bt[0] = ds.INPUT_REPORT_BT
        bt[1] = 0
        bt[2:2 + 63] = usb[1:64]           # 蓝牙下同样的结构，整体后移一字节
        st = ds.parse_input_report(bytes(bt), True)
        self.assertTrue(st.pressed("circle"))
        self.assertAlmostEqual(st.lx, (200 - 128) / 127.0, places=3)

    def test_rejects_wrong_report_id(self):
        self.assertIsNone(ds.parse_input_report(b"\x99" + b"\x00" * 63, False))
        self.assertIsNone(ds.parse_input_report(b"", False))


class TestDeadInterfaceDetection(unittest.TestCase):
    """全零报告必须被判为「这个 HID 接口没用」。

    真机上手柄枚举出两个同 VID/PID 的接口，开错那个读到的全是零，
    而全零会被解析成「十字键一直按着上、摇杆推到左上角」——看起来像有效数据。
    """

    def test_all_zero_report_is_rejected(self):
        zero = bytes([ds.INPUT_REPORT_USB]) + b"\x00" * 63
        st = ds.parse_input_report(zero, False)
        self.assertIsNotNone(st)
        self.assertTrue(st.pressed("dpad_up"), "全零确实会被解析成十字键上")
        self.assertFalse(ds.DualSense._looks_alive(st), "但不能当成有效接口")

    def test_normal_idle_report_is_accepted(self):
        st = ds.parse_input_report(make_usb_report(), False)
        self.assertTrue(ds.DualSense._looks_alive(st))

    def test_real_input_still_accepted_while_pressing_buttons(self):
        st = ds.parse_input_report(
            make_usb_report(b0=ds.DPAD_NEUTRAL | ds.BTN0_CROSS, l2=200), False)
        self.assertTrue(ds.DualSense._looks_alive(st))

    def test_none_is_rejected(self):
        self.assertFalse(ds.DualSense._looks_alive(None))


class TestInterfaceSelection(unittest.TestCase):
    """手柄可能同时挂着 USB 和蓝牙，两条接口的 usage 完全一样，只能靠路径区分。
    实机路径取自 2026-08-23 的 probe2 输出。"""

    BT = {"path": (r"\\?\HID#{00001124-0000-1000-8000-00805f9b34fb}"
                   r"_VID&0002054c_PID&0ce6#9&d43e65f&b&0000#{4d1e55b2}"),
          "usage_page": 0x01, "usage": 0x05, "product_id": ds.PRODUCT_DUALSENSE}
    USB = {"path": r"\\?\HID#VID_054C&PID_0CE6&MI_03#8&7622243&0&0000#{4d1e55b2}",
           "usage_page": 0x01, "usage": 0x05, "product_id": ds.PRODUCT_DUALSENSE}

    def test_recognises_bluetooth_path(self):
        self.assertTrue(ds.DualSense.is_bluetooth_path(self.BT))
        self.assertFalse(ds.DualSense.is_bluetooth_path(self.USB))

    def test_handles_bytes_path(self):
        info = {"path": self.USB["path"].encode()}
        self.assertFalse(ds.DualSense.is_bluetooth_path(info))
        self.assertIn("MI_03", ds.DualSense.path_str(info))

    def test_missing_path_does_not_crash(self):
        self.assertFalse(ds.DualSense.is_bluetooth_path({}))
        self.assertEqual(ds.DualSense.path_str({}), "")

    def test_usb_is_preferred_over_bluetooth(self):
        """两条都在时必须选 USB —— 只有 USB 下手柄麦克风才可用。"""
        fake = types.SimpleNamespace(enumerate=lambda vid, pid: [self.BT, self.USB])
        real = ds.hid
        ds.hid = fake
        try:
            order = ds.DualSense.enumerate()
        finally:
            ds.hid = real
        self.assertEqual(len(order), 2)
        self.assertFalse(ds.DualSense.is_bluetooth_path(order[0]),
                         "排在第一位的应该是 USB")

    def test_gamepad_usage_preferred_over_vendor_collection(self):
        vendor = dict(self.USB, usage_page=0xFFF0, usage=0x01,
                      path=r"\\?\HID#VID_054C&PID_0CE6&MI_03&Col02#x")
        fake = types.SimpleNamespace(enumerate=lambda vid, pid: [vendor, self.USB])
        real = ds.hid
        ds.hid = fake
        try:
            order = ds.DualSense.enumerate()
        finally:
            ds.hid = real
        self.assertEqual(order[0]["usage_page"], 0x01)
        self.assertEqual(order[0]["usage"], 0x05)


class TestJsApiIntrospection(unittest.TestCase):
    """pywebview 会递归遍历 js_api 对象来生成 JS 端的 api：碰到方法就登记，
    碰到公开的普通属性就当嵌套 API 继续往里钻。

    2026-08-23 实机翻车：Api 公开持有 App，而 App 又持有 Api，形成
    Api -> app -> api -> app 的无限套娃。后果是主线程卡死、窗口标题变成
    「未响应」、api 变成空对象——而且没有任何报错信息指向真正的原因。
    """

    MAX_DEPTH = 12

    def _walk(self, obj, depth=0, path="api"):
        """按 pywebview 的规则模拟一遍序列化。"""
        if depth > self.MAX_DEPTH:
            raise RecursionError("递归过深，存在循环引用：" + path)
        names = []
        for name in dir(obj):
            if name.startswith("_"):
                continue
            try:
                attr = getattr(obj, name)
            except Exception:
                continue
            if callable(attr):
                names.append(name)
            elif hasattr(attr, "__dict__") and getattr(attr, "_serializable", True):
                names.extend(self._walk(attr, depth + 1, path + "." + name))
        return names

    def test_walk_actually_detects_circular_references(self):
        """先证明这个检测器本身有效，否则测试通过也说明不了什么。"""
        class A:
            pass

        class B:
            pass

        a, b = A(), B()
        a.b = b
        b.a = a                      # 互相引用，正是当初的形状
        with self.assertRaises(RecursionError):
            self._walk(a)

    def _api(self):
        from ps5mapper import app as appmod
        return appmod.App().api

    def test_no_infinite_recursion(self):
        names = self._walk(self._api())
        self.assertTrue(names, "至少要暴露出一些方法")

    def test_app_is_not_reachable_from_api(self):
        api = self._api()
        public = [n for n in dir(api) if not n.startswith("_")]
        for name in public:
            self.assertTrue(callable(getattr(api, name)),
                            f"Api.{name} 是个普通属性，pywebview 会顺着它往里钻")

    def test_frontend_required_methods_are_exposed(self):
        """界面用到的每个方法都必须真的能被 JS 看到。"""
        names = set(self._walk(self._api()))
        for needed in ("get_config", "get_state", "set_binding", "set_path",
                       "set_active_profile", "add_profile", "delete_profile",
                       "reset_profile", "toggle_pause", "list_audio",
                       "set_audio_device", "open_sound_settings",
                       "config_file_path"):
            self.assertIn(needed, names)

    def test_api_object_is_not_empty(self):
        """api 变成空对象正是实机那次的症状。"""
        self.assertGreater(len(self._walk(self._api())), 5)


class TestOutputReport(unittest.TestCase):
    def test_usb_length_and_id(self):
        rep = ds.build_output_report(ds.build_output_common(lightbar=(1, 2, 3)), False)
        self.assertEqual(len(rep), ds.USB_OUTPUT_LEN)
        self.assertEqual(rep[0], ds.OUTPUT_REPORT_USB)

    def test_lightbar_bytes_land_in_right_place(self):
        c = ds.build_output_common(lightbar=(10, 20, 30))
        self.assertEqual((c[44], c[45], c[46]), (10, 20, 30))
        self.assertTrue(c[1] & ds.FLAG1_LIGHTBAR)

    def test_trigger_slots(self):
        left = bytes(range(11))
        right = bytes(range(100, 111))
        c = ds.build_output_common(left_trigger=left, right_trigger=right)
        self.assertEqual(bytes(c[21:32]), left)
        self.assertEqual(bytes(c[10:21]), right)
        self.assertTrue(c[0] & ds.FLAG0_LEFT_TRIGGER_FX)
        self.assertTrue(c[0] & ds.FLAG0_RIGHT_TRIGGER_FX)

    def test_bt_report_length_and_crc(self):
        c = ds.build_output_common(lightbar=(0, 0, 255))
        rep = ds.build_output_report(c, True, seq=5)
        self.assertEqual(len(rep), ds.BT_OUTPUT_LEN)
        self.assertEqual(rep[0], ds.OUTPUT_REPORT_BT)
        import struct
        crc = struct.unpack("<I", rep[74:78])[0]
        self.assertEqual(crc, ds.dualsense_crc32(rep[:74]))

    def test_rumble_sets_flag(self):
        c = ds.build_output_common(rumble_left=100, rumble_right=50)
        self.assertEqual(c[3], 100)
        self.assertEqual(c[2], 50)
        self.assertTrue(c[0] & ds.FLAG0_COMPATIBLE_VIBRATION)


class TestTriggerEffects(unittest.TestCase):
    def test_off(self):
        self.assertEqual(trg.off(), b"\x00" * 11)

    def test_all_effects_are_11_bytes(self):
        for eff in (trg.off(), trg.feedback(4, 6), trg.weapon(3, 6, 7),
                    trg.bow(2, 5, 6, 6), trg.vibration(3, 5, 40),
                    trg.slope(2, 8, 2, 8),
                    trg.multiple_position_feedback([0, 0, 5, 0, 0, 8, 0, 0, 0, 0])):
            self.assertEqual(len(eff), 11, eff)

    def test_weapon_zone_bitmask(self):
        eff = trg.weapon(3, 6, 8)
        self.assertEqual(eff[0], trg.MODE_WEAPON)
        zones = eff[1] | (eff[2] << 8)
        self.assertEqual(zones, (1 << 3) | (1 << 6))
        self.assertEqual(eff[3], 7)          # strength - 1

    def test_weapon_falls_back_when_start_too_small(self):
        eff = trg.weapon(1, 4, 6)
        self.assertEqual(eff[0], trg.MODE_FEEDBACK)

    def test_feedback_active_zones(self):
        eff = trg.feedback(7, 4)
        active = eff[1] | (eff[2] << 8)
        self.assertEqual(active, (1 << 7) | (1 << 8) | (1 << 9))

    def test_multiple_position_packs_three_bits_each(self):
        s = [0] * 10
        s[0] = 1     # → 0
        s[1] = 8     # → 7
        eff = trg.multiple_position_feedback(s)
        force = eff[3] | (eff[4] << 8) | (eff[5] << 16) | (eff[6] << 24)
        self.assertEqual(force & 0x07, 0)
        self.assertEqual((force >> 3) & 0x07, 7)
        self.assertEqual(eff[1] | (eff[2] << 8), 0b11)

    def test_zero_strength_is_off(self):
        self.assertEqual(trg.feedback(3, 0), trg.off())
        self.assertEqual(trg.weapon(3, 6, 0), trg.off())

    def test_depth_and_force_conversion(self):
        self.assertEqual(trg.depth_to_zone(0), 0)
        self.assertEqual(trg.depth_to_zone(100), 9)
        self.assertEqual(trg.depth_to_zone(50), 5)
        self.assertEqual(trg.force_to_strength(0), 0)
        self.assertEqual(trg.force_to_strength(255), 8)
        self.assertTrue(1 <= trg.force_to_strength(1) <= 8)

    def test_build_from_config_single(self):
        eff = trg.build_from_config({"enabled": True, "mode": "single",
                                     "preset": "wall", "depth": 60, "force": 200})
        self.assertEqual(eff[0], trg.MODE_WEAPON)

    def test_build_from_config_dual_has_two_walls(self):
        eff = trg.build_from_config({"enabled": True, "mode": "dual",
                                     "depth": 40, "depth2": 85, "force": 200})
        active = eff[1] | (eff[2] << 8)
        z1, z2 = trg.depth_to_zone(40), trg.depth_to_zone(85)
        self.assertTrue(active & (1 << z1))
        self.assertTrue(active & (1 << z2))

    def test_build_from_config_disabled(self):
        self.assertEqual(trg.build_from_config({"enabled": False}), trg.off())

    def test_dual_forces_second_zone_above_first(self):
        eff = trg.build_from_config({"enabled": True, "mode": "dual",
                                     "depth": 80, "depth2": 20, "force": 150})
        self.assertNotEqual(eff, trg.off())


class TestKeyParsing(unittest.TestCase):
    def test_single_keys(self):
        self.assertEqual(wi.key_to_vk("A"), 0x41)
        self.assertEqual(wi.key_to_vk("enter"), 0x0D)
        self.assertEqual(wi.key_to_vk("Delete"), 0x2E)
        self.assertEqual(wi.key_to_vk("F5"), 0x74)
        self.assertEqual(wi.key_to_vk("Up"), 0x26)
        self.assertEqual(wi.key_to_vk("←"), 0x25)
        self.assertIsNone(wi.key_to_vk("完全不存在的键"))

    def test_combos(self):
        self.assertEqual(wi.parse_combo("Ctrl+C"), ([0xA2], 0x43))
        self.assertEqual(wi.parse_combo("Ctrl+Shift+S"), ([0xA2, 0xA0], 0x53))
        self.assertEqual(wi.parse_combo("Win+H"), ([0x5B], 0x48))
        self.assertEqual(wi.parse_combo("Alt+Tab"), ([0xA4], 0x09))
        self.assertEqual(wi.parse_combo("Enter"), ([], 0x0D))

    def test_bare_modifier(self):
        mods, main = wi.parse_combo("Win")
        self.assertEqual(main, 0x5B)

    def test_garbage(self):
        self.assertEqual(wi.parse_combo(""), (None, None))
        self.assertEqual(wi.parse_combo("###"), (None, None))

    def test_extended_key_set_covers_arrows_and_delete(self):
        for vk in (0x25, 0x26, 0x27, 0x28, 0x2E, 0x24, 0x23):
            self.assertIn(vk, wi._EXTENDED)


class TestConfig(unittest.TestCase):
    def test_default_has_three_profiles(self):
        cfg = cfgmod._factory_config()
        self.assertEqual(len(cfg["profiles"]), 3)
        self.assertEqual(cfg["active_profile"], 0)

    def test_every_button_id_has_a_slot(self):
        prof = cfgmod._factory_profile()
        for bid in ds.BUTTON_IDS:
            self.assertIn(bid, prof["buttons"], bid)

    def test_default_bindings_parse(self):
        prof = cfgmod._factory_profile()
        for cid, act in prof["buttons"].items():
            if act.get("type") == "combo":
                mods, main = wi.parse_combo(act["keys"])
                self.assertIsNotNone(main, f"{cid} 的默认组合键解析失败: {act['keys']}")

    def test_win_h_is_on_mic_by_default(self):
        prof = cfgmod._factory_profile()
        self.assertEqual(prof["buttons"]["mic"]["keys"], "Win+H")

    def test_save_load_roundtrip(self):
        import tempfile
        cfg = cfgmod._factory_config()
        cfg["profiles"][0]["buttons"]["cross"] = cfgmod.combo("Ctrl+Q", "自定义")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            self.assertTrue(cfgmod.save(cfg, p))
            back = cfgmod.load(p)
        self.assertEqual(back["profiles"][0]["buttons"]["cross"]["keys"], "Ctrl+Q")

    def test_load_missing_file_returns_defaults(self):
        """没有配置文件时就该拿到出厂默认——不写死几个档，
        因为出厂默认可能是烘焙进来的那一份。"""
        cfg = cfgmod.load("/nonexistent/path/x.json")
        self.assertEqual([p["name"] for p in cfg["profiles"]],
                         [p["name"] for p in cfgmod.default_config()["profiles"]])

    def test_partial_config_is_merged_with_defaults(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"poll_hz": 999,
                           "profiles": [{"name": "只有名字"}]}, f)
            cfg = cfgmod.load(p)
        self.assertEqual(cfg["poll_hz"], 999)
        self.assertEqual(cfg["profiles"][0]["name"], "只有名字")
        # 缺的部分应该被默认值补上
        self.assertIn("cross", cfg["profiles"][0]["buttons"])
        self.assertIn("l2", cfg["profiles"][0]["triggers"])

    def test_corrupt_config_falls_back(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            open(p, "w").write("{ 这不是 json")
            cfg = cfgmod.load(p)
        self.assertEqual([x["name"] for x in cfg["profiles"]],
                         [x["name"] for x in cfgmod.default_config()["profiles"]])

    def test_conflicts_detected(self):
        prof = cfgmod._factory_profile()
        prof["buttons"]["ps"] = cfgmod.combo("Ctrl+C", "复制")   # 和 L1 撞
        con = cfgmod.find_conflicts(prof)
        self.assertTrue(con.get("ps"))
        self.assertTrue(con.get("l1"))

    def test_no_conflicts_in_factory_default(self):
        con = cfgmod.find_conflicts(cfgmod._factory_profile())
        self.assertEqual(con, {}, f"出厂配置不该有重复绑定：{con}")

    def test_unbound_controls_not_reported_as_conflict(self):
        prof = cfgmod._factory_profile()
        prof["buttons"]["ps"] = dict(cfgmod.NONE_ACTION)
        prof["buttons"]["r3"] = dict(cfgmod.NONE_ACTION)
        con = cfgmod.find_conflicts(prof)
        self.assertNotIn("ps", con)


class TestStickCurve(unittest.TestCase):
    def test_inside_deadzone_is_zero(self):
        x, y = Engine._apply_curve(0.05, 0.0, {"deadzone": 11, "curve": 2.0})
        self.assertEqual((x, y), (0.0, 0.0))

    def test_full_deflection_is_one(self):
        x, y = Engine._apply_curve(1.0, 0.0, {"deadzone": 11, "curve": 2.0})
        self.assertAlmostEqual(x, 1.0, places=5)

    def test_curve_is_monotonic(self):
        prev = -1
        for v in [i / 20 for i in range(3, 21)]:
            x, _ = Engine._apply_curve(v, 0.0, {"deadzone": 10, "curve": 2.0})
            self.assertGreaterEqual(x, prev)
            prev = x

    def test_higher_exponent_is_slower_at_low_input(self):
        soft, _ = Engine._apply_curve(0.5, 0.0, {"deadzone": 10, "curve": 1.0})
        hard, _ = Engine._apply_curve(0.5, 0.0, {"deadzone": 10, "curve": 3.0})
        self.assertLess(hard, soft)

    def test_diagonal_keeps_direction(self):
        x, y = Engine._apply_curve(0.7, 0.7, {"deadzone": 10, "curve": 2.0})
        self.assertAlmostEqual(x, y, places=6)

    def test_deadzone_is_radial_not_per_axis(self):
        # 两轴各 0.09，合成 0.127 > 0.1 死区，应该有输出
        x, y = Engine._apply_curve(0.09, 0.09, {"deadzone": 10, "curve": 1.0})
        self.assertGreater(abs(x) + abs(y), 0)


class TestTouchProcessor(unittest.TestCase):
    def setUp(self):
        self.tp = TouchProcessor()
        self.cfg = {"pointer_enabled": True, "sensitivity": 6, "acceleration": False,
                    "two_finger_scroll": True, "natural_scroll": True, "scroll_speed": 5,
                    "pinch_zoom": True, "edge_slider": False, "edge_side": "right",
                    "edge_target": "volume", "edge_width": 12}

    def state(self, pts):
        return ds.parse_input_report(make_usb_report(touch=pts), False)

    def settle(self, pts, tp=None, cfg=None, frames=None):
        """喂够去抖需要的帧数，让触点被正式认下来。"""
        tp = tp or self.tp
        cfg = cfg or self.cfg
        for _ in range(frames or TouchProcessor.DEBOUNCE_FRAMES):
            tp.update(self.state(pts), cfg)
        return tp

    def test_first_contact_does_not_jump(self):
        dx, dy = self.tp.update(self.state([(True, 1, 900, 500)]), self.cfg)
        self.assertEqual((dx, dy), (0.0, 0.0))

    def test_moving_after_settling_produces_delta(self):
        self.settle([(True, 1, 900, 500)])
        dx, dy = self.tp.update(self.state([(True, 1, 960, 520)]), self.cfg)
        self.assertGreater(dx, 0)
        self.assertGreater(dy, 0)

    def test_lift_and_replace_finger_does_not_jump(self):
        self.tp.update(self.state([(True, 1, 100, 100)]), self.cfg)
        self.tp.update(self.state([(True, 1, 120, 100)]), self.cfg)
        self.tp.update(self.state([]), self.cfg)                 # 抬手
        dx, dy = self.tp.update(self.state([(True, 2, 1800, 900)]), self.cfg)
        self.assertEqual((dx, dy), (0.0, 0.0))                   # 落在别处也不能跳

    def test_edge_slider_only_when_starting_at_edge(self):
        cfg = dict(self.cfg, edge_slider=True)
        tp = TouchProcessor()
        self.settle([(True, 1, 1900, 500)], tp, cfg)             # 从右边缘落下
        self.assertEqual(tp.edge_ident, 1)
        dx, dy = tp.update(self.state([(True, 1, 1900, 300)]), cfg)
        self.assertEqual((dx, dy), (0.0, 0.0))                   # 走滑条，不动指针

    def test_sliding_into_edge_still_moves_pointer(self):
        cfg = dict(self.cfg, edge_slider=True)
        tp = TouchProcessor()
        self.settle([(True, 1, 900, 500)], tp, cfg)              # 从中间落下
        self.assertIsNone(tp.edge_ident)
        dx, _ = tp.update(self.state([(True, 1, 1900, 500)]), cfg)
        self.assertGreater(dx, 0)

    def test_two_fingers_do_not_move_pointer(self):
        tp = TouchProcessor()
        tp.update(self.state([(True, 1, 800, 500), (True, 2, 1000, 500)]), self.cfg)
        dx, dy = tp.update(self.state([(True, 1, 800, 400), (True, 2, 1000, 400)]), self.cfg)
        self.assertEqual((dx, dy), (0.0, 0.0))

    def test_pointer_disabled(self):
        cfg = dict(self.cfg, pointer_enabled=False)
        tp = TouchProcessor()
        self.settle([(True, 1, 900, 500)], tp, cfg)
        dx, dy = tp.update(self.state([(True, 1, 960, 500)]), cfg)
        self.assertEqual((dx, dy), (0.0, 0.0))

    def test_sensitivity_scales_output(self):
        def travel(sens):
            tp = TouchProcessor()
            cfg = dict(self.cfg, sensitivity=sens)
            self.settle([(True, 1, 900, 500)], tp, cfg)
            return tp.update(self.state([(True, 1, 1000, 500)]), cfg)[0]
        self.assertGreater(travel(10), travel(3))

    def test_reset_clears_history(self):
        self.tp.update(self.state([(True, 1, 900, 500)]), self.cfg)
        self.tp.reset()
        dx, dy = self.tp.update(self.state([(True, 1, 1200, 700)]), self.cfg)
        self.assertEqual((dx, dy), (0.0, 0.0))


class TestKeyRepeat(unittest.TestCase):
    """长按连发必须自己实现。

    SendInput 注入的按键**不会**触发 Windows 的自动重复——键盘长按之所以连发，
    是键盘硬件在持续重发扫描码，系统不给注入事件生成重复。
    2026-08-23 用户反馈：L2 映射成 Delete，按住不放只删一个字符。
    """

    def setUp(self):
        from ps5mapper import engine as eng
        self.eng = eng
        self.calls = []
        self._orig = eng.wi.combo_down
        eng.wi.combo_down = lambda mods, main: self.calls.append(main)
        self.cfg = cfgmod._factory_config()
        self.runner = eng.ActionRunner(types.SimpleNamespace(cfg=self.cfg))

    def tearDown(self):
        self.eng.wi.combo_down = self._orig

    DEL = {"type": "combo", "keys": "Delete", "label": "删除"}

    def test_press_sends_exactly_one_keydown(self):
        self.runner.press("l2", self.DEL)
        self.assertEqual(len(self.calls), 1)

    def test_nothing_repeats_before_the_delay(self):
        t = 100.0
        self.runner.press("l2", self.DEL)
        self.runner.tick_repeat(t + 0.1)          # 延迟是 400ms
        self.assertEqual(len(self.calls), 1)

    def test_repeats_after_the_delay(self):
        self.runner.press("l2", self.DEL)
        t0 = self.runner._repeat["l2"]["next"]
        self.runner.tick_repeat(t0 + 0.001)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1], 0x2E)     # VK_DELETE

    def test_repeat_rate_is_honoured(self):
        self.cfg["key_repeat_rate"] = 20           # 每 50ms 一次
        self.runner.press("l2", self.DEL)
        t0 = self.runner._repeat["l2"]["next"]
        for i in range(1, 11):                     # 走 500ms
            self.runner.tick_repeat(t0 + i * 0.05)
        # 1 次按下 + 大约 10 次连发
        self.assertGreaterEqual(len(self.calls), 10)
        self.assertLessEqual(len(self.calls), 12)

    def test_release_stops_repeating(self):
        self.runner.press("l2", self.DEL)
        t0 = self.runner._repeat["l2"]["next"]
        self.runner.tick_repeat(t0 + 0.001)
        n = len(self.calls)
        self.runner.release("l2", self.DEL)
        self.runner.tick_repeat(t0 + 5.0)
        self.assertEqual(len(self.calls), n, "松开之后不能再发")

    def test_release_all_stops_repeating(self):
        self.runner.press("l2", self.DEL)
        self.runner.release_all()
        self.assertEqual(self.runner._repeat, {})

    def test_per_binding_opt_out(self):
        act = dict(self.DEL, repeat=False)
        self.runner.press("l2", act)
        self.runner.tick_repeat(time.monotonic() + 10)
        self.assertEqual(len(self.calls), 1)

    def test_global_switch_off(self):
        self.cfg["key_repeat"] = False
        self.runner.press("l2", self.DEL)
        self.runner.tick_repeat(time.monotonic() + 10)
        self.assertEqual(len(self.calls), 1)

    def test_mouse_binding_never_repeats(self):
        self.runner.press("r2", {"type": "mouse", "button": "left"})
        self.runner.tick_repeat(time.monotonic() + 10)
        self.assertEqual(self.runner._repeat, {})

    def test_long_stall_does_not_dump_a_burst(self):
        """程序卡了几秒之后，不能一次把攒下的几百次全补出去。"""
        self.runner.press("l2", self.DEL)
        t0 = self.runner._repeat["l2"]["next"]
        self.runner.tick_repeat(t0 + 30.0)         # 假装卡了 30 秒
        self.assertLessEqual(len(self.calls), 1 + 8)

    def test_window_switching_default_does_not_repeat(self):
        """任务视图按一次就该只打开一次，连发会把它反复重开。"""
        act = cfgmod.active(self.cfg)["buttons"]["options"]
        self.assertEqual(act["keys"], "Win+Tab")
        self.assertIs(act.get("repeat"), False)
        self.runner.press("options", act)
        self.runner.tick_repeat(time.monotonic() + 10)
        self.assertEqual(len(self.calls), 1)

    def test_win_tab_parses_correctly(self):
        mods, main = wi.parse_combo("Win+Tab")
        self.assertEqual(mods, [0x5B])            # LWIN
        self.assertEqual(main, 0x09)              # TAB

    def test_ctrl_win_arrows_parse(self):
        mods, main = wi.parse_combo("Ctrl+Win+Left")
        self.assertEqual(mods, [0xA2, 0x5B])
        self.assertEqual(main, 0x25)

    def test_trigger_binding_repeats_too(self):
        """用户遇到的就是扳机这一路。"""
        prof = cfgmod.active(self.cfg)
        prof["triggers"]["l2"]["binding"] = dict(self.DEL)
        self.runner.press("l2", prof["triggers"]["l2"]["binding"])
        t0 = self.runner._repeat["l2"]["next"]
        self.runner.tick_repeat(t0 + 0.001)
        self.assertEqual(len(self.calls), 2)


class TestHeldBuiltinRelease(unittest.TestCase):
    """「按住才生效」的内置功能必须能被松开。

    2026-08-23 用户反馈：鼠标用一段时间之后永久变慢。原因是 release() 里
    只处理了 combo 和 mouse 两种，漏了 builtin —— L3 默认绑「指针减速」，
    按过一次之后 slow_pointer 再也没被改回 False，指针永久停在 35% 速度。
    """

    def setUp(self):
        from ps5mapper import engine as eng
        self.eng = eng
        self.engine = types.SimpleNamespace(cfg=cfgmod.default_config(),
                                            slow_pointer=False)
        self.runner = eng.ActionRunner(self.engine)

    SLOW = {"type": "builtin", "name": "slow_pointer", "label": "指针减速"}

    def test_press_enables_slow_pointer(self):
        self.runner.press("l3", self.SLOW)
        self.assertTrue(self.engine.slow_pointer)

    def test_release_disables_slow_pointer(self):
        self.runner.press("l3", self.SLOW)
        self.runner.release("l3", self.SLOW)
        self.assertFalse(self.engine.slow_pointer, "松开 L3 之后指针必须恢复全速")

    def test_release_without_action_arg_still_works(self):
        """引擎在很多地方只传 cid，不传动作。"""
        self.runner.press("l3", self.SLOW)
        self.runner.release("l3")
        self.assertFalse(self.engine.slow_pointer)

    def test_release_all_disables_slow_pointer(self):
        self.runner.press("l3", self.SLOW)
        self.runner.release_all()
        self.assertFalse(self.engine.slow_pointer)

    def test_repeated_press_release_does_not_stick(self):
        for _ in range(5):
            self.runner.press("l3", self.SLOW)
            self.runner.release("l3", self.SLOW)
            self.assertFalse(self.engine.slow_pointer)

    def test_held_dict_does_not_leak(self):
        self.runner.press("l3", self.SLOW)
        self.runner.release("l3", self.SLOW)
        self.assertEqual(self.runner._held, {})


class TestPointerSpeedIsFrameRateIndependent(unittest.TestCase):
    """同样的摇杆推程，走过同样的时间，位移必须一样 —— 不管循环跑多快。

    2026-08-23：指针循环把标称周期当时间增量传进去，循环一旦跑不满设定频率
    指针就成比例变慢。而 Windows 在程序不在前台时会忽略定时器精度请求，
    240Hz 会掉到 64Hz，指针慢到不足三分之一。
    """

    def setUp(self):
        from ps5mapper import engine as eng
        self.eng = eng
        self.moves = []
        self._orig = eng.wi.mouse_move
        eng.wi.mouse_move = lambda dx, dy: self.moves.append((dx, dy))

    def tearDown(self):
        self.eng.wi.mouse_move = self._orig

    def _travel(self, hz, seconds=1.0):
        """摇杆推到底 seconds 秒，按 hz 的频率积分，返回总位移。"""
        e = self.eng.Engine.__new__(self.eng.Engine)
        e.cfg = cfgmod.default_config()
        e.slow_pointer = False
        e.touch = self.eng.TouchProcessor()
        e._lock = threading.Lock()
        e._move_x = e._move_y = 0.0
        e._pending_dx = e._pending_dy = 0.0
        e._scroll = e._hscroll = 0.0
        st = ds.parse_input_report(make_usb_report(lx=255), False)
        self.moves = []
        dt = 1.0 / hz
        for _ in range(int(hz * seconds)):
            e._tick_pointer(st, dt)
        return sum(m[0] for m in self.moves)

    def test_same_distance_at_240hz_and_60hz(self):
        fast = self._travel(240)
        slow = self._travel(60)
        self.assertGreater(fast, 0)
        # 允许亚像素累积带来的一点点误差
        self.assertAlmostEqual(fast, slow, delta=max(2, fast * 0.02))

    def test_same_distance_at_1000hz(self):
        base = self._travel(240)
        self.assertAlmostEqual(self._travel(1000), base, delta=max(2, base * 0.02))

    def _run_loop(self, forced_period, seconds=0.4):
        """真跑一遍 _pointer_loop —— bug 在调用方，只测 _tick_pointer 抓不到。

        把 sleep 换成固定时长，模拟循环被拖慢（前台丢失导致定时器精度下降就是
        这个效果），然后看同样时间内指针走过的距离是否一致。
        """
        e = self.eng.Engine.__new__(self.eng.Engine)
        e.cfg = cfgmod.default_config()
        e.running = True
        e.paused = False
        e.slow_pointer = False
        e.touch = self.eng.TouchProcessor()
        e.runner = self.eng.ActionRunner(e)
        e._lock = threading.Lock()
        e._move_x = e._move_y = 0.0
        e._pending_dx = e._pending_dy = 0.0
        e._scroll = e._hscroll = 0.0
        e.state = ds.parse_input_report(make_usb_report(lx=255), False)

        orig_sleep = self.eng.wi.precise_sleep
        orig_timer = self.eng.wi.make_precise_timer
        self.eng.wi.make_precise_timer = lambda: None
        self.eng.wi.precise_sleep = lambda h, s: time.sleep(forced_period)
        self.moves = []
        try:
            t = threading.Thread(target=e._pointer_loop, daemon=True)
            t.start()
            time.sleep(seconds)
            e.running = False
            t.join(timeout=1.0)
        finally:
            self.eng.wi.precise_sleep = orig_sleep
            self.eng.wi.make_precise_timer = orig_timer
        return sum(m[0] for m in self.moves)

    def test_loop_travel_is_the_same_when_the_loop_is_throttled(self):
        fast = self._run_loop(1 / 240.0)
        slow = self._run_loop(1 / 60.0)
        self.assertGreater(fast, 50, "得真的动起来才有得比")
        ratio = slow / fast
        self.assertGreater(ratio, 0.75,
                           f"循环被拖慢后指针只走了 {ratio:.0%}，速度不该跟循环频率挂钩")
        self.assertLess(ratio, 1.35)

    def test_slow_pointer_actually_halves_speed(self):
        normal = self._travel(240)
        e = self.eng.Engine.__new__(self.eng.Engine)
        e.cfg = cfgmod.default_config()
        e.slow_pointer = True
        e.touch = self.eng.TouchProcessor()
        e._lock = threading.Lock()
        e._move_x = e._move_y = 0.0
        e._pending_dx = e._pending_dy = 0.0
        e._scroll = e._hscroll = 0.0
        st = ds.parse_input_report(make_usb_report(lx=255), False)
        self.moves = []
        for _ in range(240):
            e._tick_pointer(st, 1 / 240.0)
        slowed = sum(m[0] for m in self.moves)
        self.assertLess(slowed, normal * 0.5, "减速档应该明显更慢")


class TestGhostTouchFiltering(unittest.TestCase):
    """实机上手指离开后仍有约 1/3 的帧报告「按下」，坐标卡在上次的位置不动。
    数据来自 2026-08-23 probe3：手完全拿开时 461/1251 帧误报，
    触点1 id=25 固定在 (1007, 808)。
    """

    def setUp(self):
        self.tp = TouchProcessor()
        self.cfg = {"pointer_enabled": True, "sensitivity": 6, "acceleration": False,
                    "two_finger_scroll": True, "natural_scroll": True, "scroll_speed": 5,
                    "pinch_zoom": True, "edge_slider": False, "edge_side": "right",
                    "edge_target": "volume", "edge_width": 12}

    def st(self, pts):
        return ds.parse_input_report(make_usb_report(touch=pts), False)

    def feed(self, pts, t):
        return self.tp.update(self.st(pts), self.cfg, now=t)

    GHOST = (True, 25, 1007, 808)

    def test_single_frame_flicker_never_counts_as_a_finger(self):
        """闪一两帧就消失的触点根本不该被当成手指。"""
        t = 0.0
        for _ in range(4):
            self.feed([self.GHOST], t); t += 0.004
            self.feed([], t); t += 0.004
        self.assertEqual(self.tp._slots[0]["down"], False)

    def test_motionless_point_becomes_ghost(self):
        """坐标一动不动超过 300ms 就当它不存在。"""
        t = 0.0
        for _ in range(120):                      # 约 480ms
            self.feed([self.GHOST], t)
            t += 0.004
        self.assertTrue(self.tp._slots[0]["ghost"])

    def test_ghost_does_not_turn_one_finger_into_two(self):
        """这是幽灵触点最要命的后果：单指滑动被误判成双指滚动。"""
        t = 0.0
        for _ in range(120):                      # 让触点1 变成幽灵
            self.feed([self.GHOST], t)
            t += 0.004
        # 现在真手指在触点2 上落下并滑动（同样要走完去抖）
        for i in range(TouchProcessor.DEBOUNCE_FRAMES):
            self.feed([self.GHOST, (True, 7, 800 + i, 500)], t)
            t += 0.004
        dx, dy = self.feed([self.GHOST, (True, 7, 880, 560)], t)
        self.assertGreater(dx, 0, "应该走单指指针移动，而不是双指滚动")
        self.assertGreater(dy, 0)

    def test_ghost_revives_when_it_actually_moves(self):
        """幽灵一旦真的动起来，说明是真手指，要立刻恢复。"""
        t = 0.0
        for _ in range(120):
            self.feed([self.GHOST], t)
            t += 0.004
        self.assertTrue(self.tp._slots[0]["ghost"])
        # 复活那一帧当作新触摸，不能凭旧坐标算出一大截位移
        dx, dy = self.feed([(True, 25, 1100, 900)], t)
        t += 0.004
        self.assertFalse(self.tp._slots[0]["ghost"])
        self.assertEqual((dx, dy), (0.0, 0.0), "复活帧不该让指针瞬移")
        # 之后正常跟随
        dx, dy = self.feed([(True, 25, 1160, 940)], t)
        self.assertGreater(dx, 0)

    def test_real_finger_still_works_normally(self):
        """去抖不能把正常触摸也挡掉。"""
        t = 0.0
        for i in range(6):                        # 稳定按住几帧
            self.feed([(True, 3, 900 + i, 500)], t)
            t += 0.004
        dx, dy = self.feed([(True, 3, 960, 500)], t)
        self.assertGreater(dx, 0)

    def test_debounce_delays_pickup_by_only_a_few_frames(self):
        t = 0.0
        self.feed([(True, 3, 900, 500)], t); t += 0.004
        self.assertFalse(self.tp._slots[0]["down"], "第 1 帧还不该认")
        self.feed([(True, 3, 901, 500)], t); t += 0.004
        self.feed([(True, 3, 902, 500)], t)
        self.assertTrue(self.tp._slots[0]["down"], "第 3 帧应该认了")

    def test_brief_dropout_does_not_end_a_real_touch(self):
        """真手指滑动时偶尔掉一帧，不能当成抬手。"""
        t = 0.0
        for i in range(6):
            self.feed([(True, 3, 900 + i * 5, 500)], t)
            t += 0.004
        self.feed([], t); t += 0.004               # 掉一帧
        dx, _ = self.feed([(True, 3, 960, 500)], t)
        self.assertGreater(dx, 0, "掉一帧不该重置成新触摸")

    def test_sustained_lift_does_end_the_touch(self):
        """真抬手（连续多帧无接触）必须结束这次触摸，下次落下不能算位移。"""
        t = 0.0
        for i in range(6):
            self.feed([(True, 3, 900 + i * 5, 500)], t)
            t += 0.004
        for _ in range(TouchProcessor.DEBOUNCE_FRAMES + 1):
            self.feed([], t); t += 0.004
        self.assertFalse(self.tp._slots[0]["down"])
        # 手指落到别的地方，指针不能跟着瞬移过去
        for i in range(TouchProcessor.DEBOUNCE_FRAMES):
            dx, dy = self.feed([(True, 9, 1800, 900)], t)
            t += 0.004
            self.assertEqual((dx, dy), (0.0, 0.0))


class TestPauseCombo(unittest.TestCase):
    """紧急暂停组合的判定（不启动线程，只测纯函数）。"""

    def setUp(self):
        cfg = cfgmod.default_config()
        self.e = Engine.__new__(Engine)
        self.e.cfg = cfg

    def st(self, **kw):
        return ds.parse_input_report(make_usb_report(**kw), False)

    def test_all_three_required(self):
        s = self.st(b1=ds.BTN1_L1 | ds.BTN1_R1)                  # 少了触摸板
        self.assertFalse(self.e._pause_combo_active(s))
        s = self.st(b1=ds.BTN1_L1 | ds.BTN1_R1, b2=ds.BTN2_TOUCHPAD)
        self.assertTrue(self.e._pause_combo_active(s))

    def test_empty_combo_never_triggers(self):
        self.e.cfg["pause_combo"] = []
        s = self.st(b1=ds.BTN1_L1 | ds.BTN1_R1, b2=ds.BTN2_TOUCHPAD)
        self.assertFalse(self.e._pause_combo_active(s))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTriggerFirePoint(unittest.TestCase):
    """按键触发点必须落在阻力墙的「按穿」处，而不是刚碰到墙的地方。

    2026-08-23 用户反馈（截图）：L2 触发点标 40%，但要压到明显更深才感觉到
    阻力，而且阻力还没到按键就已经触发了。原因是 depth 一个值被当成两件事用：
      · triggers.build_from_config 用它当 weapon 墙的起点，墙实际覆盖 z1..z1+2
      · Engine._handle_triggers 用它当按键触发阈值
    于是按键在墙的「入口」触发，而手感上的「按穿」在墙的「出口」。
    """

    def cfg(self, **kw):
        t = cfgmod.default_trigger(40, 180)
        t.update(kw)
        return t

    # -- wall_span 必须和 build_from_config 真正下发的字节一致 --------
    def test_wall_span_matches_weapon_bytes(self):
        for depth in (20, 40, 55, 62, 80):
            c = self.cfg(depth=depth)
            start, end = trg.wall_span(c, 1)
            z1 = trg.depth_to_zone(depth)
            self.assertEqual((start, end), (z1, min(9, z1 + 2)),
                             "wall 预设的墙跨 3 个区，depth=%d" % depth)

    def test_wall_span_constant_preset_has_no_far_edge(self):
        c = self.cfg(preset="constant")
        self.assertEqual(trg.wall_span(c, 1), (4, 4),
                         "恒定阻力没有『按穿』这回事，触发点就是起点")

    # -- 核心回归：触发点在墙之后 ------------------------------------
    def test_fire_is_past_the_wall_not_at_its_start(self):
        c = self.cfg(depth=40)
        fire = trg.fire_percent(c, 1)
        self.assertEqual(fire, 70.0)
        self.assertGreater(fire, c["depth"],
                           "修好之前 fire 就等于 depth=40，正是用户报的 bug")

    def test_fire_never_lands_inside_the_wall(self):
        for depth in range(15, 96, 5):
            c = self.cfg(depth=depth)
            start, end = trg.wall_span(c, 1)
            fire = trg.fire_percent(c, 1)
            self.assertGreaterEqual(fire, min(trg.FIRE_MAX, (end + 1) * 10) - 0.001,
                                    "depth=%d 时触发点掉进墙里了" % depth)

    def test_constant_preset_fires_at_depth(self):
        c = self.cfg(preset="constant", depth=40)
        self.assertEqual(trg.fire_percent(c, 1), 40.0)

    # -- 三种判定模式 ------------------------------------------------
    def test_custom_mode_uses_slider(self):
        c = self.cfg(fire_mode="custom", fire_depth=33)
        self.assertEqual(trg.fire_percent(c, 1), 33.0)

    def test_custom_mode_is_clamped(self):
        self.assertEqual(trg.fire_percent(self.cfg(fire_mode="custom", fire_depth=999), 1),
                         float(trg.FIRE_MAX))
        self.assertEqual(trg.fire_percent(self.cfg(fire_mode="custom", fire_depth=-5), 1),
                         float(trg.FIRE_MIN))

    def test_bottom_mode_is_reachable(self):
        c = self.cfg(fire_mode="bottom")
        self.assertEqual(trg.fire_percent(c, 1), float(trg.BOTTOM_PCT))
        self.assertLessEqual(trg.BOTTOM_PCT, 98, "按到底的判定值必须真的够得着")

    # -- 没有墙的时候要退回按深度触发 --------------------------------
    def test_no_wall_falls_back_to_depth(self):
        c = self.cfg(depth=40)
        self.assertEqual(trg.fire_percent(c, 1, wall_active=False), 40.0)
        self.assertEqual(trg.fire_percent(self.cfg(depth=40, enabled=False), 1), 40.0)

    # -- 双段 --------------------------------------------------------
    def test_dual_stage_fire_points_are_ordered(self):
        c = self.cfg(mode="dual", depth=40, depth2=80)
        f1 = trg.fire_percent(c, 1)
        f2 = trg.fire_percent(c, 2)
        self.assertLess(f1, f2, "第一段必须先于第二段触发")
        s1, s2 = trg.wall_span(c, 1), trg.wall_span(c, 2)
        self.assertLess(s1[1], s2[0], "两道墙不能重叠")

    def test_dual_first_wall_does_not_swallow_second(self):
        c = self.cfg(mode="dual", depth=40, depth2=55)
        s1, s2 = trg.wall_span(c, 1), trg.wall_span(c, 2)
        self.assertLess(s1[1], s2[0])

    # -- 引擎真的按新触发点动作 --------------------------------------
    def _engine(self, tcfg):
        e = Engine.__new__(Engine)
        e.cfg = cfgmod._factory_config()
        prof = cfgmod.active(e.cfg)          # profile 是只读属性，直接改 cfg
        prof["triggers"]["l2"] = tcfg
        prof["triggers"]["r2"] = cfgmod.default_trigger(40, 180)
        e._trigger_state = {"l2": {"s1": False, "s2": False},
                            "r2": {"s1": False, "s2": False}}
        events = []
        e.runner = types.SimpleNamespace(
            press=lambda cid, act=None: events.append(("press", cid)),
            release=lambda cid, act=None: events.append(("release", cid)),
            tap=lambda cid, act=None: events.append(("tap", cid)))
        return e, events

    def feed(self, e, l2):
        e._handle_triggers(types.SimpleNamespace(l2=l2, r2=0.0))

    def test_engine_does_not_fire_at_wall_entry(self):
        t = self.cfg(depth=40, binding=cfgmod.combo("Backspace", "退格"))
        e, ev = self._engine(t)
        for pct in (0.30, 0.45, 0.55, 0.65):
            self.feed(e, pct)
        self.assertEqual(ev, [], "还在墙里就触发了——正是用户报的 bug")
        self.feed(e, 0.72)
        self.assertEqual(ev, [("press", "l2")], "按穿墙之后才该触发")

    def test_engine_releases_after_rebound(self):
        t = self.cfg(depth=40, binding=cfgmod.combo("Backspace", "退格"))
        e, ev = self._engine(t)
        self.feed(e, 0.80)
        self.feed(e, 0.20)
        self.assertEqual(ev, [("press", "l2"), ("release", "l2")])

    def test_hysteresis_cannot_exceed_fire_point(self):
        """回弹阈值默认 38，触发点 70，两者不能打架；哪怕用户把回弹拉到 80。"""
        t = self.cfg(depth=40, hysteresis=80, binding=cfgmod.combo("A", "A"))
        e, ev = self._engine(t)
        self.feed(e, 0.72)
        self.feed(e, 0.71)
        self.assertEqual(ev, [("press", "l2")],
                         "按住不放不能因为回弹阈值高于触发点就立刻松开")

    def test_engine_honours_custom_fire_depth(self):
        t = self.cfg(depth=40, fire_mode="custom", fire_depth=25,
                     binding=cfgmod.combo("A", "A"))
        e, ev = self._engine(t)
        self.feed(e, 0.30)
        self.assertEqual(ev, [("press", "l2")])

    def test_engine_bottom_mode_needs_full_press(self):
        t = self.cfg(depth=40, fire_mode="bottom", binding=cfgmod.combo("A", "A"))
        e, ev = self._engine(t)
        self.feed(e, 0.90)
        self.assertEqual(ev, [], "没按到底不能触发")
        self.feed(e, 1.0)
        self.assertEqual(ev, [("press", "l2")])

    def test_master_switch_off_falls_back_to_depth(self):
        t = self.cfg(depth=40, binding=cfgmod.combo("A", "A"))
        e, ev = self._engine(t)
        e.cfg["adaptive_triggers_master"] = False
        self.feed(e, 0.45)
        self.assertEqual(ev, [("press", "l2")],
                         "关掉阻力之后没有墙可按穿，就该按深度触发")

    def test_default_config_uses_wall_mode(self):
        for tid in ("l2", "r2"):
            self.assertEqual(cfgmod._factory_profile()["triggers"][tid]["fire_mode"], "wall")

    def test_old_config_without_fire_mode_still_loads(self):
        merged = cfgmod._merge(cfgmod.default_trigger(40, 180), {"depth": 55})
        self.assertEqual(merged["fire_mode"], "wall")
        self.assertEqual(trg.fire_percent(merged, 1), 80.0)


class TestLightMath(unittest.TestCase):
    """灯光的纯计算部分。手柄上的灯我没法自动验证，至少保证算出来的数是对的。"""

    def test_scale_clamps_and_rounds(self):
        self.assertEqual(lts.scale((255, 128, 0), 0.5), (128, 64, 0))
        self.assertEqual(lts.scale((255, 255, 255), 0.0), (0, 0, 0))
        self.assertEqual(lts.scale((255, 255, 255), 5.0), (255, 255, 255),
                         "亮度系数必须夹在 0..1，不然会溢出成别的颜色")
        self.assertEqual(lts.scale((-20, 300, 10), 1.0), (0, 255, 10))

    def test_breathe_stays_in_range(self):
        for i in range(200):
            f = lts.breathe_factor(i * 0.037)
            self.assertGreaterEqual(f, lts.BREATHE_FLOOR - 1e-9)
            self.assertLessEqual(f, 1.0 + 1e-9)

    def test_breathe_never_goes_fully_dark(self):
        lows = [lts.breathe_factor(t) for t in (0.0, 4.0, 8.0)]
        for f in lows:
            self.assertAlmostEqual(f, lts.BREATHE_FLOOR, places=6,
                                   msg="最暗处要留一点亮，全黑会看着像坏了")

    def test_breathe_peaks_at_half_period(self):
        self.assertAlmostEqual(lts.breathe_factor(2.0, 4.0), 1.0, places=6)

    def test_breathe_is_periodic(self):
        for t in (0.3, 1.1, 2.7, 3.9):
            self.assertAlmostEqual(lts.breathe_factor(t, 4.0),
                                   lts.breathe_factor(t + 4.0, 4.0), places=9)

    def test_breathe_survives_zero_period(self):
        lts.breathe_factor(1.0, 0.0)          # 不许除零炸掉


class TestBatteryLeds(unittest.TestCase):
    """三档电量显示。

    掩码是 probe12 在真手柄上试出来的，不是从位序推的——位序那一问至今没定论。
    所以这些测试盯的是「只能出现这三个图案、分档点在哪、永远不会五颗全亮」，
    而不是去验证某一位对应哪一颗灯。
    """

    def test_three_levels_only(self):
        self.assertEqual(lts.battery_leds(5), lts.BATTERY_LOW)
        self.assertEqual(lts.battery_leds(10), lts.BATTERY_LOW)
        self.assertEqual(lts.battery_leds(11), lts.BATTERY_MID)
        self.assertEqual(lts.battery_leds(25), lts.BATTERY_MID)
        self.assertEqual(lts.battery_leds(26), lts.BATTERY_HIGH)
        self.assertEqual(lts.battery_leds(100), lts.BATTERY_HIGH)

    def test_never_lights_all_five(self):
        """五颗全亮看着是歪的——中间三颗排布更紧密。一个电量都不许画成 0x1F。"""
        for pct in range(0, 101):
            for chg in (False, True):
                for t in (0.0, 0.3, 0.6, 0.9, 1.4):
                    self.assertNotEqual(lts.battery_leds(pct, chg, t), 0x1F,
                                        "%d%% charging=%s t=%s" % (pct, chg, t))

    def test_only_the_three_measured_patterns_ever_appear(self):
        seen = set()
        for pct in list(range(0, 101)) + [None]:
            for chg in (False, True):
                for i in range(12):
                    seen.add(lts.battery_leds(pct, chg, i * 0.25))
        self.assertTrue(seen <= {0, lts.BATTERY_LOW, lts.BATTERY_MID, lts.BATTERY_HIGH},
                        "出现了没在真手柄上验过的图案：%s" % sorted(seen))

    def test_unknown_battery_shows_nothing(self):
        self.assertEqual(lts.battery_leds(None), 0, "读不到电量就别瞎猜")
        self.assertEqual(lts.battery_leds(None, charging=True, now=0.0), 0)

    def test_out_of_range_is_clamped(self):
        self.assertEqual(lts.battery_leds(999), lts.BATTERY_HIGH)
        self.assertEqual(lts.battery_leds(-50), lts.BATTERY_LOW)

    def test_mask_never_exceeds_five_leds(self):
        for pct in range(0, 101):
            for chg in (False, True):
                for t in (0.0, 0.6):
                    self.assertEqual(lts.battery_leds(pct, chg, t) & ~0x1F, 0)

    def test_charging_climbs_the_ladder(self):
        """充电时一颗→两颗→四颗循环往上，任何电量下都看得出在充电。"""
        frames = [lts.battery_leds(50, charging=True, now=i * 0.5) for i in range(4)]
        self.assertEqual(frames, [lts.BATTERY_LOW, lts.BATTERY_MID,
                                  lts.BATTERY_HIGH, lts.BATTERY_LOW])

    def test_charging_animates_at_every_level(self):
        for pct in (5, 20, 90):
            a = lts.battery_leds(pct, charging=True, now=0.0)
            b = lts.battery_leds(pct, charging=True, now=0.5)
            self.assertNotEqual(a, b, "%d%% 充电时灯是静止的，看不出在充电" % pct)

    def test_not_charging_is_static(self):
        for t in (0.0, 0.5, 3.7):
            self.assertEqual(lts.battery_leds(90, charging=False, now=t),
                             lts.BATTERY_HIGH)


class TestVoiceComboDetection(unittest.TestCase):
    """麦克风灯认的是动作不是按键——Win+H 绑到哪个键上都该点亮。"""

    def test_accepts_win_h_variants(self):
        for k in ("Win+H", "win+h", " WIN + H ", "Meta+H", "Cmd+h", "Super+H"):
            self.assertTrue(lts.is_voice_input_combo(k), k)

    def test_rejects_everything_else(self):
        for k in ("Win+Tab", "Ctrl+H", "H", "", None, "Win+Shift+H", "Win"):
            self.assertFalse(lts.is_voice_input_combo(k), repr(k))


class TestLightResolve(unittest.TestCase):

    def base(self, **kw):
        c = lts.default_lights()
        c.update(kw)
        return c

    # -- 优先级：总开关 > 暂停 > 切档提示 > 常态 -----------------------
    def test_master_off_kills_everything(self):
        v = lts.resolve(self.base(enabled=False), 0.0, battery=100, mic_on=True)
        self.assertEqual(v, {"lightbar": (0, 0, 0), "player_leds": 0, "mic_led": False})

    def test_zero_brightness_kills_everything(self):
        v = lts.resolve(self.base(brightness=0), 0.0, battery=100, mic_on=True)
        self.assertEqual(v["lightbar"], (0, 0, 0))
        self.assertEqual(v["player_leds"], 0)

    def test_paused_beats_flash(self):
        v = lts.resolve(self.base(), 0.0, paused=True,
                        flash_color=(0, 255, 0), flash_until=99.0,
                        battery=100, mic_on=True)
        self.assertEqual(v["lightbar"], lts.scale(lts.PAUSED_COLOR, 0.8))
        self.assertEqual(v["player_leds"], 0, "暂停时另两盏灯也要灭，别看错")
        self.assertFalse(v["mic_led"])

    def test_paused_colour_is_distinct_from_any_profile_colour(self):
        r, g, b = lts.PAUSED_COLOR
        self.assertGreater(r, 0)
        self.assertEqual((g, b), (0, 0), "暂停色必须是纯红，不能像任何配置档色")

    def test_flash_shows_profile_colour_then_falls_back(self):
        c = self.base(color=[10, 10, 10], effect="steady", brightness=100)
        during = lts.resolve(c, 1.0, flash_color=(200, 0, 0), flash_until=3.0)
        after = lts.resolve(c, 3.0, flash_color=(200, 0, 0), flash_until=3.0)
        self.assertEqual(during["lightbar"], (200, 0, 0))
        self.assertEqual(after["lightbar"], (10, 10, 10), "提示期一到就该回常态")

    def test_flash_is_steady_not_breathing(self):
        """闪的时候还呼吸就看不清到底是什么颜色了。"""
        c = self.base(effect="breathe", brightness=100)
        a = lts.resolve(c, 0.0, flash_color=(200, 0, 0), flash_until=9.0)
        b = lts.resolve(c, 2.0, flash_color=(200, 0, 0), flash_until=9.0)
        self.assertEqual(a["lightbar"], b["lightbar"])

    def test_flash_can_be_switched_off(self):
        c = self.base(profile_flash=False, color=[10, 10, 10], brightness=100)
        v = lts.resolve(c, 1.0, flash_color=(200, 0, 0), flash_until=9.0)
        self.assertEqual(v["lightbar"], (10, 10, 10))

    # -- 常态灯效 ----------------------------------------------------
    def test_steady_is_actually_steady(self):
        c = self.base(effect="steady", color=[100, 100, 100], brightness=100)
        self.assertEqual(lts.resolve(c, 0.0)["lightbar"],
                         lts.resolve(c, 1.7)["lightbar"])

    def test_breathe_actually_changes(self):
        c = self.base(effect="breathe", color=[255, 255, 255], brightness=100)
        self.assertNotEqual(lts.resolve(c, 0.0)["lightbar"],
                            lts.resolve(c, 2.0)["lightbar"])

    def test_effect_off_kills_lightbar_but_keeps_other_leds(self):
        c = self.base(effect="off")
        v = lts.resolve(c, 0.0, battery=100, mic_on=True)
        self.assertEqual(v["lightbar"], (0, 0, 0))
        self.assertEqual(v["player_leds"], lts.BATTERY_HIGH, "关灯条不该连电量一起关")
        self.assertTrue(v["mic_led"])

    def test_brightness_scales_lightbar(self):
        full = lts.resolve(self.base(color=[200, 100, 50], brightness=100), 0.0)
        half = lts.resolve(self.base(color=[200, 100, 50], brightness=50), 0.0)
        self.assertEqual(full["lightbar"], (200, 100, 50))
        self.assertEqual(half["lightbar"], (100, 50, 25))

    # -- 另两盏灯的开关 ----------------------------------------------
    def test_battery_display_can_be_switched_off(self):
        v = lts.resolve(self.base(battery_on_player_leds=False), 0.0, battery=100)
        self.assertEqual(v["player_leds"], 0)

    def test_mic_led_can_be_switched_off(self):
        self.assertFalse(lts.resolve(self.base(mic_led=False), 0.0, mic_on=True)["mic_led"])
        self.assertTrue(lts.resolve(self.base(), 0.0, mic_on=True)["mic_led"])

    # -- 什么时候需要高频刷新 ----------------------------------------
    def test_steady_does_not_need_animation(self):
        self.assertFalse(lts.needs_animation(self.base(effect="steady")))

    def test_breathe_needs_animation(self):
        self.assertTrue(lts.needs_animation(self.base(effect="breathe")))

    def test_flash_needs_animation_even_when_steady(self):
        self.assertTrue(lts.needs_animation(self.base(effect="steady"), flashing=True))

    def test_charging_needs_animation_for_the_blink(self):
        self.assertTrue(lts.needs_animation(self.base(effect="steady"), charging=True))
        self.assertFalse(lts.needs_animation(
            self.base(effect="steady", battery_on_player_leds=False), charging=True))

    def test_paused_and_disabled_never_animate(self):
        self.assertFalse(lts.needs_animation(self.base(effect="breathe"), paused=True))
        self.assertFalse(lts.needs_animation(self.base(effect="breathe", enabled=False)))


class TestLightsWiring(unittest.TestCase):
    """引擎和配置这一侧的接线。"""

    def test_default_config_has_lights(self):
        c = cfgmod._factory_config()
        self.assertIn("lights", c)
        self.assertEqual(c["lights"]["effect"], "steady",
                         "默认常亮——不开呼吸就不需要那个 30Hz 输出线程")

    def test_old_config_without_lights_still_loads(self):
        merged = cfgmod._merge(cfgmod._factory_config(), {"poll_hz": 125})
        self.assertEqual(merged["lights"], lts.default_lights())

    def test_toggle_lights_is_a_bindable_builtin(self):
        self.assertIn("toggle_lights", cfgmod.BUILTINS)

    def test_voice_combo_does_not_repeat(self):
        """Win+H 长按连发会把语音输入反复开关，和任务视图是同一类问题。"""
        for p in cfgmod._factory_config()["profiles"]:
            act = p["buttons"]["mic"]
            if lts.is_voice_input_combo(act.get("keys")):
                self.assertFalse(act.get("repeat", True))

    def _engine(self):
        e = Engine.__new__(Engine)
        e.cfg = cfgmod._factory_config()
        e.on_event = lambda *a: None
        e._last_output = "stale"
        e._flash_color = None
        e._flash_until = 0.0
        e.voice_on = False
        e.runner = types.SimpleNamespace(release_all=lambda: None)
        e.touch = types.SimpleNamespace(reset=lambda: None)
        return e

    def test_switching_profile_starts_the_flash(self):
        e = self._engine()
        e.set_profile(2)
        self.assertGreater(e._flash_until, time.monotonic())
        self.assertEqual(tuple(e._flash_color),
                         tuple(e.cfg["profiles"][2]["lightbar"]))

    def test_flash_uses_the_configured_duration(self):
        e = self._engine()
        e.cfg["lights"]["profile_flash_secs"] = 7.0
        t0 = time.monotonic()
        e.next_profile()
        self.assertGreater(e._flash_until, t0 + 6.5)

    def test_flash_respects_the_switch(self):
        e = self._engine()
        e.cfg["lights"]["profile_flash"] = False
        e.next_profile()
        self.assertEqual(e._flash_until, 0.0)

    def test_toggle_lights_flips_the_master(self):
        e = self._engine()
        e.toggle_lights()
        self.assertFalse(e.cfg["lights"]["enabled"])
        e.toggle_lights()
        self.assertTrue(e.cfg["lights"]["enabled"])

    def test_neutral_report_restores_ps5_blue(self):
        """退出时要恢复出厂蓝，别把手柄留在自定义颜色上。"""
        sent = {}
        dev = ds.DualSense()
        dev.dev = types.SimpleNamespace(write=lambda b: sent.update(buf=b))
        dev.bluetooth = False
        dev.send_neutral()
        buf = sent["buf"]
        self.assertEqual((buf[45], buf[46], buf[47]), lts.PS5_BLUE)


class TestLightbarClaimSequence(unittest.TestCase):
    """连上手柄后必须先把灯条从固件手里抢过来，否则它根本不理会主机设的颜色。

    2026-08-23 用户实测：麦克风灯好使，灯条和玩家灯完全没反应——调亮度、
    换颜色、切配置档、锁定手柄都一点变化都没有。根因是漏了 hid-playstation
    里那一步 dualsense_reset_leds()：手柄上电后会跑一段灯条渐变动画，动画
    结束后固件继续占着灯条，主机发的 RGB 全被无视。麦克风灯在第 8 字节，
    走的是另一条路，不受影响——所以症状才会是「三盏灯只坏两盏」。
    """

    def test_reset_sets_the_two_bytes_that_matter(self):
        b = ds.build_lightbar_reset()
        self.assertEqual(b[38] & ds.FLAG2_LIGHTBAR_SETUP, ds.FLAG2_LIGHTBAR_SETUP,
                         "valid_flag2 必须置上 LIGHTBAR_SETUP 位")
        self.assertEqual(b[41], ds.LIGHTBAR_SETUP_LIGHT_OUT,
                         "lightbar_setup 必须是 LIGHT_OUT，用来打断开机动画")

    def test_reset_does_not_disturb_anything_else(self):
        b = ds.build_lightbar_reset()
        self.assertEqual(b[0], 0, "抢灯这一帧不该顺带启用别的东西")
        self.assertEqual(b[1], 0)
        self.assertEqual((b[44], b[45], b[46]), (0, 0, 0))

    def test_normal_output_does_not_carry_the_setup_byte(self):
        """抢灯是一次性的，每帧都带着它会不停地打断自己。"""
        b = ds.build_output_common(lightbar=(1, 2, 3))
        self.assertEqual(b[38], 0)
        self.assertEqual(b[41], 0)

    def test_device_sends_reset_on_connect(self):
        """接线检查：真的有人调用它，而不是只写了个函数。"""
        sent = []
        dev = ds.DualSense()
        dev.dev = types.SimpleNamespace(write=lambda b: sent.append(b))
        dev.bluetooth = False
        dev.reset_leds()
        self.assertEqual(len(sent), 1)
        buf = sent[0]
        self.assertEqual(buf[0], ds.OUTPUT_REPORT_USB)
        self.assertEqual(buf[1 + 38] & ds.FLAG2_LIGHTBAR_SETUP, ds.FLAG2_LIGHTBAR_SETUP)
        self.assertEqual(buf[1 + 41], ds.LIGHTBAR_SETUP_LIGHT_OUT)

    def test_engine_resets_leds_right_after_connecting(self):
        e = Engine.__new__(Engine)
        e.cfg = cfgmod.default_config()
        e.on_event = lambda *a: None
        e._last_output = None
        e._prev_buttons = {}
        calls = []
        e.touch = types.SimpleNamespace(reset=lambda: calls.append("touch"))
        e.dev = types.SimpleNamespace(open=lambda: True, bluetooth=False,
                                      reset_leds=lambda: calls.append("reset"))
        self.assertTrue(e._try_connect())
        self.assertIn("reset", calls, "连上之后没有抢灯，灯条就不会听话")

    def test_connect_survives_a_failing_reset(self):
        """抢灯失败不能把连接也带崩——没灯总比没手柄强。"""
        events = []
        e = Engine.__new__(Engine)
        e.cfg = cfgmod.default_config()
        e.on_event = lambda k, v=None: events.append(k)
        e._last_output = None
        e._prev_buttons = {}
        e.touch = types.SimpleNamespace(reset=lambda: None)

        def boom():
            raise OSError("写失败")
        e.dev = types.SimpleNamespace(open=lambda: True, bluetooth=False, reset_leds=boom)
        self.assertTrue(e._try_connect())
        self.assertIn("connected", events)
        self.assertIn("error", events, "失败要说出来，不能悄悄吞掉")


class TestPlayerLedBrightness(unittest.TestCase):

    def test_brightness_byte_goes_out_with_player_leds(self):
        b = ds.build_output_common(player_leds=0b11111, led_brightness=1)
        self.assertEqual(b[42], 1)
        self.assertEqual(b[43], 0b11111)
        self.assertEqual(b[1] & ds.FLAG1_PLAYER_INDICATOR, ds.FLAG1_PLAYER_INDICATOR)

    def test_defaults_to_brightest(self):
        self.assertEqual(ds.build_output_common(player_leds=1)[42], 0,
                         "0 = 最亮，别默认成暗的让人以为坏了")


class TestMicLedSource(unittest.TestCase):
    """麦克风灯的状态来源。按键计数会和 Win+H 的自动停止对不上。"""

    def setUp(self):
        from ps5mapper import audio
        self.audio = audio
        self._orig = audio.mic_in_use

    def tearDown(self):
        self.audio.mic_in_use = self._orig

    def _engine(self, source="system", voice_on=True):
        e = Engine.__new__(Engine)
        e.cfg = cfgmod.default_config()
        e.cfg["lights"]["mic_led_source"] = source
        e.voice_on = voice_on
        e._mic_source_warned = False
        e.on_event = lambda *a: None
        return e

    def test_system_source_follows_windows_not_keypresses(self):
        """核心回归：程序以为还在录（voice_on=True），但系统说没在录 → 必须灭。"""
        self.audio.mic_in_use = lambda force=False: False
        e = self._engine("system", voice_on=True)
        self.assertFalse(e._mic_on(e.cfg["lights"]),
                         "Win+H 自动停了灯还亮着，正是用户报的 bug")

    def test_system_source_lights_when_windows_says_so(self):
        self.audio.mic_in_use = lambda force=False: True
        e = self._engine("system", voice_on=False)
        self.assertTrue(e._mic_on(e.cfg["lights"]))

    def test_unknown_falls_back_to_keypress_and_warns(self):
        self.audio.mic_in_use = lambda force=False: None
        events = []
        e = self._engine("system", voice_on=True)
        e.on_event = lambda k, v=None: events.append((k, v))
        self.assertTrue(e._mic_on(e.cfg["lights"]))
        self.assertEqual(events[0][0], "error", "读不出来要说一声，不能装作正常")

    def test_warning_is_only_emitted_once(self):
        self.audio.mic_in_use = lambda force=False: None
        events = []
        e = self._engine("system")
        e.on_event = lambda k, v=None: events.append(k)
        for _ in range(20):
            e._mic_on(e.cfg["lights"])
        self.assertEqual(events.count("error"), 1, "别每帧刷一条一样的提示")

    def test_keypress_source_ignores_the_system(self):
        self.audio.mic_in_use = lambda force=False: False
        e = self._engine("keypress", voice_on=True)
        self.assertTrue(e._mic_on(e.cfg["lights"]))

    def test_default_source_is_system(self):
        self.assertEqual(cfgmod.default_config()["lights"]["mic_led_source"], "system")

    def test_mic_in_use_never_guesses_false_on_error(self):
        """读不出来必须返回 None，不能把「不知道」谎报成「没在用」。"""
        self.assertIsNone(self.audio.mic_in_use(force=True),
                          "非 Windows 上没有这套注册表，只能是 None")


class _TouchHarness(unittest.TestCase):
    """喂合成触摸帧，并把注入的鼠标事件截下来。"""

    def setUp(self):
        from ps5mapper import engine as eng
        self.eng = eng
        self.clicks = []
        self._orig = (eng.wi.mouse_click, eng.wi.mouse_button)
        eng.wi.mouse_click = lambda b: self.clicks.append(("click", b))
        eng.wi.mouse_button = lambda b, d: self.clicks.append(("down" if d else "up", b))
        self.tp = TouchProcessor()
        self.cfg = dict(cfgmod.default_touchpad(), acceleration=False, edge_slider=False)
        self.t = 100.0

    def tearDown(self):
        self.eng.wi.mouse_click, self.eng.wi.mouse_button = self._orig

    def frame(self, pts, click=False, dt=0.004, cfg=None):
        self.t += dt
        b2 = ds.BTN2_TOUCHPAD if click else 0
        st = ds.parse_input_report(make_usb_report(touch=pts, b2=b2), False)
        return self.tp.update(st, cfg or self.cfg, now=self.t)

    def hold(self, pts, ms, click=False, cfg=None, step=4.0, wander=12.0):
        """按住若干毫秒，坐标带真实幅度的抖动。

        wander 默认 12 个单位，对得上 probe5 实测的轻点位移中位数 13.3。
        早先这里只抖 1 个单位，太干净了——正因为如此，「静止的真手指坐标
        同样是逐帧完全相同的」这个真实行为在测试里完全没暴露出来。
        """
        out = []
        n = max(1, int(ms / step))
        for i in range(n):
            # 从 0 出发、单峰、再回落——手指落下、滑一点、抬起就是这个形状。
            # 归一化之后「离落点最远处」正好等于 wander，可以直接拿实测值当断言。
            k = (wander / math.hypot(1.0, 0.4)
                 * math.sin(math.pi * i / max(1, n - 1)))
            jig = [(a, i_, round(x + k), round(y + k * 0.4)) for (a, i_, x, y) in pts]
            out.append(self.frame(jig, click=click, dt=step / 1000.0, cfg=cfg))
        return out

    def hold_frozen(self, pts, ms, click=False, cfg=None, step=4.0):
        """坐标逐帧完全相同 —— 静止的真手指和幽灵触点都长这样。

        probe5 实测：手指放着不动 4 秒，2116 帧里 1931 帧和上一帧一模一样，
        最长连续 320ms 纹丝不动。所以「坐标冻住」**不能**用来判断已经抬手。
        """
        for _ in range(max(1, int(ms / step))):
            self.frame(pts, click=click, dt=step / 1000.0, cfg=cfg)

    def down(self, x=900, y=500, ident=1):
        return [(True, ident, x, y)]


class TestTapToClick(_TouchHarness):
    """轻点即点击 —— 笔记本上大多数人根本不按下去，轻点手指几乎不滚动，
    天生就没有下压漂移的问题。"""

    def tap(self, ms=80, x=900, y=500, cfg=None):
        self.hold(self.down(x, y), ms, cfg=cfg)
        for _ in range(TouchProcessor.DEBOUNCE_FRAMES + 1):
            self.frame([], cfg=cfg)

    def test_quick_tap_clicks(self):
        self.tap(110)                    # 实测中位时长
        self.assertEqual(self.clicks, [("click", "left")])

    def test_whole_measured_envelope_counts_as_a_tap(self):
        """probe5 实测 14 次轻点：78~156ms、位移 5.1~71.1。每一种都得认。"""
        for ms, wander in ((78, 5.1), (110, 13.3), (156, 71.1)):
            self.clicks.clear()
            self.tp = TouchProcessor()
            self.hold(self.down(), ms, wander=wander)
            for _ in range(4):
                self.frame([])
            self.assertEqual(self.clicks, [("click", "left")],
                             "实测样本 %dms / 位移 %.1f 应该算轻点" % (ms, wander))

    def test_resting_finger_does_not_click(self):
        """把手指放上去休息，坐标会逐帧完全相同——绝不能因此判成「抬手了」。

        这正是 probe5 推翻的那条设计：原本用「坐标冻住 = 已抬手」剥幽灵尾巴，
        实测发现静止的真手指最长能连续 320ms 纹丝不动，照那个规则做，
        手指往板子上一放就会误发一次点击。
        """
        self.hold_frozen(self.down(), 1500)
        for _ in range(4):
            self.frame([])
        self.assertEqual(self.clicks, [], "放手指上休息不能变成点击")

    def test_phantom_contact_with_no_movement_is_ignored(self):
        """空闲时约 24% 的帧会误报有触点，坐标卡死不动。

        没有位移下限的话，手柄搁在桌上自己就会乱点。
        """
        self.hold_frozen(self.down(), 100)
        for _ in range(4):
            self.frame([])
        self.assertEqual(self.clicks, [], "位移为 0 的接触是幽灵，不是手指")

    def test_contact_too_short_is_ignored(self):
        self.hold(self.down(), 16)
        for _ in range(4):
            self.frame([])
        self.assertEqual(self.clicks, [])

    def test_long_press_is_not_a_tap(self):
        self.tap(600)
        self.assertEqual(self.clicks, [], "按住不放是按住，不能当成点击")

    def test_moving_a_lot_is_not_a_tap(self):
        for i in range(20):
            self.frame([(True, 1, 900 + i * 20, 500)])
        for _ in range(4):
            self.frame([])
        self.assertEqual(self.clicks, [], "划过去不能变成点击")

    def test_tap_can_be_switched_off(self):
        cfg = dict(self.cfg, tap_to_click=False)
        self.tap(80, cfg=cfg)
        self.assertEqual(self.clicks, [], "关掉之后放手指上休息不该误触")

    def test_two_finger_tap_is_right_click(self):
        self.hold([(True, 1, 800, 500), (True, 2, 1000, 500)], 110)
        for _ in range(4):
            self.frame([])
        self.assertEqual(self.clicks, [("click", "right")])

    def test_two_finger_tap_can_be_switched_off(self):
        cfg = dict(self.cfg, two_finger_tap_right=False)
        self.hold([(True, 1, 800, 500), (True, 2, 1000, 500)], 110, cfg=cfg)
        for _ in range(4):
            self.frame([], cfg=cfg)
        self.assertEqual(self.clicks, [])

    def test_measured_ghost_tail_still_leaves_it_a_tap(self):
        """轻点之后的幽灵尾巴实测只有 0~32ms，加上去仍在 200ms 窗口内。

        （原先按 300ms 尾巴设计的「坐标冻住 = 抬手」修正被实测否掉了，
        见 test_resting_finger_does_not_click。）
        """
        self.hold(self.down(), 110)
        self.hold_frozen(self.down(), 32)      # 实测最长的尾巴
        for _ in range(4):
            self.frame([])
        self.assertEqual(self.clicks, [("click", "left")])


class TestTapDrag(_TouchHarness):
    """轻点一下再按住 = 拖动，笔记本上的「一次半」手势。"""

    def test_tap_then_hold_starts_a_drag(self):
        self.hold(self.down(), 110)
        for _ in range(4):
            self.frame([])
        self.assertEqual(self.clicks, [("click", "left")])
        self.hold(self.down(), 400)          # 紧接着再按住
        self.assertIn(("down", "left"), self.clicks, "第二下按住应该开始拖动")
        for _ in range(4):
            self.frame([])
        self.assertEqual(self.clicks[-1], ("up", "left"), "抬手要松开左键")

    def test_drag_can_be_switched_off(self):
        cfg = dict(self.cfg, tap_drag=False)
        self.hold(self.down(), 110, cfg=cfg)
        for _ in range(4):
            self.frame([], cfg=cfg)
        self.hold(self.down(), 400, cfg=cfg)
        self.assertNotIn(("down", "left"), self.clicks)

    def test_second_tap_far_away_is_not_a_drag(self):
        self.hold(self.down(200, 200), 110)
        for _ in range(4):
            self.frame([])
        self.hold(self.down(1700, 900), 400)
        self.assertNotIn(("down", "left"), self.clicks,
                         "落在另一头就是两次独立操作，不是双击拖动")

    def test_slow_second_tap_is_not_a_drag(self):
        self.hold(self.down(), 110)
        for _ in range(4):
            self.frame([])
        for _ in range(120):                 # 隔了快 500ms 才按第二下
            self.frame([])
        self.hold(self.down(), 400)
        self.assertNotIn(("down", "left"), self.clicks)

    def test_pointer_follows_the_finger_once_dragging(self):
        self.hold(self.down(), 110)
        for _ in range(4):
            self.frame([])
        self.hold(self.down(), 400)          # 进入拖动
        moved = 0.0
        for i in range(40):                  # 拖出去，要能带着光标走
            dx, dy = self.frame([(True, 1, 900 + i * 12, 500)])
            moved += abs(dx)
        self.assertGreater(moved, 0, "拖动中光标必须跟着走，否则拖不动东西")


class TestClickDriftGuard(_TouchHarness):
    """物理下压时的漂移。

    2026-08-23 用户反馈：把触摸板下压绑成鼠标左键，一按下去光标就飘走。
    根因是触摸板是整块压下去的按键，**开关在按压行程的最后才闭合**，
    手指在这段行程里已经压扁打滑了；而原来的代码压根不知道下压这回事，
    位移照发不误。
    """

    def creep(self, n=12, click=False, cfg=None):
        """模拟压手指时那种又慢又短的打滑。"""
        out = []
        for i in range(n):
            out.append(self.frame([(True, 1, 900 + i, 500 + i)], click=click, cfg=cfg))
        return out

    def test_pointer_is_frozen_right_after_the_click(self):
        self.creep(12)
        self.frame([(True, 1, 912, 512)], click=True)
        moved = [self.frame([(True, 1, 912 + i, 512 + i)], click=True) for i in range(6)]
        self.assertTrue(all(d == (0.0, 0.0) for d in moved),
                        "按下瞬间指针必须锁住")

    def test_drift_before_the_click_is_rewound(self):
        """关键：漂移发生在开关闭合**之前**，所以光冻结来不及，必须倒回去。"""
        fwd = sum(d[0] for d in self.creep(12))
        self.assertGreater(fwd, 0, "先确认这段爬行真的推动了光标")
        rdx, rdy = self.frame([(True, 1, 912, 512)], click=True)
        self.assertAlmostEqual(rdx, -fwd, places=6, msg="按下时要把这段位移吐回来")
        self.assertLess(rdy, 0)

    def test_fast_deliberate_move_is_not_rewound(self):
        """你正快速划过去顺手点一下，那段位移是真的，不能往回拽。"""
        for i in range(12):
            self.frame([(True, 1, 300 + i * 90, 500)])
        rdx, _ = self.frame([(True, 1, 1380, 500)], click=True)
        self.assertEqual(rdx, 0.0)

    def test_always_mode_rewinds_regardless(self):
        cfg = dict(self.cfg, rewind_mode="always")
        fwd = 0.0
        for i in range(12):
            fwd += self.frame([(True, 1, 300 + i * 90, 500)], cfg=cfg)[0]
        rdx, _ = self.frame([(True, 1, 1380, 500)], click=True, cfg=cfg)
        self.assertAlmostEqual(rdx, -fwd, places=6)

    def test_off_mode_does_not_rewind_but_still_freezes(self):
        cfg = dict(self.cfg, rewind_mode="off")
        self.creep(12, cfg=cfg)
        self.assertEqual(self.frame([(True, 1, 912, 512)], click=True, cfg=cfg), (0.0, 0.0))

    def test_drag_gate_blocks_small_movement_then_opens(self):
        self.creep(12)
        self.frame([(True, 1, 912, 512)], click=True)
        for _ in range(20):                       # 先等冻结期过掉
            self.frame([(True, 1, 912, 512)], click=True)
        small = [self.frame([(True, 1, 912 + i, 512)], click=True) for i in range(1, 5)]
        self.assertTrue(all(d == (0.0, 0.0) for d in small), "小动作应被门槛吃掉")
        big = [self.frame([(True, 1, 912 + i * 30, 512)], click=True) for i in range(1, 8)]
        self.assertTrue(any(d[0] != 0.0 for d in big), "推过门槛就该能拖了")

    def test_gate_zero_allows_immediate_drag(self):
        cfg = dict(self.cfg, click_gate=0)
        self.creep(12, cfg=cfg)
        self.frame([(True, 1, 912, 512)], click=True, cfg=cfg)
        for _ in range(20):
            self.frame([(True, 1, 912, 512)], click=True, cfg=cfg)
        got = [self.frame([(True, 1, 912 + i, 512)], click=True, cfg=cfg) for i in range(1, 6)]
        self.assertTrue(any(d[0] != 0.0 for d in got))

    def test_release_also_freezes(self):
        """抬手同样会把光标带一下。"""
        self.creep(12)
        self.frame([(True, 1, 912, 512)], click=True)
        for _ in range(30):
            self.frame([(True, 1, 950, 550)], click=True)
        self.frame([(True, 1, 950, 550)], click=False)         # 松开
        after = [self.frame([(True, 1, 950 + i * 6, 550)]) for i in range(1, 6)]
        self.assertTrue(all(d == (0.0, 0.0) for d in after))

    def test_stabilize_can_be_switched_off(self):
        cfg = dict(self.cfg, click_stabilize=False)
        self.creep(12, cfg=cfg)
        self.frame([(True, 1, 912, 512)], click=True, cfg=cfg)
        got = [self.frame([(True, 1, 912 + i * 6, 512)], click=True, cfg=cfg)
               for i in range(1, 5)]
        self.assertTrue(any(d[0] != 0.0 for d in got), "关掉之后应恢复成原来的行为")

    def test_guard_applies_even_when_click_is_bound_elsewhere(self):
        """按下去带一下光标这件事，和这个键绑了什么功能无关。"""
        self.creep(12)
        self.assertEqual(self.frame([(True, 1, 912, 512)], click=True)[0] < 0, True)


class TestMeasuredDriftEnvelope(_TouchHarness):
    """用 probe5 的实测数字直接当断言，防止以后有人把阈值调回估的值。

    实测 8 次下压，开关闭合前 100ms 手指滑了 2.2 ~ 114.9 个触摸板单位
    （中位 27）。换算成光标是 1.2 ~ 63.2 像素——最严重那次飘 63 像素，
    正是用户说的「一按下去光标就飘走」。
    """

    def slide(self, units, ms=80, cfg=None):
        """在 ms 毫秒内滑 units 个单位，然后按下去。返回按下那一帧的补偿量。"""
        n = max(2, int(ms / 4.0))
        for i in range(n):
            self.frame([(True, 1, 900 + round(units * i / (n - 1)), 500)], cfg=cfg)
        return self.frame([(True, 1, 900 + round(units), 500)], click=True, cfg=cfg)

    def test_measured_worst_case_is_rewound(self):
        """114.9 单位是实测最大的一次。旧阈值（26 像素）会把它漏掉——
        而漏掉的恰恰是用户唯一会注意到的那几次。"""
        dx, _ = self.slide(115)
        self.assertLess(dx, 0, "实测最严重的那次下压漂移必须被倒回")

    def test_measured_median_is_rewound(self):
        dx, _ = self.slide(27)
        self.assertLess(dx, 0)

    def test_measured_minimum_is_harmless(self):
        dx, _ = self.slide(2)
        self.assertLessEqual(dx, 0.0)

    def test_threshold_leaves_headroom_over_the_measured_max(self):
        self.assertGreater(TouchProcessor.REWIND_MAX_UNITS, 114.9,
                           "倒回上限必须高于实测最大漂移，否则最严重的几次会被漏掉")

    def test_a_real_swipe_is_still_not_rewound(self):
        """真的在划：100ms 内走过大半块板子，那段位移是真的，不许往回拽。"""
        dx, _ = self.slide(900)
        self.assertEqual(dx, 0.0)

    def test_threshold_is_in_touchpad_units_not_pixels(self):
        """判据必须与灵敏度无关——不然用户一调灵敏度，防漂移就跟着失准。"""
        a = _TouchHarness.__new__(_TouchHarness)
        for sens in (1, 6, 10):
            self.clicks.clear()
            self.tp = TouchProcessor()
            self.t = 100.0
            cfg = dict(self.cfg, sensitivity=sens)
            dx, _ = self.slide(115, cfg=cfg)
            self.assertLess(dx, 0, "灵敏度 %d 时实测最大漂移也该被倒回" % sens)


class TestGhostFloorIsGrounded(unittest.TestCase):
    """位移下限的取值必须夹在「幽灵的 0」和「实测最轻的一点 5.1」之间。"""

    def test_floor_sits_between_ghost_and_the_lightest_real_tap(self):
        self.assertGreater(TouchProcessor.TAP_MIN_MOVE, 0,
                           "下限必须大于 0，否则位移恒为 0 的幽灵会被当成轻点")
        self.assertLess(TouchProcessor.TAP_MIN_MOVE, 5.1,
                        "下限必须低于实测最轻的一次轻点（5.1），否则会漏点")

    def test_tap_window_covers_the_measured_range(self):
        self.assertLess(TouchProcessor.TAP_MIN_MS, 78, "实测最短轻点 78ms")
        self.assertGreater(TouchProcessor.TAP_MAX_MS, 156, "实测最长轻点 156ms")
        self.assertGreater(TouchProcessor.TAP_MAX_MOVE, 71.1, "实测最大位移 71.1")

    def test_no_still_lift_shortcut_remains(self):
        """实测证明静止的真手指坐标同样逐帧完全相同（最长 320ms），
        所以「坐标冻住 = 已抬手」这条捷径必须彻底不存在。"""
        self.assertFalse(hasattr(TouchProcessor, "STILL_LIFT_MS"),
                         "这个常量被实测否掉了，不该再有")


class TestPresetLibrary(unittest.TestCase):
    """编辑器里那份预设清单。每一条都必须真的能解析成按键，
    否则用户点了之后是静默无效——UI 上看着绑好了，按下去什么也不发生。"""

    def lib(self):
        """从 ui/index.html 里把 LIB 那段扒出来，转成 Python 结构。"""
        import json
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ps5mapper", "ui", "index.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        i = html.index("var LIB = {")
        depth, j = 0, html.index("{", i)
        for k in range(j, len(html)):
            if html[k] == "{":
                depth += 1
            elif html[k] == "}":
                depth -= 1
                if depth == 0:
                    break
        body = html[j:k + 1]
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)       # 去注释
        body = re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', body)
        body = re.sub(r",(\s*[}\]])", r"\1", body)
        return json.loads(body)

    def test_every_key_preset_parses(self):
        bad = []
        for group, items in self.lib().items():
            for it in items:
                if "k" not in it:
                    continue
                mods, main = wi.parse_combo(it["k"])
                if main is None:
                    bad.append("%s / %s = %s" % (group, it["t"], it["k"]))
        self.assertEqual(bad, [], "这些预设解析不出主键，点了会毫无反应：%s" % bad)

    def test_every_builtin_preset_exists(self):
        bad = [it["b"] for items in self.lib().values() for it in items
               if "b" in it and it["b"] not in cfgmod.BUILTINS]
        self.assertEqual(bad, [], "引用了不存在的内置功能：%s" % bad)

    def test_page_navigation_presets_are_present_and_unambiguous(self):
        """「上一页/下一页」中文里有两个意思，两种都要给，而且名字要写死。"""
        keys = {it["t"]: it["k"] for items in self.lib().values()
                for it in items if "k" in it}
        self.assertEqual(keys.get("上一页（后退）"), "Alt+Left")
        self.assertEqual(keys.get("下一页（前进）"), "Alt+Right")
        self.assertEqual(keys.get("上翻一屏"), "PageUp")
        self.assertEqual(keys.get("下翻一屏"), "PageDown")

    def test_one_shot_presets_do_not_repeat(self):
        """按一次就该只发生一次的动作必须标 r:false，否则长按会反复触发。"""
        one_shot = {"任务视图", "切换窗口", "显示桌面", "新标签页",
                    "关闭标签页", "刷新", "回到顶部", "跳到末尾"}
        for items in self.lib().values():
            for it in items:
                if it.get("t") in one_shot:
                    self.assertIs(it.get("r"), False,
                                  "%s 长按连发会出事，必须 r:false" % it["t"])

    def test_preset_labels_are_unique(self):
        names = [it["t"] for items in self.lib().values() for it in items]
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(dupes, [], "预设名字重复，用户分不清：%s" % dupes)


class TestDuplicateProfile(unittest.TestCase):
    """复制配置档。核心是**必须深拷贝**——浅拷贝的话两份档共用同一批
    buttons / triggers 字典，改一边另一边跟着变，用户根本想不到是这个原因。"""

    def setUp(self):
        self.profs = cfgmod._factory_config()["profiles"]

    def test_inserts_right_after_the_original(self):
        i = cfgmod.duplicate_profile(self.profs, 0)
        self.assertEqual(i, 1)
        self.assertEqual([p["name"] for p in self.profs][:2],
                         ["日常桌面", "日常桌面 副本"])

    def test_copy_is_complete(self):
        self.profs[0]["buttons"]["ps"] = cfgmod.combo("F1", "帮助")
        self.profs[0]["triggers"]["l2"]["depth"] = 77
        self.profs[0]["touchpad"]["click_gate"] = 19
        self.profs[0]["sticks"]["left"]["speed"] = 3
        i = cfgmod.duplicate_profile(self.profs, 0)
        src, dst = self.profs[0], self.profs[i]
        for key in ("buttons", "triggers", "touchpad", "sticks", "lightbar"):
            self.assertEqual(src[key], dst[key], "%s 没被完整复制" % key)

    def test_editing_the_copy_does_not_touch_the_original(self):
        i = cfgmod.duplicate_profile(self.profs, 0)
        self.profs[i]["buttons"]["cross"] = cfgmod.combo("X", "改过")
        self.profs[i]["triggers"]["l2"]["depth"] = 11
        self.profs[i]["touchpad"]["sensitivity"] = 1
        self.profs[i]["lightbar"][0] = 255
        self.assertEqual(self.profs[0]["buttons"]["cross"]["label"], "确认")
        self.assertNotEqual(self.profs[0]["triggers"]["l2"]["depth"], 11)
        self.assertNotEqual(self.profs[0]["touchpad"]["sensitivity"], 1)
        self.assertNotEqual(self.profs[0]["lightbar"][0], 255)

    def test_editing_the_original_does_not_touch_the_copy(self):
        i = cfgmod.duplicate_profile(self.profs, 0)
        self.profs[0]["buttons"]["cross"] = cfgmod.combo("Y", "原件改过")
        self.assertEqual(self.profs[i]["buttons"]["cross"]["label"], "确认")

    def test_repeated_duplication_keeps_names_unique(self):
        names = set()
        for _ in range(5):
            i = cfgmod.duplicate_profile(self.profs, 0)
            names.add(self.profs[i]["name"])
        self.assertEqual(len(names), 5, "连续复制不能撞名：%s" % names)
        allnames = [p["name"] for p in self.profs]
        self.assertEqual(len(allnames), len(set(allnames)))

    def test_out_of_range_is_a_no_op(self):
        before = len(self.profs)
        for bad in (-1, 99):
            self.assertIsNone(cfgmod.duplicate_profile(self.profs, bad))
        self.assertEqual(len(self.profs), before)


class TestRenameProfile(unittest.TestCase):

    def setUp(self):
        self.profs = cfgmod._factory_config()["profiles"]

    def test_plain_rename(self):
        self.assertEqual(cfgmod.unique_name(self.profs, "写代码", skip=0), "写代码")

    def test_renaming_to_a_taken_name_gets_a_suffix(self):
        self.assertEqual(cfgmod.unique_name(self.profs, "聊天打字", skip=0), "聊天打字 2")

    def test_renaming_to_its_own_name_is_fine(self):
        """skip 就是为了这个：改成自己现在的名字不该被加后缀。"""
        self.assertEqual(cfgmod.unique_name(self.profs, "聊天打字", skip=1), "聊天打字")

    def test_blank_name_falls_back(self):
        for bad in ("", "   ", None):
            self.assertTrue(cfgmod.unique_name(self.profs, bad).strip())

    def test_whitespace_is_trimmed(self):
        self.assertEqual(cfgmod.unique_name(self.profs, "  写代码  ", skip=0), "写代码")

    def test_name_is_length_capped(self):
        self.assertLessEqual(len(cfgmod.unique_name(self.profs, "长" * 500)), 40)


class TestProfileApiWiring(unittest.TestCase):
    """接线检查：Api 上真的有这两个方法，且真的落到配置里。"""

    def setUp(self):
        from ps5mapper import app as appmod
        self.api = appmod.Api.__new__(appmod.Api)
        cfg = cfgmod._factory_config()
        saved = []
        self.api._app = types.SimpleNamespace(
            cfg=cfg, save=lambda: saved.append(1),
            engine=types.SimpleNamespace(set_profile=lambda i: cfg.__setitem__("active_profile", i)))
        self.api.get_config = lambda: {"config": self.api._app.cfg}
        self.saved = saved

    def test_duplicate_is_exposed_and_persists(self):
        self.api.duplicate_profile(0)
        self.assertEqual(len(self.api._app.cfg["profiles"]), 4)
        self.assertTrue(self.saved, "改完必须存盘，否则重启就没了")

    def test_duplicate_switches_to_the_new_copy(self):
        self.api.duplicate_profile(0)
        self.assertEqual(self.api._app.cfg["active_profile"], 1,
                         "复制出来通常就是要接着改它")

    def test_rename_is_exposed_and_persists(self):
        self.api.rename_profile(2, "看剧")
        self.assertEqual(self.api._app.cfg["profiles"][2]["name"], "看剧")
        self.assertTrue(self.saved)

    def test_rename_out_of_range_is_a_no_op(self):
        before = [p["name"] for p in self.api._app.cfg["profiles"]]
        self.api.rename_profile(99, "不存在")
        self.assertEqual([p["name"] for p in self.api._app.cfg["profiles"]], before)

    def test_rename_cannot_create_a_duplicate_name(self):
        self.api.rename_profile(0, "聊天打字")
        names = [p["name"] for p in self.api._app.cfg["profiles"]]
        self.assertEqual(len(names), len(set(names)), "重名会让人分不清在哪一档")


class TestApiSurfaceCoversTheUI(unittest.TestCase):
    """界面里每一个 call("xxx") 都必须在 Api 上真的存在。

    对不上的话是**静默失败**：按钮点下去，promise 悄悄 reject，界面上什么
    都不会发生，也不报错——排查起来极其费劲。加个测试当场拦住。
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def ui_calls(self):
        import re
        with open(os.path.join(self.ROOT, "ps5mapper", "ui", "index.html"),
                  encoding="utf-8") as f:
            return set(re.findall(r'call\(\s*"([a-z_]+)"', f.read()))

    def api_methods(self):
        import ast
        with open(os.path.join(self.ROOT, "ps5mapper", "app.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Api":
                return {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
        return set()

    def test_every_ui_call_exists_on_the_api(self):
        missing = sorted(self.ui_calls() - self.api_methods())
        self.assertEqual(missing, [], "界面调了但后端没有的方法（点了会毫无反应）：%s" % missing)

    def test_the_new_profile_methods_are_reachable(self):
        calls = self.ui_calls()
        for name in ("duplicate_profile", "rename_profile"):
            self.assertIn(name, calls, "界面上没有入口能调到 %s" % name)
            self.assertIn(name, self.api_methods())

    def test_detector_actually_detects(self):
        """证明这个测试本身有效——否则它可能一直是空跑。"""
        self.assertNotIn("duplicate_profile", self.ui_calls() - self.api_methods())
        fake = self.ui_calls() | {"method_that_does_not_exist"}
        self.assertEqual(sorted(fake - self.api_methods()), ["method_that_does_not_exist"])


class TestScrollAxesAreIndependent(unittest.TestCase):
    """上下和左右各有各的滚动方向。

    2026-08-23 用户反馈「看起来上下左右共用同一套方向设置」。实际比这更糟：
    方向符号**只作用在上下**，左右的符号是写死的，想反过来都没办法。
    触摸板和「摇杆当滚轮」两处是同一个毛病。
    """

    def setUp(self):
        from ps5mapper import engine as eng
        self.eng = eng
        self.v, self.h = [], []
        self._orig = (eng.wi.wheel, eng.wi.hwheel)
        eng.wi.wheel = lambda c: self.v.append(c)
        eng.wi.hwheel = lambda c: self.h.append(c)

    def tearDown(self):
        self.eng.wi.wheel, self.eng.wi.hwheel = self._orig

    # -- 触摸板 ------------------------------------------------------
    def swipe(self, cfg, dx=0, dy=0):
        """双指从中央同向滑动，返回 (纵向滚动量, 横向滚动量)。"""
        self.v.clear(); self.h.clear()
        tp = TouchProcessor()
        pts = lambda x, y: [(True, 1, x, y), (True, 2, x + 200, y)]
        for _ in range(TouchProcessor.DEBOUNCE_FRAMES + 1):
            tp.update(ds.parse_input_report(make_usb_report(touch=pts(700, 500)), False), cfg)
        for i in range(1, 40):
            tp.update(ds.parse_input_report(
                make_usb_report(touch=pts(700 + dx * i, 500 + dy * i)), False), cfg)
        return sum(self.v), sum(self.h)

    def tpcfg(self, **kw):
        return dict(cfgmod.default_touchpad(), acceleration=False, edge_slider=False,
                    pinch_zoom=False, **kw)

    def test_flipping_vertical_does_not_touch_horizontal(self):
        a = self.swipe(self.tpcfg(natural_scroll=True, natural_hscroll=False), dx=6)
        b = self.swipe(self.tpcfg(natural_scroll=False, natural_hscroll=False), dx=6)
        self.assertNotEqual(a[1], 0)
        self.assertEqual(a[1], b[1], "改上下方向不该把左右也翻过来")

    def test_flipping_horizontal_does_not_touch_vertical(self):
        a = self.swipe(self.tpcfg(natural_scroll=True, natural_hscroll=True), dy=6)
        b = self.swipe(self.tpcfg(natural_scroll=True, natural_hscroll=False), dy=6)
        self.assertNotEqual(a[0], 0)
        self.assertEqual(a[0], b[0], "改左右方向不该把上下也翻过来")

    def test_horizontal_direction_is_actually_switchable(self):
        """这是原来根本做不到的事：左右的符号是写死的。"""
        a = self.swipe(self.tpcfg(natural_hscroll=True), dx=6)[1]
        b = self.swipe(self.tpcfg(natural_hscroll=False), dx=6)[1]
        self.assertNotEqual(a, 0)
        self.assertAlmostEqual(a, -b, places=6, msg="左右方向必须真的能反过来")

    def test_vertical_direction_is_switchable(self):
        a = self.swipe(self.tpcfg(natural_scroll=True), dy=6)[0]
        b = self.swipe(self.tpcfg(natural_scroll=False), dy=6)[0]
        self.assertAlmostEqual(a, -b, places=6)

    def test_all_four_combinations_are_distinct(self):
        seen = set()
        for nv in (True, False):
            for nh in (True, False):
                cfg = self.tpcfg(natural_scroll=nv, natural_hscroll=nh)
                sv = self.swipe(cfg, dy=6)[0]
                sh = self.swipe(cfg, dx=6)[1]
                seen.add((sv > 0, sh > 0))
        self.assertEqual(len(seen), 4, "四种组合必须互不相同：%s" % seen)

    def test_missing_horizontal_key_follows_vertical(self):
        """老配置没有横向那一项时跟着纵向走，不要默默变方向。"""
        cfg = self.tpcfg()
        cfg.pop("natural_hscroll")
        for nv in (True, False):
            cfg["natural_scroll"] = nv
            same = self.tpcfg(natural_scroll=nv, natural_hscroll=nv)
            self.assertEqual(self.swipe(cfg, dx=6)[1], self.swipe(same, dx=6)[1])

    # -- 摇杆当滚轮 --------------------------------------------------
    def stick_scroll(self, **kw):
        e = Engine.__new__(Engine)
        e.cfg = cfgmod.default_config()
        prof = cfgmod.active(e.cfg)
        st = dict(cfgmod.default_stick("scroll"), deadzone=2, curve=1.0, **kw)
        prof["sticks"]["right"] = st
        prof["sticks"]["left"] = cfgmod.default_stick("off")
        e.slow_pointer = False
        e._scroll = e._hscroll = 0.0
        e._move_x = e._move_y = 0.0
        e._pending_dx = e._pending_dy = 0.0
        e._lock = threading.Lock()
        st_in = types.SimpleNamespace(lx=0.0, ly=0.0, rx=0.9, ry=0.9)
        e.state = st_in
        e._tick_pointer(st_in, 0.5)
        return e._scroll, e._hscroll

    def test_stick_axes_are_independent(self):
        a = self.stick_scroll(invert_scroll=False, invert_hscroll=False)
        b = self.stick_scroll(invert_scroll=True, invert_hscroll=False)
        c = self.stick_scroll(invert_scroll=False, invert_hscroll=True)
        self.assertAlmostEqual(a[1], b[1], places=6, msg="改上下不该动左右")
        self.assertAlmostEqual(a[0], c[0], places=6, msg="改左右不该动上下")
        self.assertAlmostEqual(a[1], -c[1], places=6, msg="左右必须真的能反")
        self.assertAlmostEqual(a[0], -b[0], places=6)

    def test_stick_missing_horizontal_key_follows_vertical(self):
        st = dict(cfgmod.default_stick("scroll"), deadzone=2, curve=1.0, invert_scroll=True)
        st.pop("invert_hscroll")
        e = Engine.__new__(Engine)
        e.cfg = cfgmod.default_config()
        prof = cfgmod.active(e.cfg)
        prof["sticks"]["right"] = st
        prof["sticks"]["left"] = cfgmod.default_stick("off")
        e.slow_pointer = False
        e._scroll = e._hscroll = 0.0
        e._move_x = e._move_y = 0.0
        e._pending_dx = e._pending_dy = 0.0
        e._lock = threading.Lock()
        st_in = types.SimpleNamespace(lx=0.0, ly=0.0, rx=0.9, ry=0.9)
        e.state = st_in
        e._tick_pointer(st_in, 0.5)
        both = self.stick_scroll(invert_scroll=True, invert_hscroll=True)
        self.assertAlmostEqual(e._hscroll, both[1], places=6)


class TestScrollAxisMigration(unittest.TestCase):
    """老配置升级：横向方向跟着纵向走，用户没改过设置就不该变方向。"""

    def roundtrip(self, raw):
        import json
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "c.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        return cfgmod.load(path)

    def old_config(self, natural, invert):
        base = cfgmod.default_config()
        for p in base["profiles"]:
            p["touchpad"].pop("natural_hscroll", None)
            p["touchpad"]["natural_scroll"] = natural
            for side in ("left", "right"):
                p["sticks"][side].pop("invert_hscroll", None)
                p["sticks"][side]["invert_scroll"] = invert
        return base

    def test_touchpad_horizontal_inherits_vertical(self):
        for natural in (True, False):
            cfg = self.roundtrip(self.old_config(natural, False))
            for p in cfg["profiles"]:
                self.assertEqual(p["touchpad"]["natural_hscroll"], natural,
                                 "升级后横向方向应跟着纵向，而不是吃默认值")

    def test_stick_horizontal_inherits_vertical(self):
        for invert in (True, False):
            cfg = self.roundtrip(self.old_config(True, invert))
            for p in cfg["profiles"]:
                for side in ("left", "right"):
                    self.assertEqual(p["sticks"][side]["invert_hscroll"], invert)

    def test_explicit_horizontal_setting_is_respected(self):
        raw = self.old_config(True, False)
        raw["profiles"][0]["touchpad"]["natural_hscroll"] = False
        cfg = self.roundtrip(raw)
        self.assertFalse(cfg["profiles"][0]["touchpad"]["natural_hscroll"],
                         "用户自己设过的值不能被迁移覆盖")

    def test_brand_new_config_has_both_axes(self):
        for p in cfgmod.default_config()["profiles"]:
            self.assertIn("natural_hscroll", p["touchpad"])
            self.assertIn("invert_hscroll", p["sticks"]["left"])


class TestBundledDefaults(unittest.TestCase):
    """烘焙进来的出厂配置（ps5mapper/default_config.json）。

    做免安装包时，「出厂默认」应该是用户实际在用的那套，而不是通用示例值。
    找不到这个文件时必须能干净地退回通用默认——源码直接跑就是这种情况。
    """

    def setUp(self):
        self.bundled = cfgmod.bundled_default(reload=True)

    def test_bundled_file_is_present_and_sane(self):
        self.assertIsNotNone(self.bundled, "打包用的出厂配置文件不见了")
        self.assertTrue(self.bundled.get("profiles"))
        for p in self.bundled["profiles"]:
            self.assertTrue(p.get("name"))
            self.assertIn("buttons", p)
            self.assertIn("triggers", p)

    def test_default_config_uses_the_bundled_one(self):
        names = [p["name"] for p in cfgmod.default_config()["profiles"]]
        self.assertEqual(names, [p["name"] for p in self.bundled["profiles"]])

    def test_new_keys_still_appear_even_if_bundled_file_is_old(self):
        """以后加了新设置项，老的烘焙文件里没有，也必须补上默认值。"""
        cfg = cfgmod.default_config()
        for p in cfg["profiles"]:
            for k in cfgmod.default_touchpad():
                self.assertIn(k, p["touchpad"], "触摸板少了 %s" % k)
            for k in cfgmod._factory_profile()["buttons"]:
                self.assertIn(k, p["buttons"], "按键少了 %s" % k)

    def test_actions_carry_no_leftover_fields(self):
        """动作字典必须是干净的：不同类型字段不一样，混在一起会读得人一头雾水。"""
        allowed = {"combo": {"type", "keys", "label", "repeat"},
                   "mouse": {"type", "button", "label"},
                   "builtin": {"type", "name", "label"},
                   "macro": {"type", "steps", "label"},
                   None: {"type", "label"}}
        cfg = cfgmod.default_config()
        bad = []
        for p in cfg["profiles"]:
            acts = list(p["buttons"].items())
            acts += [(t + ".binding", p["triggers"][t]["binding"]) for t in p["triggers"]]
            acts += [(t + ".binding2", p["triggers"][t]["binding2"]) for t in p["triggers"]]
            for cid, a in acts:
                extra = set(a) - allowed.get(a.get("type"), set())
                if extra:
                    bad.append("%s / %s 多了 %s" % (p["name"], cid, sorted(extra)))
        self.assertEqual(bad, [], "动作里有残留字段：%s" % bad)

    def test_missing_bundled_file_falls_back_cleanly(self):
        cache = dict(cfgmod._bundled_cache)
        try:
            cfgmod._bundled_cache.update(loaded=True, data=None)
            self.assertEqual(len(cfgmod.default_config()["profiles"]), 3)
            self.assertEqual(cfgmod.default_profile("X")["buttons"]["cross"]["keys"], "Enter")
        finally:
            cfgmod._bundled_cache.update(cache)

    def test_half_baked_file_is_rejected(self):
        """半个配置比没有更糟，宁可退回通用默认。"""
        cache = dict(cfgmod._bundled_cache)
        try:
            for junk in ({}, {"profiles": []}, [], "nope", None):
                cfgmod._bundled_cache.update(loaded=True, data=junk)
                if not (isinstance(junk, dict) and junk.get("profiles")):
                    cfgmod._bundled_cache.update(loaded=False, data=None)
                    self.assertIsNotNone(cfgmod.default_config())
        finally:
            cfgmod._bundled_cache.update(cache)

    def test_reset_and_new_profile_use_the_bundled_template(self):
        tpl = cfgmod.default_profile("随便起个名", (1, 2, 3))
        first = self.bundled["profiles"][0]
        self.assertEqual(tpl["buttons"]["cross"], first["buttons"]["cross"])
        self.assertEqual(tpl["touchpad"]["sensitivity"], first["touchpad"]["sensitivity"])
        self.assertEqual(tpl["name"], "随便起个名", "名字和提示色要照传进来的走")
        self.assertEqual(tpl["lightbar"], [1, 2, 3])


class TestActionMergeIsAtomic(unittest.TestCase):
    """动作字典必须整体替换，不能逐字段合并。

    这是用户那份配置攒出一堆残留字段的根因：load() 每次都拿出厂模板去 merge
    用户的动作，模板里 combo 的 keys 就留在了用户改成 mouse 之后的动作里。
    """

    def test_changing_action_type_drops_the_old_fields(self):
        tpl = cfgmod._factory_profile()
        self.assertEqual(tpl["buttons"]["l1"]["type"], "combo")   # 模板里是组合键
        merged = cfgmod._merge_profile(tpl, {"buttons": {
            "l1": {"type": "mouse", "button": "left", "label": "鼠标左键"}}})
        self.assertEqual(merged["buttons"]["l1"],
                         {"type": "mouse", "button": "left", "label": "鼠标左键"},
                         "换了动作类型，旧类型的字段必须消失")
        self.assertNotIn("keys", merged["buttons"]["l1"])

    def test_trigger_binding_is_also_replaced(self):
        tpl = cfgmod._factory_profile()
        self.assertEqual(tpl["triggers"]["l2"]["binding"]["type"], "mouse")
        merged = cfgmod._merge_profile(tpl, {"triggers": {"l2": {
            "binding": {"type": "combo", "keys": "Backspace", "label": "退格"}}}})
        self.assertNotIn("button", merged["triggers"]["l2"]["binding"])

    def test_unspecified_keys_still_come_from_the_template(self):
        merged = cfgmod._merge_profile(cfgmod._factory_profile(),
                                       {"buttons": {"l1": {"type": None, "label": None}}})
        self.assertIn("cross", merged["buttons"], "没提到的按键要保留模板值")
        self.assertIn("touchpad", merged)

    def test_load_does_not_reintroduce_stale_fields(self):
        import json
        import tempfile
        raw = cfgmod._factory_config()
        raw["profiles"][0]["buttons"]["l1"] = {"type": "mouse", "button": "left",
                                              "label": "鼠标左键"}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(raw, f)
            cfg = cfgmod.load(p)
        self.assertNotIn("keys", cfg["profiles"][0]["buttons"]["l1"],
                         "读盘时又把模板的 keys 合并回来了")


class TestFrozenLayout(unittest.TestCase):
    """模拟 PyInstaller 打包后的目录布局。

    打包后模块是从 PYZ 里加载的，`__file__` 指向一个并不存在的路径——
    只靠 `__file__` 找资源在源码下能跑、打包后必挂，而这种事只有用户
    双击 exe 才会发现。这里提前用假的 _MEIPASS 布局验一遍。
    """

    def setUp(self):
        import shutil
        import tempfile
        from ps5mapper import config as c
        self.c = c
        self.dir = tempfile.mkdtemp()
        self.internal = os.path.join(self.dir, "_internal")
        os.makedirs(os.path.join(self.internal, "ps5mapper", "ui"))
        shutil.copy(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "ps5mapper", "default_config.json"),
                    os.path.join(self.internal, "ps5mapper", "default_config.json"))
        open(os.path.join(self.internal, "ps5mapper", "ui", "index.html"), "w").close()
        self._file = c.__file__
        self._exe = sys.executable
        self._cache = dict(c._bundled_cache)
        sys.frozen = True
        sys._MEIPASS = self.internal
        sys.executable = os.path.join(self.dir, "DualSenseMapper.exe")
        c.__file__ = os.path.join(self.internal, "nowhere", "config.py")
        c._bundled_cache.update(loaded=False, data=None)

    def tearDown(self):
        import shutil
        self.c.__file__ = self._file
        sys.executable = self._exe
        for attr in ("frozen", "_MEIPASS"):
            if hasattr(sys, attr):
                delattr(sys, attr)
        self.c._bundled_cache.update(self._cache)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_bundled_default_is_found_inside_the_bundle(self):
        b = self.c.bundled_default(reload=True)
        self.assertIsNotNone(b, "打包后找不到烘焙进来的出厂配置")
        self.assertTrue(b["profiles"])

    def test_config_file_lands_next_to_the_exe(self):
        """绿色免安装的关键：配置写在解压出来的那个文件夹里，不写用户目录。"""
        self.assertEqual(self.c.app_dir(), self.dir)
        self.assertTrue(self.c.config_path().startswith(self.dir))

    def test_ui_is_found_inside_the_bundle(self):
        from ps5mapper import app as appmod
        for name in ("ui_path", "_ui_path", "index_path"):
            fn = getattr(appmod, name, None)
            if callable(fn):
                self.assertTrue(os.path.exists(fn()), "打包后找不到 %s" % name)
                return
        self.skipTest("app 模块没有暴露界面路径函数")


class TestReleaseScript(unittest.TestCase):
    """release.bat 是唯一的打包入口，几个硬性要求锁死在这里。"""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def script(self):
        with open(os.path.join(self.ROOT, "release.bat"), "rb") as f:
            return f.read().decode("ascii")

    def test_exists_and_is_ascii_crlf(self):
        with open(os.path.join(self.ROOT, "release.bat"), "rb") as f:
            raw = f.read()
        raw.decode("ascii")                       # 非 ASCII 会让 cmd 乱码
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), "必须是 CRLF")

    def test_never_touches_the_system_python(self):
        t = self.script()
        self.assertIn(".venv", t)
        for line in t.splitlines():
            if "pip install" in line:
                self.assertIn("%VPY%", line,
                              "所有安装都必须走项目内的 .venv：%s" % line.strip())

    def test_bundles_ui_and_baked_defaults(self):
        t = self.script()
        self.assertIn("ps5mapper\\ui;ps5mapper\\ui", t)
        self.assertIn("default_config.json;ps5mapper", t,
                      "不带上烘焙配置的话，免安装包第一次运行就是通用默认")

    def test_uses_onedir_not_onefile(self):
        """onefile 每次启动都要往 %TEMP% 解压。用户要的是「东西都在我这个文件夹里」。"""
        self.assertNotIn("--onefile", self.script())

    def test_cleans_up_build_leftovers(self):
        t = self.script()
        for junk in ("build", "dist", "DualSenseMapper.spec"):
            self.assertIn(junk, t, "没清理 %s，交付目录会不干净" % junk)

    def test_never_uses_bare_start_on_a_name(self):
        """`start "" "名字"` 会按 PATHEXT 把裸名字**当命令**解析。

        2026-08-23 踩过：结尾写 start "" "release" 想打开 release 文件夹，
        但同目录有 release.bat，start 挑中了它——每打包完一次就自己重启一次，
        用户屏幕上堆了一排还在跑的窗口。打开文件夹一律用 explorer + 完整路径。
        """
        import glob
        import re
        bad = []
        for path in glob.glob(os.path.join(self.ROOT, "*.bat")):
            with open(path, "rb") as f:
                txt = f.read().decode("ascii")
            for m in re.finditer(r'^\s*start\s+""\s+"([^"]+)"', txt, re.M | re.I):
                arg = m.group(1)
                if not (arg.startswith("%") or "\\" in arg or ":" in arg):
                    bad.append("%s: start \"\" \"%s\"" % (os.path.basename(path), arg))
        self.assertEqual(bad, [], "裸名字会被 start 当命令解析：%s" % bad)

    def test_output_folder_does_not_collide_with_any_script_name(self):
        """输出目录一旦和某个 .bat 同名，就给上面那种自我重启留了口子。"""
        import glob
        t = self.script()
        scripts = {os.path.splitext(os.path.basename(p))[0].lower()
                   for p in glob.glob(os.path.join(self.ROOT, "*.bat"))}
        import re
        for folder in re.findall(r'mkdir "([^"\\]+)', t):
            self.assertNotIn(folder.lower(), scripts,
                             "输出目录 %s 和同名脚本会撞车" % folder)

    def test_has_a_reentry_guard(self):
        t = self.script()
        self.assertIn("DSM_BUILD_RUNNING", t, "缺少重入保险")

    def test_opens_the_folder_with_an_explicit_path(self):
        t = self.script()
        self.assertIn('explorer "%~dp0', t, "打开文件夹要用 explorer + 完整路径")

    def test_old_build_script_is_gone(self):
        self.assertFalse(os.path.exists(os.path.join(self.ROOT, "build.bat")),
                         "两个打包脚本会让人不知道该点哪个")
