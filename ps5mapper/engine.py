"""映射引擎：读手柄 → 判断动作 → 注入键鼠。

三个线程：
  * 输入线程：以 poll_hz 读 HID，做按键边缘检测和扳机判定
  * 指针线程：以 pointer_hz 推进鼠标移动 / 滚动 / 触摸板手势（连续量需要匀速积分）
  * 输出线程：逐帧算灯光并下发（灯条没有硬件动画，呼吸只能自己画）
"""
from __future__ import annotations

import math
import threading
import time
from typing import Optional

from . import config as cfgmod
from . import lights as lt
from . import triggers as trg
from . import winput as wi
from .dualsense import DualSense, ControllerState, TOUCH_MAX_X, TOUCH_MAX_Y
from .gyro import GyroPointer, tilt_angles


# ---------------------------------------------------------------- 动作执行

class ActionRunner:
    """负责把一个「动作」变成实际的键鼠事件，并记住按下状态以便正确松开。

    另外负责**长按连发**。这一点必须自己做：SendInput 注入的按键不会触发
    Windows 的自动重复——键盘之所以长按会连发，是键盘硬件在持续重发扫描码，
    系统不给注入事件生成重复。所以按住不放时要按设定频率补发 key-down。
    """

    def __init__(self, engine: "Engine"):
        self.engine = engine
        self._held = {}      # control_id -> (kind, payload)
        self._repeat = {}    # control_id -> {"combo": (mods, main), "next": 时间戳}

    def _repeat_wanted(self, act: dict) -> bool:
        cfg = self.engine.cfg
        if not cfg.get("key_repeat", True):
            return False
        want = act.get("repeat")
        return True if want is None else bool(want)

    def press(self, cid: str, act: dict):
        if not act or not act.get("type") or cid in self._held:
            return
        t = act["type"]
        if t == "combo":
            mods, main = wi.parse_combo(act.get("keys"))
            if main is None:
                return
            wi.combo_down(mods, main)
            self._held[cid] = ("combo", (mods, main))
            # 麦克风灯跟着语音输入走。绑在哪个键上都算数——按键是可自由映射的，
            # 认「动作是不是 Win+H」比认「是不是麦克风键」靠谱。
            # 注意这只反映你按过几次，Windows 语音输入实际在不在听我们无从得知。
            if lt.is_voice_input_combo(act.get("keys")):
                self.engine.voice_on = not self.engine.voice_on
            if self._repeat_wanted(act):
                delay = max(50, self.engine.cfg.get("key_repeat_delay", 400)) / 1000.0
                self._repeat[cid] = {"combo": (mods, main),
                                     "next": time.monotonic() + delay}
        elif t == "mouse":
            btn = act.get("button", "left")
            wi.mouse_button(btn, True)
            self._held[cid] = ("mouse", btn)
        elif t == "macro":
            self._held[cid] = ("macro", None)
            threading.Thread(target=self._run_macro, args=(act,), daemon=True).start()
        elif t == "builtin":
            self._builtin(cid, act.get("name"), True)

    def tick_repeat(self, now: float):
        """按住不放时补发 key-down，模拟键盘的连发。"""
        if not self._repeat:
            return
        rate = max(1.0, float(self.engine.cfg.get("key_repeat_rate", 25)))
        step = 1.0 / rate
        for cid, info in list(self._repeat.items()):
            if now < info["next"]:
                continue
            mods, main = info["combo"]
            fired = 0
            while now >= info["next"] and fired < 8:   # 卡顿后不要一次补一大串
                wi.combo_down(mods, main)
                info["next"] += step
                fired += 1
            if fired and info["next"] < now:
                info["next"] = now + step

    def release(self, cid: str, act: dict = None):
        self._repeat.pop(cid, None)
        held = self._held.pop(cid, None)
        if held is None:
            if act and act.get("type") == "builtin":
                self._builtin(cid, act.get("name"), False)
            return
        kind, payload = held
        if kind == "combo":
            mods, main = payload
            wi.combo_up(mods, main)
        elif kind == "mouse":
            wi.mouse_button(payload, False)
        elif kind == "builtin":
            # 少了这一支会漏掉「按住才生效」的内置功能。指针减速就是这么泄漏的：
            # 按一次 L3 之后 slow_pointer 再也没被改回 False，指针永久停在 35% 速度。
            self._builtin(cid, payload, False)

    def tap(self, cid: str, act: dict):
        self.press(cid + "#tap", act)
        self.release(cid + "#tap", act)

    def release_all(self):
        self._repeat.clear()
        for cid in list(self._held.keys()):
            self.release(cid)

    def _run_macro(self, act: dict):
        for step in act.get("steps", []):
            keys = step.get("keys")
            if keys:
                mods, main = wi.parse_combo(keys)
                if main is not None:
                    wi.tap_combo(mods, main)
            time.sleep(max(0, step.get("delay", 30)) / 1000.0)

    def _builtin(self, cid: str, name: str, down: bool):
        e = self.engine
        if name == "slow_pointer":
            e.slow_pointer = down
            if down:
                self._held[cid] = ("builtin", name)
            return
        if not down:
            return
        if name == "pause_toggle":
            e.toggle_pause()
        elif name == "profile_next":
            e.next_profile()
        elif name == "audio_switch":
            e.switch_audio_device()
        elif name == "double_click":
            wi.mouse_click("left")
            time.sleep(0.03)
            wi.mouse_click("left")
        elif name == "wheel_up":
            wi.wheel(1)
        elif name == "wheel_down":
            wi.wheel(-1)
        elif name == "toggle_lights":
            e.toggle_lights()


# ---------------------------------------------------------------- 触摸板

