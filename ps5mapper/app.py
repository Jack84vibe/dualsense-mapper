"""程序外壳：pywebview 窗口 + JS 桥 + 托盘图标。

UI 用 HTML 写（ui/index.html），通过 Edge WebView2 渲染 —— Win10/11 自带，
所以打包出来的体积很小，也不用装运行时。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import webbrowser

from . import __version__
from . import config as cfgmod
from . import triggers as trg
from .engine import Engine

try:
    import webview
except ImportError:
    webview = None


def ui_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", here)
        return os.path.join(base, "ps5mapper", "ui", "index.html")
    return os.path.join(here, "ui", "index.html")


class Api:
    """暴露给页面 JS 的方法，全部通过 window.pywebview.api.xxx() 调用。"""

    # pywebview 会递归遍历这个对象来生成 JS 端的 api：碰到方法就注册，
    # 碰到公开的普通属性就当嵌套 API 继续往里钻。App 又持有 Api，
    # 一旦用公开名字保存就变成 Api -> app -> api -> app 的无限套娃，
    # 主线程卡死、api 变成空对象、窗口显示「未响应」。
    # 下划线开头的成员不会被暴露，所以这里必须是 _app。
    _serializable = False

    def __init__(self, app: "App"):
        self._app = app

    # -- 配置 --------------------------------------------------------
    def get_config(self):
        if not self._app.bridge_ok:
            self._app.bridge_ok = True
            print("  [ok] 界面已连上后端")
        cfg = self._app.cfg
        return {
            "config": cfg,
            "conflicts": cfgmod.find_conflicts(cfgmod.active(cfg)),
            "builtins": cfgmod.BUILTINS,
            "presets": trg.PRESETS,
            # 界面右上角显示的版本。以前界面里写死 "1.0.0"，而 __init__.py 里
            # 那个 __version__ 没有任何人读 —— 改了版本号界面也不会变。接上。
            "version_string": __version__,
        }

    def set_binding(self, control_id, action):
        prof = cfgmod.active(self._app.cfg)
        if control_id in ("l2", "r2"):
            prof["triggers"][control_id]["binding"] = action
        elif control_id in ("l2_2", "r2_2"):
            prof["triggers"][control_id[:2]]["binding2"] = action
        else:
            prof["buttons"][control_id] = action
        self._app.save()
        return self.get_config()

    def set_path(self, path, value):
        """按点分路径改配置，比如 set_path("profiles.0.sticks.left.speed", 14)"""
        node = self._app.cfg
        parts = str(path).split(".")
        for p in parts[:-1]:
            node = node[int(p)] if isinstance(node, list) else node[p]
        last = parts[-1]
        if isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value
        self._app.save()
        return True

    def set_active_profile(self, index):
        self._app.engine.set_profile(int(index))
        self._app.save()
        return self.get_config()

    def add_profile(self, name):
        p = cfgmod.default_profile(name or "新配置档")
        self._app.cfg["profiles"].append(p)
        self._app.save()
        return self.get_config()

    def delete_profile(self, index):
        profs = self._app.cfg["profiles"]
        if len(profs) > 1 and 0 <= index < len(profs):
            profs.pop(index)
            self._app.cfg["active_profile"] = min(self._app.cfg["active_profile"], len(profs) - 1)
            self._app.save()
        return self.get_config()

    def duplicate_profile(self, index):
        """完整复制一份，并切到新的那一档——复制出来通常就是要接着改它。"""
        profs = self._app.cfg["profiles"]
        new_i = cfgmod.duplicate_profile(profs, int(index))
        if new_i is not None:
            self._app.engine.set_profile(new_i)
            self._app.save()
        return self.get_config()

    def rename_profile(self, index, name):
        profs = self._app.cfg["profiles"]
        i = int(index)
        if 0 <= i < len(profs):
            profs[i]["name"] = cfgmod.unique_name(profs, name, skip=i)
            self._app.save()
        return self.get_config()

    def reset_profile(self, index):
        profs = self._app.cfg["profiles"]
        if 0 <= index < len(profs):
            name = profs[index].get("name")
            profs[index] = cfgmod.default_profile(name)
            self._app.save()
        return self.get_config()

    # -- 运行时 ------------------------------------------------------
    def get_state(self):
        return self._app.engine.snapshot()

    def toggle_pause(self):
        self._app.engine.toggle_pause()
        return self._app.engine.paused

    def gyro_recalibrate(self):
        """手动重设陀螺仪零点。光标自己往一边爬的时候救急用。"""
        self._app.engine.gyro.reset_zero()
        self._app.engine.gyro.clear()
        return True

    def switch_audio(self):
        self._app.engine.switch_audio_device()
        return self.list_audio()

    def list_audio(self):
        try:
            from . import audio
            return [{"id": i, "name": n} for i, n in audio.list_input_devices()]
        except Exception:
            return []

    def set_audio_device(self, device_id):
        from . import audio
        return audio.set_default_input(device_id)

    def open_sound_settings(self):
        from . import audio
        audio.open_sound_settings()
        return True

    def open_url(self, url):
        webbrowser.open(url)
        return True

    def config_file_path(self):
        return cfgmod.config_path()

    def quit(self):
        self._app.quit()
        return True


class App:
    _serializable = False        # 别让 pywebview 试图把它当成嵌套 API

    def __init__(self):
        self.cfg = cfgmod.load()
        self.engine = Engine(self.cfg, on_event=self._on_event)
        self.api = Api(self)
        self.window = None
        self.tray = None
        self.bridge_ok = False
        self._save_timer = None

    # -- 存盘（合并短时间内的多次修改） ---------------------------------
    def save(self):
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(0.6, lambda: cfgmod.save(self.cfg))
        self._save_timer.daemon = True
        self._save_timer.start()

    def _on_event(self, kind, payload=None):
        if self.window is None:
            return
        try:
            self.window.evaluate_js(
                "window.__onEngineEvent && window.__onEngineEvent(%s, %s)"
                % (json.dumps(kind), json.dumps(payload)))
        except Exception:
            pass

    # -- 托盘 --------------------------------------------------------
    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return

        def icon_image(active=True):
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            body = (91, 157, 255, 255) if active else (110, 116, 130, 255)
            d.rounded_rectangle((6, 20, 58, 46), radius=13, fill=body)
            d.ellipse((14, 26, 26, 38), fill=(12, 14, 18, 255))
            d.ellipse((38, 26, 50, 38), fill=(12, 14, 18, 255))
            return img

        def on_show(*_):
            if self.window:
                self.window.show()

        def on_pause(*_):
            self.engine.toggle_pause()
            self.tray.icon = icon_image(not self.engine.paused)

        def on_profile(idx):
            def handler(*_):
                self.engine.set_profile(idx)
                self.save()
            return handler

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", on_show, default=True),
            pystray.MenuItem("暂停 / 恢复映射", on_pause),
            pystray.MenuItem("配置档", pystray.Menu(*[
                pystray.MenuItem(p.get("name", f"档 {i+1}"), on_profile(i))
                for i, p in enumerate(self.cfg["profiles"])
            ])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda *_: self.quit()),
        )
        self.tray = pystray.Icon("ps5mapper", icon_image(True), "DualSense 映射台", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    # -- 启动 / 退出 --------------------------------------------------
    def run(self):
        if webview is None:
            print("缺少 pywebview，请先 pip install pywebview")
            return
        page = ui_path()
        print("  pywebview %s" % getattr(webview, "__version__", "?"))
        print("  界面文件  %s" % page)
        if not os.path.exists(page):
            print("  [错误] 界面文件不存在，程序无法显示内容")
            return
        print("  配置文件  %s" % cfgmod.config_path())
        self.engine.start()
        self._start_tray()
        self.window = webview.create_window(
            "DualSense 映射台",
            page,
            js_api=self.api,
            width=1180, height=820, min_size=(940, 660),
            background_color="#0A0B0E",
            hidden=bool(self.cfg.get("start_minimized")),
        )
        self.window.events.closed += self._on_closed
        webview.start(debug=False)

    def _on_closed(self):
        self.quit()

    def quit(self):
        try:
            self.engine.stop()
        except Exception:
            pass
        cfgmod.save(self.cfg)
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            if self.window:
                self.window.destroy()
        except Exception:
            pass
        os._exit(0)


def main():
    App().run()
