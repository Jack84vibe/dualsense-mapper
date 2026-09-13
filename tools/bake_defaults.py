"""把一份实际在用的配置烘焙成「出厂默认」。

用法：python tools/bake_defaults.py 某个.config.json

产物是 ps5mapper/default_config.json，会被打进 exe。之后：
  * 第一次运行（旁边还没有配置文件）→ 直接用这份
  * 配置档「重置」/「新建」→ 也用这份里的第一个档当模板

烘焙时会做两件清理：
  1. 去掉机器相关的东西（录音设备列表、当前选中哪一档）
  2. 去掉动作字典里的**残留字段**。改绑定时旧字段没被清掉，会留下
     像 {"type":"mouse","keys":"Ctrl+C","label":"鼠标左键","button":"left"}
     这种东西——type 说了算所以不影响运行，但读起来会让人以为绑错了。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "ps5mapper", "default_config.json")

# 每种动作真正需要的字段，其余一律丢掉
KEEP = {
    "combo": ("type", "keys", "label", "repeat"),
    "mouse": ("type", "button", "label"),
    "builtin": ("type", "name", "label"),
    "macro": ("type", "steps", "label"),
}


def clean_action(act):
    if not isinstance(act, dict):
        return act
    keep = KEEP.get(act.get("type"), ("type", "label"))
    return {k: act[k] for k in keep if k in act}


def clean_profile(p: dict) -> dict:
    p = dict(p)
    p["buttons"] = {k: clean_action(v) for k, v in (p.get("buttons") or {}).items()}
    trig = {}
    for tid, t in (p.get("triggers") or {}).items():
        t = dict(t)
        for k in ("binding", "binding2"):
            if k in t:
                t[k] = clean_action(t[k])
        trig[tid] = t
    p["triggers"] = trig
    return p


def bake(src: str) -> dict:
    with open(src, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg = dict(cfg)
    cfg["profiles"] = [clean_profile(p) for p in (cfg.get("profiles") or [])]
    cfg["active_profile"] = 0          # 别把「上次用的是哪一档」也当成出厂设置
    cfg["audio_devices"] = []          # 录音设备 id 是这台机器专属的
    return cfg


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cfg = bake(sys.argv[1])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("已写入 %s" % os.path.relpath(OUT, ROOT))
    print("配置档 %d 个：%s" % (len(cfg["profiles"]),
                              "、".join(p.get("name", "?") for p in cfg["profiles"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