class TouchProcessor:
    """把两个触点的原始坐标翻译成指针移动 / 滚动 / 缩放 / 边缘滑条。

    实机上手指离开后，触点仍会有约三分之一的帧报告「按下」，坐标卡在上次
    触摸的位置不动（2026-08-23 实测：手完全拿开时 461/1251 帧误报）。
    所以进来的触点要先过两道滤网：
      * 去抖    —— 连续若干帧一致才改变按下/抬起状态
      * 幽灵过滤 —— 坐标一动不动超过一定时间就当它不存在，直到重新移动
    这两道尤其重要，否则单指滑动会被幽灵触点凑成「双指」，变成滚动。
    """

    DEBOUNCE_FRAMES = 3        # 250Hz 下约 12ms，人感觉不到
    GHOST_MS = 300             # 坐标完全不变超过这么久就认定是幽灵

    # -- 手势判定。单位是触摸板原始单位（板子 1920x1080），时间是毫秒。
    #    这些值来自 probe5 在这只手柄、这双手上的实测（2026-08-23），不是估的。
    #
    #    实测轻点 14 次：时长 78~156ms（中位 110），位移 5.1~71.1（中位 13.3）。
    #    手指离开后的「幽灵尾巴」只有 0~32ms，比预想的短得多，所以直接用
    #    原始时长判定就够了，不需要任何修正。
    TAP_MIN_MS = 40            # 比这还短的接触是噪声，不是手指（实测最短 78ms）
    TAP_MAX_MS = 200           # 超过就是按住不放（实测最长 156ms）
    TAP_MIN_MOVE = 3           # **防幽灵**：幽灵触点位移恒为 0，真手指最少也有 5.1
    TAP_MAX_MOVE = 90          # 实测最大 71.1（14 次里的孤例，90% 只有 30.5），留足余量
    DTAP_GAP_MS = 320          # 两次轻点之间的最大间隔
    DTAP_MAX_DIST = 240        # 第二次落点离第一次多远之内才算同一处

    # -- 下压防漂移
    #    实测 8 次下压，开关闭合前 100ms 手指滑了 2.2~114.9 个单位（中位 27）。
    #    阈值用**触摸板原始单位**而不是像素，这样改灵敏度不会把判定带偏。
    REWIND_MS = 100            # 往回看这么久的位移，正好覆盖实测窗口
    REWIND_MAX_UNITS = 150     # 超过这么多就不像压手指压出来的，是你真的在划
    FREEZE_MS = 55             # 按下瞬间锁住指针多久
    RELEASE_FREEZE_MS = 70     # 松开之后再锁一会儿——抬手同样会带一下
    GATE_UNIT = 12             # 「拖动门槛」每一档对应多少触摸板单位

    def __init__(self):
        self.reset()

    def reset(self):
        self.prev = {}          # ident -> (x, y)
        self.prev_dist = None
        self.edge_ident = None
        self.edge_accum = 0.0
        self.scroll_accum = 0.0
        self.hscroll_accum = 0.0
        self.zoom_accum = 0.0
        self.dx = 0.0
        self.dy = 0.0
        self._slots = [self._new_slot(), self._new_slot()]
        self._fresh = set()
        self._last_now = None
        # -- 手势 --
        self._ep = None              # 当前这一次接触（从落下到全部抬起）
        self._last_tap_t = 0.0
        self._last_tap_pos = None
        self._drag_held = False      # 双击拖动：左键正被我们按着
        # -- 防漂移 --
        self._hist = []              # [(时刻, dx, dy)]，用来倒回按下前那段漂移
        self._rewind = None
        self._freeze_until = 0.0
        self._gate_on = False
        self._gate_moved = 0.0
        self._click_down = False

    @staticmethod
    def _new_slot():
        return {"down": False, "on": 0, "off": 0,
                "x": None, "y": None, "still_since": None, "ghost": False}

    def _filter_points(self, state: ControllerState, now: float):
        """返回真正该被当成手指的触点，并把「本帧算作新触摸」的触点 id 记进 _fresh。

        算新触摸的有两种：刚走完去抖的，以及从幽灵状态复活的。这两种都必须
        丢掉旧坐标重新起算，否则指针会跳一大截。
        """
        real = []
        self._fresh = set()
        for slot, p in zip(self._slots, state.touch):
            if not p.active:
                slot["on"] = 0
                slot["off"] += 1
                if slot["off"] >= self.DEBOUNCE_FRAMES:
                    slot.update(down=False, ghost=False, still_since=None,
                                x=None, y=None)
                continue

            slot["off"] = 0
            slot["on"] += 1
            if not slot["down"]:
                if slot["on"] < self.DEBOUNCE_FRAMES:
                    continue                      # 还在去抖，先不认
                slot.update(down=True, ghost=False, x=p.x, y=p.y, still_since=now)
                self._fresh.add(p.ident)

            if (p.x, p.y) != (slot["x"], slot["y"]):
                if slot["ghost"]:
                    # 幽灵动起来了：可能是原来那根手指，也可能是新落下的一根，
                    # 分不清就当新的，宁可少走一帧也不要指针瞬移
                    self._fresh.add(p.ident)
                slot.update(x=p.x, y=p.y, still_since=now, ghost=False)
            elif slot["still_since"] is not None and \
                    (now - slot["still_since"]) * 1000.0 >= self.GHOST_MS:
                slot["ghost"] = True

            if not slot["ghost"]:
                real.append(p)
        return real

    # ---------------- 下压防漂移 ----------------
    #
    # 触摸板是整块压下去的按键，**开关是在按压行程的最后才闭合的**，
    # 而手指在这段行程里已经压扁、打滑了一两毫米。所以等收到「按下了」
    # 的时候光标早跑掉了，只靠「收到点击就冻住指针」根本来不及。
    # 笔记本的精准触摸板驱动是「事后反悔」：缓存最近几十毫秒的位移，
    # 收到点击就把那段倒回去，再在按住期间设一个拖动门槛。这里照做。

    def _push_hist(self, now: float, dx: float, dy: float, raw: float = 0.0):
        self._hist.append((now, dx, dy, raw))
        cut = now - (self.REWIND_MS * 2) / 1000.0
        while self._hist and self._hist[0][0] < cut:
            self._hist.pop(0)

    def _begin_click(self, now: float, tp: dict):
        """按下（物理下压或双击拖动开始）的那一刻要做的事。"""
        if not tp.get("click_stabilize", True):
            return
        mode = tp.get("rewind_mode", "smart")
        if mode != "off":
            since = now - self.REWIND_MS / 1000.0
            sx = sum(d[1] for d in self._hist if d[0] >= since)
            sy = sum(d[2] for d in self._hist if d[0] >= since)
            raw = sum(d[3] for d in self._hist if d[0] >= since)
            # smart：只倒回「又慢又短」的那种位移——看着就是压手指压出来的。
            # 你要是正快速划过去顺手点一下，那段位移是真的，不能动。
            # 判据用原始触摸板单位，不受灵敏度设置影响。
            if mode == "always" or raw <= self.REWIND_MAX_UNITS:
                if sx or sy:
                    self._rewind = (-sx, -sy)
        self._hist.clear()
        self._freeze_until = now + self.FREEZE_MS / 1000.0
        gate = max(0, int(tp.get("click_gate", 6)))
        self._gate_on = gate > 0
        self._gate_moved = 0.0

    def _end_click(self, now: float, tp: dict):
        """松开同样会带一下，所以再锁一小会儿。"""
        if not tp.get("click_stabilize", True):
            return
        self._freeze_until = max(self._freeze_until,
                                 now + self.RELEASE_FREEZE_MS / 1000.0)
        self._gate_on = False
        self._gate_moved = 0.0

    def _gate_pointer(self, now: float, tp: dict, ddx, ddy, raw_move):
        """返回过滤后的指针位移。冻结期和门槛期一律吞掉。"""
        if now < self._freeze_until:
            return 0.0, 0.0
        if self._gate_on:
            self._gate_moved += raw_move
            need = max(0, int(tp.get("click_gate", 6))) * self.GATE_UNIT
            if self._gate_moved < need:
                return 0.0, 0.0
            self._gate_on = False          # 过了门槛，从此放行，开始拖
        return ddx, ddy

    def _take_rewind(self):
        r, self._rewind = self._rewind, None
        return r

    # ---------------- 轻点 / 双击拖动 / 双指轻点 ----------------
    #
    # 一次「接触」= 从第一根手指落下，到全部抬起。判定全在接触结束时做，
    # 所以不会给每次点击平白加延迟。
    #
    # 【实测推翻过一个设计】原本打算用「坐标冻住 = 已抬手」来剥掉幽灵尾巴。
    # probe5 实测证明这条路是错的：**静止的真手指坐标同样是逐帧完全相同的**，
    # 4 秒里 2116 帧有 1931 帧和上一帧一模一样，最长连续 320ms 纹丝不动。
    # 照那个规则做，手指往板子上一放就会被判成「抬手了」，直接误发一次点击。
    # 好在同一份实测也说明根本不需要这个修正：轻点的幽灵尾巴只有 0~32ms，
    # 直接用原始时长判定就够了。
    #
    # 防幽灵改用**位移下限**：幽灵触点的坐标是卡死的，位移恒为 0；
    # 真手指再轻的一点也有 5 个单位以上的抖动。空闲时约 24% 的帧在误报有触点，
    # 没有这条下限，光放着不动的手柄自己就会乱点。

    def _gesture(self, state: ControllerState, tp: dict, now: float):
        down = [s for s in self._slots if s["down"]]
        n = len(down)

        if n and self._ep is None:
            s0 = down[0]
            gap = (now - self._last_tap_t) * 1000.0
            near = (self._last_tap_pos is not None and
                    math.hypot(s0["x"] - self._last_tap_pos[0],
                               s0["y"] - self._last_tap_pos[1]) <= self.DTAP_MAX_DIST)
            self._ep = {
                "t0": now, "pos": (s0["x"], s0["y"]), "max_n": n,
                "move": 0.0, "dragging": False,
                # 紧接着上一次轻点、且落在同一处 —— 这一下有可能是「双击拖动」
                "dtap": bool(tp.get("tap_drag", True) and
                             gap <= self.DTAP_GAP_MS and near),
            }
            return

        if self._ep is None:
            return
        ep = self._ep

        if n:
            ep["max_n"] = max(ep["max_n"], n)
            s0 = down[0]
            if s0["x"] is not None:
                ep["move"] = max(ep["move"],
                                 math.hypot(s0["x"] - ep["pos"][0],
                                            s0["y"] - ep["pos"][1]))
            held = (now - ep["t0"]) * 1000.0
            if (ep["dtap"] and not ep["dragging"] and held > self.TAP_MAX_MS
                    and ep["max_n"] == 1 and tp.get("tap_to_click", True)):
                ep["dragging"] = True                 # 轻点一下再按住 = 拖动
                self._drag_held = True
                wi.mouse_button("left", True)
                self._begin_click(now, tp)
            return

        # -- 接触结束 --
        self._ep = None
        if ep["dragging"]:
            wi.mouse_button("left", False)
            self._drag_held = False
            self._end_click(now, tp)
            self._last_tap_t = 0.0
            return

        dur = (now - ep["t0"]) * 1000.0
        if dur > self.TAP_MAX_MS or ep["move"] > self.TAP_MAX_MOVE:
            self._last_tap_t = 0.0                    # 按住不放 / 划过去，不是轻点
            return
        if dur < self.TAP_MIN_MS or ep["move"] < self.TAP_MIN_MOVE:
            return                                    # 幽灵：位移恒为 0，真手指不会

        if ep["max_n"] >= 2:
            if tp.get("two_finger_tap_right", True):
                wi.mouse_click("right")
            self._last_tap_t = 0.0
            return
        if not tp.get("tap_to_click", True):
            return
        wi.mouse_click("left")
        self._last_tap_t = now
        self._last_tap_pos = ep["pos"]

    def _last_pos(self, ident):
        """本帧算新触摸的话，就当没有历史位置。"""
        return None if ident in self._fresh else self.prev.get(ident)

    def update(self, state: ControllerState, tp: dict, now: float = None):
        """返回 (指针 dx, 指针 dy)；滚动 / 缩放 / 音量直接在内部发出。"""
        self.dx = self.dy = 0.0
        if now is None:
            now = time.monotonic()
        pts = self._filter_points(state, now)
        prev_now, self._last_now = self._last_now, now

        # 物理下压的边沿：防漂移必须在这里挂上，不管这个键绑的是不是鼠标左键
        # ——按下去带一下这件事和绑什么无关
        clicked = bool(state.pressed("touchpad_click"))
        if clicked != self._click_down:
            self._click_down = clicked
            (self._begin_click if clicked else self._end_click)(now, tp)

        self._gesture(state, tp, now)

        rew = self._take_rewind()
        if rew:
            return rew

        if not pts:
            # 只有确实全部抬起时才清历史。滑动中偶尔掉一帧（这只手柄很常见）
            # 如果也清掉，下一帧就会被当成新触摸，指针会顿一下。
            if not any(s["down"] for s in self._slots):
                self.prev.clear()
                self.prev_dist = None
                self.edge_ident = None
                self.edge_accum = 0.0
            return 0.0, 0.0

        if len(pts) == 1:
            self.prev_dist = None
            p = pts[0]
            last = self._last_pos(p.ident)
            self.prev = {p.ident: (p.x, p.y)}

            # 边缘滑条：只有「手指从边缘落下」才算，中途滑过去不算
            if tp.get("edge_slider") and last is None:
                band = TOUCH_MAX_X * max(1, tp.get("edge_width", 12)) / 100.0
                on_right = tp.get("edge_side", "right") == "right" and p.x >= TOUCH_MAX_X - band
                on_left = tp.get("edge_side") == "left" and p.x <= band
                self.edge_ident = p.ident if (on_right or on_left) else None
                self.edge_accum = 0.0

            if self.edge_ident == p.ident:
                if last is not None:
                    self.edge_accum += (last[1] - p.y) / 40.0     # 向上滑 = 加
                    while abs(self.edge_accum) >= 1.0:
                        step = 1 if self.edge_accum > 0 else -1
                        self._edge_step(tp, step)
                        self.edge_accum -= step
                return 0.0, 0.0

            if tp.get("pointer_enabled", True) and last is not None:
                sens = max(1, tp.get("sensitivity", 6)) / 6.0
                ddx = (p.x - last[0]) * 0.55 * sens
                ddy = (p.y - last[1]) * 0.55 * sens
                # 加速要按「速度」算，不能按每帧位移算：上报速率从 64Hz 提到
                # 250Hz 之后每帧位移只有原来的四分之一，按位移判断就再也触发不了了
                dt = (now - prev_now) if prev_now else 0.0
                if tp.get("acceleration", True) and dt > 0:
                    speed = math.hypot(p.x - last[0], p.y - last[1]) / dt
                    if speed > 1200:                      # 触摸板单位/秒
                        boost = min(2.6, 1.0 + (speed - 1200) / 2600.0)
                        ddx *= boost
                        ddy *= boost
                raw = math.hypot(p.x - last[0], p.y - last[1])
                ddx, ddy = self._gate_pointer(now, tp, ddx, ddy, raw)
                self._push_hist(now, ddx, ddy, raw)
                self.dx, self.dy = ddx, ddy
            return self.dx, self.dy

        # 两指
        self.edge_ident = None
        a, b = pts[0], pts[1]
        dist = math.hypot(a.x - b.x, a.y - b.y)
        la = self._last_pos(a.ident)
        lb = self._last_pos(b.ident)
        self.prev = {a.ident: (a.x, a.y), b.ident: (b.x, b.y)}

        if la is None or lb is None or self.prev_dist is None:
            self.prev_dist = dist
            return 0.0, 0.0

        move_x = ((a.x - la[0]) + (b.x - lb[0])) / 2.0
        move_y = ((a.y - la[1]) + (b.y - lb[1])) / 2.0
        d_dist = dist - self.prev_dist
        self.prev_dist = dist

        # 同向移动 = 滚动；间距变化 = 缩放。取变化更明显的那个
        if tp.get("pinch_zoom") and abs(d_dist) > abs(move_y) * 1.4 and abs(d_dist) > 3:
            self.zoom_accum += d_dist / 90.0
            while abs(self.zoom_accum) >= 1.0:
                step = 1 if self.zoom_accum > 0 else -1
                mods, main = wi.parse_combo("Ctrl")
                wi.key_down(main)
                wi.wheel(step)
                wi.key_up(main)
                self.zoom_accum -= step
            return 0.0, 0.0

        if tp.get("two_finger_scroll", True):
            speed = max(1, tp.get("scroll_speed", 5)) / 5.0
            # 上下和左右是两套独立的方向设置。以前左右**根本没有**方向选项——
            # sign 只作用在纵向上，横向的符号是写死的，想反过来都没办法。
            sign = -1 if tp.get("natural_scroll", True) else 1
            hsign = -1 if tp.get("natural_hscroll", tp.get("natural_scroll", True)) else 1
            self.scroll_accum += sign * move_y / 28.0 * speed
            self.hscroll_accum += hsign * move_x / 28.0 * speed
            while abs(self.scroll_accum) >= 1.0:
                step = 1 if self.scroll_accum > 0 else -1
                wi.wheel(step)
                self.scroll_accum -= step
            while abs(self.hscroll_accum) >= 1.0:
                step = 1 if self.hscroll_accum > 0 else -1
                wi.hwheel(step)
                self.hscroll_accum -= step
        return 0.0, 0.0

    @staticmethod
    def _edge_step(tp: dict, step: int):
        target = tp.get("edge_target", "volume")
        if target == "volume":
            wi.tap_combo([], 0xAF if step > 0 else 0xAE)
        elif target == "scroll":
            wi.wheel(step)
        else:                                   # 亮度没有通用快捷键，退回滚动
            wi.wheel(step)


# ---------------------------------------------------------------- 引擎

class Engine:
    def __init__(self, cfg: dict, on_event=None):
        self.cfg = cfg
        self.on_event = on_event or (lambda *_: None)
        self.dev = DualSense()
        self.runner = ActionRunner(self)
        self.touch = TouchProcessor()
        self.gyro = GyroPointer()
        self._gyro_latched = False
        self._gyro_abs_ref = None      # 绝对模式的基准俯仰角

        self.state: Optional[ControllerState] = None
        self.paused = False
        self.slow_pointer = False
        self.running = False

        self._prev_buttons = {}
        self._combo_latched = False
        self._trigger_state = {"l2": {"s1": False, "s2": False},
                               "r2": {"s1": False, "s2": False}}
        self._move_x = 0.0
        self._move_y = 0.0
        self._pending_dx = 0.0
        self._pending_dy = 0.0
        self._scroll = 0.0
        self._hscroll = 0.0
        self._last_output = None
        self._last_output_at = 0.0
        self._flash_color = None
        self._flash_until = 0.0
        self.voice_on = False
        self._mic_source_warned = False
        self._output_error_shown = False
        self._lock = threading.Lock()
        self._threads = []

    # -- 生命周期 -----------------------------------------------------
    def start(self):
        if self.running:
            return
        self.running = True
        wi.begin_high_res_timer()      # 没有这句，两个循环都会被压到 64Hz
        self._threads = [
            threading.Thread(target=self._input_loop, daemon=True),
            threading.Thread(target=self._pointer_loop, daemon=True),
            threading.Thread(target=self._output_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        self.running = False
        time.sleep(0.05)
        wi.end_high_res_timer()
        self.runner.release_all()
        try:
            self.dev.send_neutral()
        except Exception:
            pass
        self.dev.close()

    # -- 状态 --------------------------------------------------------
    @property
    def profile(self) -> dict:
        return cfgmod.active(self.cfg)

    def toggle_pause(self):
        self.paused = not self.paused
        self.runner.release_all()
        self._last_output = None
        self.on_event("pause", self.paused)

    def next_profile(self):
        n = len(self.cfg["profiles"])
        if n <= 1:
            return
        self.cfg["active_profile"] = (self.cfg.get("active_profile", 0) + 1) % n
        self.runner.release_all()
        self.touch.reset()
        self._begin_profile_flash()
        self.on_event("profile", self.cfg["active_profile"])

    def set_profile(self, index: int):
        if 0 <= index < len(self.cfg["profiles"]):
            self.cfg["active_profile"] = index
            self.runner.release_all()
            self.touch.reset()
            self._begin_profile_flash()
            self.on_event("profile", index)

    def _begin_profile_flash(self):
        """切档后让灯条亮一会儿这个档的提示色，到点自己回到常态灯效。"""
        lights = self.cfg.get("lights") or {}
        self._last_output = None
        if not lights.get("profile_flash", True):
            self._flash_until = 0.0
            return
        secs = max(0.0, float(lights.get("profile_flash_secs", 3.0)))
        self._flash_color = tuple(self.profile.get("lightbar", (40, 110, 255)))
        self._flash_until = time.monotonic() + secs

    def toggle_lights(self):
        lights = self.cfg.setdefault("lights", lt.default_lights())
        lights["enabled"] = not lights.get("enabled", True)
        self._last_output = None
        self.on_event("lights", lights["enabled"])

    def switch_audio_device(self):
        try:
            from . import audio
            audio.cycle_input_device()
            self.on_event("audio", audio.current_input_name())
        except Exception as exc:
            self.on_event("error", f"切换录音设备失败：{exc}")

    def snapshot(self) -> dict:
        """给 UI 用的实时状态。"""
        st = self.state
        return {
            "connected": self.dev.connected,
            "bluetooth": self.dev.bluetooth,
            "paused": self.paused,
            "battery": st.battery_percent if st else None,
            "charging": st.charging if st else False,
            "buttons": dict(st.buttons) if st else {},
            "sticks": {"lx": st.lx, "ly": st.ly, "rx": st.rx, "ry": st.ry} if st else {},
            "triggers": {"l2": st.l2, "r2": st.r2} if st else {"l2": 0, "r2": 0},
            "touch": [
                {"active": p.active, "x": p.x, "y": p.y} for p in (st.touch if st else ())
            ],
            "profile": self.cfg.get("active_profile", 0),
            # 界面上实时显示角速度，用户照着自己的手抖调压制阈值——
            # probe10 量到的 0.37 度/秒是手柄放在桌上时的传感器本底，
            # 不是握在手里的抖动，所以这个值只能自己看着定。
            "gyro": {
                "on": bool((self.profile.get("gyro") or {}).get("enabled")),
                "calibrated": self.gyro.calibrated,
                "dps": [round(v, 2) for v in self.gyro.live_dps],
                "tick_us": round(self.gyro.ticks.us_per_tick, 4),
                "tick_measured": self.gyro.ticks.measured,
                "bias": [round(v, 1) for v in self.gyro._bias],
                "recoveries": self.gyro.stuck_recoveries,
                "static": self.gyro.static,
                "tilt_dps": round(self.gyro.tilt_dps, 2),
            },
        }

    # -- 输入线程 -----------------------------------------------------
    def _input_loop(self):
        """阻塞读取，不用 sleep 掐时间。

        Windows 的 time.sleep 精度只有 15.6ms，原来「读一次 + sleep(4ms)」实际
        只能跑到 64Hz，触摸板和指针都明显发卡。阻塞读取由 hidapi 在有数据时
        立刻返回，节奏完全跟着手柄的原生上报速率。
        """
        while self.running:
            if not self.dev.connected:
                if not self._try_connect():
                    time.sleep(1.0)
                    continue
                self.dev.set_blocking(True)
            st = self.dev.read_state(timeout_ms=50)
            if st is None:
                continue
            self.state = st
            if not self.paused:
                self._handle_buttons(st)
                self._handle_triggers(st)
                self._feed_touch(st)
                self._feed_gyro(st)
            else:
                self._handle_pause_combo(st)

    def _feed_gyro(self, st: ControllerState):
        """陀螺仪按每一帧积分。

        上报速率实测 586Hz，比指针线程快一倍多。所以这里只累加角度，
        鼠标事件交给指针线程整批发——既不丢转动，也不会变成 586Hz 的
        事件风暴。和触摸板那边是同一个套路。
        """
        g = self.profile.get("gyro") or {}
        if not g.get("enabled"):
            if self.gyro.enabled:
                self.gyro.enabled = False
                self.gyro.clear()
            return
        self._sync_gyro_settings(g)
        self.gyro.feed(st.gyro, st.sensor_timestamp, st.accel)

    def _sync_gyro_settings(self, g: dict):
        gp = self.gyro
        if gp.scale_source is not self.dev.calib:
            gp.set_calibration(self.dev.calib)
        gp.enabled = True
        gp.pixels_per_degree = float(g.get("sensitivity", 25.0))
        # 锁定时垂直跟着水平走，免得斜向方向被两个不等的灵敏度拧歪
        gp.vertical_pixels_per_degree = (
            gp.pixels_per_degree if g.get("link_sensitivity", True)
            else float(g.get("vertical_sensitivity", 25.0)))
        gp.world = g.get("space", "world") != "local"
        gp.tremor_dps = float(g.get("tremor", 1.5))
        gp.smoothing = int(g.get("smoothing", 4))
        gp.accel = int(g.get("accel", 60))
        gp.invert_x = bool(g.get("invert_x"))
        gp.invert_y = bool(g.get("invert_y"))
        gp.vertical_relative = g.get("vertical_mode", "relative") != "absolute"

    # -- 陀螺仪开关组合键 ---------------------------------------------
    def _gyro_combo_active(self, st: ControllerState) -> bool:
        combo = (self.profile.get("gyro") or {}).get("toggle_combo") or []
        return bool(combo) and all(st.pressed(c) for c in combo)

    # 扳机当组合键用的两个门槛。
    # L2/R2 是模拟量，不在 st.buttons 里，所以要自己定「算按下了没」。
    TRIGGER_COMBO_DEPTH = 0.90     # 按到底才算组合的一员
    TRIGGER_COMBO_ENGAGE = 0.25    # 两个都过了这条线，就认为你在做组合动作

    def _combo_pressed(self, st: ControllerState, cid: str) -> bool:
        """组合键专用的「按下了没」。普通按键查 buttons，扳机按深度判断。"""
        if cid == "l2":
            return st.l2 >= self.TRIGGER_COMBO_DEPTH
        if cid == "r2":
            return st.r2 >= self.TRIGGER_COMBO_DEPTH
        return st.pressed(cid)

    def _trigger_combo_engaged(self, st: ControllerState) -> bool:
        """组合键里同时含 L2 和 R2，而且两个都按下去了。

        这时要把两个扳机各自的绑定**整个屏蔽掉**。否则会很难受：L2 默认是
        鼠标右键、R2 是左键，你为了切换陀螺仪捏一下两个扳机，就先在屏幕上
        左右各点了一下——可能正好点开一个链接。

        门槛故意定得很浅（25%）：正常用的时候几乎不会两个扳机同时轻按着，
        所以这条规则不会误伤，也不需要靠延时去分辨，没有任何额外延迟。
        """
        combo = (self.profile.get("gyro") or {}).get("toggle_combo") or []
        if "l2" not in combo or "r2" not in combo:
            return False
        return (st.l2 >= self.TRIGGER_COMBO_ENGAGE
                and st.r2 >= self.TRIGGER_COMBO_ENGAGE)

    def _handle_gyro_combo(self, st: ControllerState) -> bool:
        """返回 True 表示这一帧被组合键吃掉了，别再走普通按键映射。"""
        combo = (self.profile.get("gyro") or {}).get("toggle_combo") or []
        if not combo:
            self._gyro_latched = False
            return False
        if all(self._combo_pressed(st, c) for c in combo):
            if not self._gyro_latched:
                self._gyro_latched = True
                self.runner.release_all()
                self._prev_buttons = dict(st.buttons)
                self.toggle_gyro()
            return True
        if self._gyro_latched:
            if not any(self._combo_pressed(st, c) for c in combo):
                self._gyro_latched = False
                self._prev_buttons = dict(st.buttons)
            return True
        return False

    def toggle_gyro(self):
        g = self.profile.setdefault("gyro", cfgmod.default_gyro())
        g["enabled"] = not g.get("enabled", False)
        if g["enabled"]:
            # 每次打开都重新认零点和基准姿态，否则会带着上次的漂移开始
            self.gyro.reset_zero()
            self._gyro_abs_ref = None
        self.gyro.clear()
        self._last_output = None
        self.on_event("gyro", g["enabled"])

    def _feed_touch(self, st: ControllerState):
        """触摸板必须按每一帧算，才对得起 250Hz 的上报速率。"""
        tdx, tdy = self.touch.update(st, self.profile.get("touchpad", {}))
        if tdx or tdy:
            with self._lock:
                self._pending_dx += tdx
                self._pending_dy += tdy

    def _try_connect(self) -> bool:
        try:
            ok = self.dev.open()
        except Exception as exc:
            self.on_event("error", str(exc))
            return False
        if ok:
            self.touch.reset()
            self._prev_buttons = {}
            self._last_output = None
            # 必须在发任何颜色之前把灯条从固件手里抢过来，否则手柄不理会
            try:
                self.dev.reset_leds()
            except Exception as exc:
                self.on_event("error", "重置灯条失败：%s" % exc)
            self.on_event("connected", {"bluetooth": self.dev.bluetooth})
        return ok

    # -- 按键 --------------------------------------------------------
    def _pause_combo_active(self, st: ControllerState) -> bool:
        combo = self.cfg.get("pause_combo") or []
        return bool(combo) and all(st.pressed(c) for c in combo)

    def _handle_pause_combo(self, st: ControllerState):
        """暂停状态下只关心「再按一次组合恢复」。"""
        active = self._pause_combo_active(st)
        if active and not self._combo_latched:
            self._combo_latched = True
            self.toggle_pause()
        elif not active:
            combo = self.cfg.get("pause_combo") or []
            if not any(st.pressed(c) for c in combo):
                self._combo_latched = False

    def _handle_buttons(self, st: ControllerState):
        combo = self.cfg.get("pause_combo") or []
        if self._pause_combo_active(st):
            if not self._combo_latched:
                self._combo_latched = True
                self.runner.release_all()
                self._prev_buttons = dict(st.buttons)
                self.toggle_pause()
            return
        if self._combo_latched:
            # 组合松开一部分之前不恢复普通映射，避免误触发单键
            if not any(st.pressed(c) for c in combo):
                self._combo_latched = False
                self._prev_buttons = dict(st.buttons)
            return

        # 陀螺仪开关也是组合键，和暂停一样的处理：按成组合时不走普通映射，
        # 否则组合里的键会同时触发它们自己绑定的动作。
        if self._handle_gyro_combo(st):
            return

        binds = self.profile.get("buttons", {})
        for cid, now in st.buttons.items():
            before = self._prev_buttons.get(cid, False)
            if now == before:
                continue
            act = binds.get(cid)
            if now:
                self.runner.press(cid, act)
            else:
                self.runner.release(cid, act)
        self._prev_buttons = dict(st.buttons)

    # -- 扳机 --------------------------------------------------------
    def _handle_triggers(self, st: ControllerState):
        # 两个扳机一起按着 = 你在按陀螺仪的组合键，不是在点鼠标。
        # 把已经按下的先松开，免得留一个卡住的左键。
        if self._trigger_combo_engaged(st):
            for tid in ("l2", "r2"):
                ts = self._trigger_state[tid]
                if ts["s1"]:
                    ts["s1"] = False
                    self.runner.release(tid, (self.profile.get("triggers", {})
                                              .get(tid) or {}).get("binding"))
                ts["s2"] = False
            return
        binds = self.profile.get("triggers", {})
        for tid, value in (("l2", st.l2), ("r2", st.r2)):
            cfg = binds.get(tid) or {}
            ts = self._trigger_state[tid]
            pct = value * 100.0
            # 触发点和阻力墙的位置是两回事：墙从 depth 开始，但要按穿整道墙
            # 才算「按下去了」。fire_percent 是唯一事实来源。
            wall_active = bool(self.cfg.get("adaptive_triggers_master", True))
            d1 = trg.fire_percent(cfg, 1, wall_active)
            hyst = min(cfg.get("hysteresis", 38), d1 - 3)
            dual = cfg.get("mode") == "dual"
            d2 = max(d1 + 5, trg.fire_percent(cfg, 2, wall_active))

            if not ts["s1"] and pct >= d1:
                ts["s1"] = True
                self.runner.press(tid, cfg.get("binding"))
                if cfg.get("haptic"):
                    self._pulse()
            elif ts["s1"] and pct <= hyst:
                ts["s1"] = False
                self.runner.release(tid, cfg.get("binding"))

            if dual:
                if not ts["s2"] and pct >= d2:
                    ts["s2"] = True
                    self.runner.tap(tid + "2", cfg.get("binding2"))
                elif ts["s2"] and pct <= d2 - 10:
                    ts["s2"] = False

    def _mic_on(self, lcfg: dict) -> bool:
        """麦克风灯该不该亮。

        默认读 Windows 真实的麦克风占用状态——只数按键的话，Win+H 静音几秒
        自己停掉之后程序还以为在录，下一次按键的开关方向就整个反了。
        系统状态读不出来（返回 None）时才退回按键计数，并且只提示一次。
        """
        if lcfg.get("mic_led_source", "system") != "system":
            return self.voice_on
        from . import audio
        live = audio.mic_in_use()
        if live is None:
            if not self._mic_source_warned:
                self._mic_source_warned = True
                self.on_event("error", "读不到系统麦克风状态，麦克风灯已退回按键计数")
            return self.voice_on
        return live

    def _pulse(self):
        threading.Thread(target=self._pulse_worker, daemon=True).start()

    def _pulse_worker(self):
        from .dualsense import build_output_common
        prof = self.profile
        self.dev.send(build_output_common(rumble_left=90, rumble_right=60,
                                          lightbar=tuple(prof.get("lightbar", (40, 110, 255)))))
        time.sleep(0.05)
        self._last_output = None      # 让输出线程下一帧复位震动

    # -- 输出线程（灯条 / 玩家灯 / 麦克风灯 / 扳机） --------------------
    ANIM_HZ = 30       # 呼吸这类慢变化 30Hz 足够顺滑，比 60Hz 省一半带宽
    IDLE_HZ = 4        # 画面静止时降下来，别为一个不动的颜色占蓝牙带宽

    def _output_loop(self):
        """灯效必须逐帧下发——手柄的灯条没有硬件动画，呼吸得我们自己画出来。

        但静止的画面没理由高频发包：常亮且没在闪的时候降到 4Hz，
        再加上 _refresh_output 里的去重，实际上几乎不发东西。
        """
        timer = wi.make_precise_timer()
        try:
            while self.running:
                try:
                    animating = self._refresh_output()
                except Exception as exc:
                    # 不许静默吞掉。上一版这里是个光秃秃的 except，
                    # 结果输出坏掉时界面上一点提示都没有，只能靠用户发现灯不动。
                    animating = False
                    if not self._output_error_shown:
                        self._output_error_shown = True
                        self.on_event("error", "灯光输出出错：%r" % (exc,))
                wi.precise_sleep(timer, 1.0 / (self.ANIM_HZ if animating else self.IDLE_HZ))
        finally:
            wi.close_precise_timer(timer)

    def _refresh_output(self) -> bool:
        """算并下发一帧输出，返回「是否还需要高频刷新」。"""
        from .dualsense import build_output_common
        prof = self.profile
        lcfg = self.cfg.get("lights") or lt.default_lights()
        master = self.cfg.get("adaptive_triggers_master", True)
        now = time.monotonic()

        st = self.state
        battery = getattr(st, "battery_percent", None) if st else None
        charging = bool(getattr(st, "charging", False)) if st else False
        # pause_dims_lightbar 现在的含义是「暂停时要不要让灯条变红」
        paused_shown = self.paused and self.cfg.get("pause_dims_lightbar", True)
        flashing = self._flash_color is not None and now < self._flash_until

        vis = lt.resolve(lcfg, now, paused=paused_shown,
                         flash_color=self._flash_color,
                         flash_until=self._flash_until,
                         battery=battery, charging=charging,
                         mic_on=self._mic_on(lcfg),
                         profile_color=prof.get("lightbar"))

        if self.paused or not master:
            left = right = trg.off()
        else:
            tcfg = prof.get("triggers", {})
            left = trg.build_from_config(tcfg.get("l2"))
            right = trg.build_from_config(tcfg.get("r2"))

        animating = lt.needs_animation(lcfg, paused=paused_shown,
                                       flashing=flashing, charging=charging)
        sig = (vis["lightbar"], vis["player_leds"], vis["mic_led"], left, right)
        wall = time.time()
        if sig == self._last_output and wall - self._last_output_at < 2.0:
            return animating
        self._last_output = sig
        self._last_output_at = wall
        self.dev.send(build_output_common(
            lightbar=vis["lightbar"], player_leds=vis["player_leds"],
            mic_led=vis["mic_led"], left_trigger=left, right_trigger=right))
        return animating

    # -- 指针线程 -----------------------------------------------------
    def _pointer_loop(self):
        """指针积分必须用**实测**的时间增量，不能用标称周期。

        用标称周期的话，循环一旦跑不满设定频率，指针就会成比例变慢。而这在
        Windows 上很容易发生：程序不在前台时，系统会忽略它抬高定时器精度的请求，
        sleep 精度掉回 15.6ms，240Hz 直接变成 64Hz。
        用实测 dt 之后，同样的摇杆推程无论循环多快都走同样的距离。
        """
        hz = max(60, self.cfg.get("pointer_hz", 240))
        period = 1.0 / hz
        timer = wi.make_precise_timer()      # 高精度可等待定时器，不受前台状态影响
        last = time.perf_counter()
        while self.running:
            t0 = time.perf_counter()
            dt = min(t0 - last, 0.1)         # 卡顿之后不要一次冲出一大段
            last = t0
            if not self.paused:
                self.runner.tick_repeat(time.monotonic())
            st = self.state
            if st is not None and not self.paused:
                self._tick_pointer(st, dt)
            wi.precise_sleep(timer, period - (time.perf_counter() - t0))
        wi.close_precise_timer(timer)

    def _tick_pointer(self, st: ControllerState, dt: float):
        prof = self.profile
        sticks = prof.get("sticks", {})
        slow = 0.35 if self.slow_pointer else 1.0

        dx = dy = 0.0
        for name, (ax, ay) in (("left", (st.lx, st.ly)), ("right", (st.rx, st.ry))):
            s = sticks.get(name) or {}
            mode = s.get("mode", "off")
            if mode == "off":
                continue
            mx, my = self._apply_curve(ax, ay, s)
            if mode == "mouse":
                pps = max(1, s.get("speed", 12)) * 120.0 * slow
                dx += mx * pps * dt
                dy += my * pps * dt
            elif mode == "scroll":
                sp = max(1, s.get("speed", 12)) * 1.1
                # 摇杆当滚轮用时同样是两套方向，毛病和触摸板一模一样
                sign = -1 if s.get("invert_scroll") else 1
                hsign = -1 if s.get("invert_hscroll", s.get("invert_scroll")) else 1
                self._scroll += sign * -my * sp * dt
                if s.get("horizontal_scroll", True):
                    self._hscroll += hsign * mx * sp * dt
            elif mode == "dpad":
                self._stick_as_dpad(name, mx, my)

        with self._lock:                       # 触摸板的位移在输入线程里攒着
            dx += self._pending_dx * slow
            dy += self._pending_dy * slow
            self._pending_dx = 0.0
            self._pending_dy = 0.0

        # 陀螺仪同样在输入线程里攒着（586Hz），这里整批取走。
        # 和摇杆、触摸板是**相加**关系：摇杆负责大范围，陀螺仪负责精调。
        gdx, gdy = self._take_gyro(st, prof, slow)
        dx += gdx
        dy += gdy

        self._move_x += dx
        self._move_y += dy
        ix, iy = int(self._move_x), int(self._move_y)
        if ix or iy:
            self._move_x -= ix
            self._move_y -= iy
            wi.mouse_move(ix, iy)

        while abs(self._scroll) >= 1.0:
            step = 1 if self._scroll > 0 else -1
            wi.wheel(step)
            self._scroll -= step
        while abs(self._hscroll) >= 1.0:
            step = 1 if self._hscroll > 0 else -1
            wi.hwheel(step)
            self._hscroll -= step

    def _take_gyro(self, st: ControllerState, prof: dict, slow: float):
        g = prof.get("gyro") or {}
        if not g.get("enabled"):
            self._gyro_abs_ref = None
            return 0.0, 0.0
        gx, gy = self.gyro.take()
        dx = gx * slow
        if g.get("vertical_mode", "relative") != "absolute":
            return dx, gy * slow
        return dx, self._absolute_vertical(st, g)

    def _absolute_vertical(self, st: ControllerState, g: dict) -> float:
        """绝对模式：手柄的俯仰角直接决定光标在屏幕上的高度。

        能这么做是因为加速度计感知得到重力，俯仰角有绝对参照，不会漂。
        偏航没有这个待遇——绕垂直轴转的时候重力方向不变，所以水平方向
        只能用相对模式。

        第一帧记下当前姿态当基准，这样开启的瞬间光标不会跳到别处去。
        """
        angles = tilt_angles(st.accel, self.dev.calib)
        if angles is None:               # 正在被甩动，重力方向不可信
            return 0.0
        pitch = angles[0]
        if self._gyro_abs_ref is None:
            pos = wi.cursor_pos()
            h = wi.screen_size()[1]
            frac = (pos[1] / h) if (pos and h) else 0.5
            self._gyro_abs_ref = (pitch, frac)
            return 0.0
        ref_pitch, ref_frac = self._gyro_abs_ref
        rng = max(5.0, float(g.get("absolute_range", 35.0)))
        h = wi.screen_size()[1]
        delta = (pitch - ref_pitch) / rng          # -1..1
        if g.get("invert_y"):
            delta = -delta
        target = (ref_frac + delta) * h
        target = max(0.0, min(h - 1.0, target))
        pos = wi.cursor_pos()
        return (target - pos[1]) if pos else 0.0

    @staticmethod
    def _apply_curve(ax: float, ay: float, s: dict):
        """径向死区 + 指数加速曲线。返回 -1..1 的 (x, y)。"""
        dz = max(0.0, min(0.9, s.get("deadzone", 11) / 100.0))
        mag = math.hypot(ax, ay)
        if mag <= dz:
            return 0.0, 0.0
        norm = min(1.0, (mag - dz) / (1.0 - dz))
        curve = max(1.0, float(s.get("curve", 2.0)))
        scaled = norm ** curve
        return ax / mag * scaled, ay / mag * scaled

    def _stick_as_dpad(self, name: str, mx: float, my: float):
        keymap = {"up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27}
        want = set()
        if my < -0.5:
            want.add("up")
        elif my > 0.5:
            want.add("down")
        if mx < -0.5:
            want.add("left")
        elif mx > 0.5:
            want.add("right")
        prev = getattr(self, "_dpad_" + name, set())
        for k in prev - want:
            wi.key_up(keymap[k])
        for k in want - prev:
            wi.key_down(keymap[k])
        setattr(self, "_dpad_" + name, want)
