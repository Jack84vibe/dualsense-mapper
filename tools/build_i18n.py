"""生成界面的翻译表。

# 翻译表长什么样

    ps5mapper/ui/lang/
        _source.json     每条文字的 id -> 简体原文（**生成的，别手改**）
        zh-TW.json       id -> 繁體
        en.json          id -> English

每个语言一个文件，按 id 对齐。id 是原文的 sha1 前 8 位，所以：

  · 原文没改，id 就不变，已有译文不受影响
  · 原文改了，id 跟着变 -> 旧译文自动作废、显示为「缺失」。
    这正是想要的：中文改了，译文本来就该重翻，不该让旧译文默默留着。

# 要加一门新语言

    1. 复制 lang/_source.json 成 lang/<代码>.json（比如 ja.json）
    2. 把 "_name" 改成这门语言自己的写法（界面上就显示这个）
    3. 把每条的值换成译文；留空或者删掉的条目会退回简体
    4. 跑 python tools/build_i18n.py

界面会自动发现 lang/ 下的所有语言，设置页的选项不用改代码。

# 原文是怎么收集的

界面是单文件 HTML，几百条中文散落在拼接的 HTML 字符串里。用正则从源码里
抠试过两次，都被 JS 的引号和注释坑了，抠出来的是半截代码。所以改成**把界面
真的跑起来**：无头 Chromium 打开 index.html，塞一份假的后端 API，把每一页
都点一遍，再遍历 DOM 拿实际渲染出来的文字。一条不多一条不少。

繁体默认用 opencc(s2twp) 自动转（带台湾用词）；zh-TW.json 里手工改过的条目
会被保留，不会被下次自动转换覆盖。**opencc 只在这里用，程序本身不依赖它。**

用法：
    pip install playwright opencc-python-reimplemented
    python tools/build_i18n.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
UI = os.path.join(ROOT, "ps5mapper", "ui")
LANG_DIR = os.path.join(UI, "lang")

SOURCE_LANG = "zh-CN"
SOURCE_NAME = "简体中文"

# 翻译单元的判定规则。**必须和 index.html 里 i18nUnits() 那份一模一样**，
# 否则生成出来的 id 和运行时查的对不上，整张表失效 —— 而且症状只是
# 「有些地方没翻」，很难联想到是这里。tests/test_i18n.py 守着这件事。
RULE = r"""
window.__i18nUnits = function(root){
  const SEL = 'h1,h2,h3,p,small,b,span,button,option,label,div.empty,div.note>div';
  const all = [...root.querySelectorAll(SEL)];
  const cand = all.filter(el =>
    (/[\u4e00-\u9fff]/.test(el.textContent || '') || el.hasAttribute('data-i18n')) &&
    !el.querySelector('[data-dyn],[data-rename]') &&
    !el.querySelector('svg,i') &&
    !el.closest('[data-nolocalize]') &&
    !el.hasAttribute('data-dyn') && !el.hasAttribute('data-rename') &&
    !el.classList.contains('val') && !el.closest('.val') &&
    !el.querySelector('.k,.v,.pill'));
  return cand.filter(el => !cand.some(o => o !== el && o.contains(el)));
};
"""

HARVEST = r"""
() => {
  const units = window.__i18nUnits(document.body);
  const html = [...new Set(units.map(el => el.innerHTML.trim()))];
  const attrs = [];
  document.body.querySelectorAll('[aria-label],[placeholder],[title]').forEach(el => {
    ['aria-label','placeholder','title'].forEach(a => {
      const v = el.getAttribute(a);
      if (v && /[\u4e00-\u9fff]/.test(v)) attrs.push(v.trim());
    });
  });
  return {html, attrs: [...new Set(attrs)]};
}
"""


def sid(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


# ---------------------------------------------------------------- 收集原文

def _stub():
    from ps5mapper import config as cfgmod
    cfg = cfgmod.default_config()
    state = {"connected": True, "bluetooth": True, "paused": False, "battery": 70,
             "charging": False, "buttons": {},
             "sticks": {"lx": 0, "ly": 0, "rx": 0, "ry": 0},
             "triggers": {"l2": 0, "r2": 0}, "touch": [], "profile": 0,
             "gyro": {"on": True, "calibrated": True, "dps": [0, 0, 0],
                      "tick_us": 0.45, "tick_measured": True, "bias": [0, 0, 0],
                      "recoveries": 0, "static": True, "tilt_dps": 0.0}}
    return """
window.pywebview = { api: {
  get_config: () => Promise.resolve(%s),
  get_state:  () => Promise.resolve(%s),
  config_file_path: () => Promise.resolve("C:/x/ps5mapper.config.json"),
  list_audio: () => Promise.resolve([]),
  set_path: () => Promise.resolve(true),
  open_sound_settings: () => Promise.resolve(true),
  gyro_recalibrate: () => Promise.resolve(true),
  toggle_pause: () => Promise.resolve(false),
  set_active_profile: () => Promise.resolve(null),
}};
""" % (json.dumps({"config": cfg, "conflicts": {}, "version_string": "1.0.0"},
                  ensure_ascii=False),
       json.dumps(state, ensure_ascii=False))


async def harvest():
    from playwright.async_api import async_playwright
    html, attrs = set(), set()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        pg = await browser.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        await pg.add_init_script(_stub())
        await pg.add_init_script(RULE)
        await pg.goto("file://" + os.path.join(UI, "index.html"))
        await pg.wait_for_timeout(1200)

        async def grab():
            r = await pg.evaluate(HARVEST)
            html.update(r["html"])
            attrs.update(r["attrs"])

        navs = await pg.eval_on_selector_all('#nav button', '(b)=>b.map(x=>x.dataset.go)')
        for go in navs:
            await pg.click('#nav button[data-go="%s"]' % go)
            await pg.wait_for_timeout(350)
            await grab()
        # 有些行只在某个开关打开时才出现（双段扳机的「第二道墙起点」、
        # 自定义触发点的那两行……）。默认配置里它们不存在，扫默认配置永远收不到。
        # 所以这里主动把分支都打开一遍。
        await pg.click('#nav button[data-go="trig"]')
        await pg.wait_for_timeout(300)
        await pg.evaluate("""()=>{
            document.querySelectorAll('.seg[data-path$=".mode"] button[data-v="dual"]')
                .forEach(b=>b.click()); }""")
        await pg.wait_for_timeout(400)
        await grab()
        await pg.evaluate("""()=>{
            document.querySelectorAll('.seg[data-path$=".fire_mode"] button[data-v="custom"]')
                .forEach(b=>b.click()); }""")
        await pg.wait_for_timeout(400)
        await grab()

        await pg.click('#nav button[data-go="gyro"]')
        await pg.wait_for_timeout(250)
        await pg.evaluate("""()=>{const s=document.querySelector('[data-path$=".link_sensitivity"]');
                               if(s) s.click();}""")
        await pg.wait_for_timeout(400)
        await grab()
        # 按键编辑器：**每一个热点都点开**，不能只点第一个。
        # 这个弹窗有四种形态，长得完全不一样：普通键、扳机、摇杆、十字键（还带
        # 一层方向子页）。原先这里只有一句 click('.tag')，只打开了第一个热点，
        # 于是另外三种形态里的文字一句都没收到 —— 界面上几十处翻不了，
        # 而且不报错，只是安静地显示中文。
        await pg.click('#nav button[data-go="map"]')
        await pg.wait_for_timeout(300)
        n_tags = await pg.eval_on_selector_all('.tag', '(t)=>t.length')
        if not n_tags:
            print("  提醒：按键页没有热点，编辑器里的文字这轮没收进来", file=sys.stderr)
        for i in range(n_tags):
            await pg.evaluate("(i)=>document.querySelectorAll('.tag')[i].click()", i)
            await pg.wait_for_timeout(260)
            await grab()
            # 十字键：再进一层方向子页，而且**四个方向都要进** ——
            # 每个方向的标题都不一样（十字键 上／下／左／右），只点第一个
            # 就只能收到「十字键 上」。
            n_sub = await pg.eval_on_selector_all('#editor [data-sub]', '(s)=>s.length')
            for j in range(n_sub):
                await pg.evaluate(
                    "(j)=>document.querySelectorAll('#editor [data-sub]')[j].click()", j)
                await pg.wait_for_timeout(200)
                await grab()
                back = await pg.query_selector('#editor [data-act="back"]')
                if not back:
                    break
                await back.click()
                await pg.wait_for_timeout(160)
            await pg.keyboard.press("Escape")
            await pg.wait_for_timeout(90)
        if errors:
            print("  页面报错：%s" % errors[:3], file=sys.stderr)
        await browser.close()
    return sorted(html) + sorted(attrs)


# ---------------------------------------------------------------- 繁体自动转换

# opencc 的台湾用词表在个别领域词上会转错，转完再纠回来。
# 「映射」被转成「對映」——那是数学上的用词；手柄改键的语境下台湾这边
# 讲「映射」或「對應」，「對映」读起来不知所云。
#
# 「循环点亮」被转成「迴圈點亮」——「迴圈」是程式设计里 loop 的台湾译法，
# 灯一圈圈往上点这件事该说「循環」。
#
# 「挨得更紧」被转成「捱得更緊」——「捱」是忍受、苦撑，和「靠得近」无关。
#
# 「绑定」被转成「繫結」——那是资料繫結（data binding）的用法。手柄改键这个
# 语境下台湾玩家讲「綁定」，「繫結」会读成在讲程式。
#
# 「参数」被转成「引數」——那是函式实参的说法。这里指的是设定项，该用「參數」。
TW_FIX = [("對映", "映射"), ("迴圈", "循環"), ("捱得", "挨得"),
          ("繫結", "綁定"), ("引數", "參數")]


def to_traditional(text, cc):
    out = cc.convert(text)
    for a, b in TW_FIX:
        out = out.replace(a, b)
    return out


def main():
    sources = [s for s in asyncio.run(harvest()) if re.search(r'[\u4e00-\u9fff]', s)]

    # 运行时由 JS 拼出来的字符串（连接状态、提示条……）不会整块出现在 DOM 里，
    # 抓不到。它们在 index.html 里由 t() 包着，原文列在这个文件里。
    extra = read_json(os.path.join(LANG_DIR, "_runtime.json"), [])
    sources = sorted(set(sources) | set(extra), key=lambda x: (len(x), x))

    src_map = {sid(s): s for s in sources}
    src_map["_name"] = SOURCE_NAME

    # 安全闸：下面第 244 行那句「原文已经不存在的 id 清掉」是不可逆的删除。
    # 这一版界面里出过一次 `t is not a function`，整页渲染中途炸了，收上来只有
    # 159 条 —— 脚本照样跑完，把 en.json 里 234 条译好的英文当成「原文没了」
    # 全删了，一声不吭。译文是人写的，收集是自动的，自动的那一半出问题不该让
    # 人写的那一半陪葬。
    # 所以：这轮收到的条数比上轮少两成以上，就当收集坏了，什么都不写。
    old_src = read_json(os.path.join(LANG_DIR, "_source.json"), {})
    old_n = len([k for k in old_src if k != "_name"])
    new_n = len(sources)
    if old_n and new_n < old_n * 0.8:
        print("原文只收到 %d 条，上一轮是 %d 条 —— 少得不正常，这轮不写任何文件。"
              % (new_n, old_n), file=sys.stderr)
        print("多半是界面渲染中途报错了（往上看有没有「页面报错」），"
              "先把界面修好再跑。", file=sys.stderr)
        return 1

    write_json(os.path.join(LANG_DIR, "_source.json"), src_map)

    from opencc import OpenCC
    cc = OpenCC("s2twp")

    langs = []
    for fn in sorted(os.listdir(LANG_DIR)):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        code = fn[:-5]
        table = read_json(os.path.join(LANG_DIR, fn))
        name = table.get("_name") or code

        if code == "zh-TW":
            # 自动补全没译过的；手工改过的原样保留
            for i, s in src_map.items():
                if i != "_name" and not table.get(i):
                    table[i] = to_traditional(s, cc)
        # 原文已经不存在的 id 清掉，免得表越积越脏
        table = {i: v for i, v in table.items() if i == "_name" or i in src_map}
        table["_name"] = name
        write_json(os.path.join(LANG_DIR, fn), table)

        missing = [i for i in src_map if i != "_name" and not table.get(i)]
        langs.append((code, name, table, missing))

    # 运行时查表用「原文 -> 译文」，所以在这里就按 id 连接好，
    # 省得界面上再做一次 id 查找。
    built = {}
    for code, name, table, _ in langs:
        built[code] = {src_map[i]: v for i, v in table.items()
                       if i != "_name" and v and i in src_map}

    meta = ([{"code": SOURCE_LANG, "name": SOURCE_NAME}]
            + [{"code": c, "name": n} for c, n, _, _ in langs])
    body = json.dumps(built, ensure_ascii=False, indent=0)
    out = ("/* 翻译表 —— 由 tools/build_i18n.py 从 ui/lang/*.json 生成，**别手改**。\n"
           "   要改译文或加语言，改 ui/lang/ 下的 json，再跑一次生成脚本。\n"
           "   简体是源语言，不查表。 */\n"
           "window.I18N_LANGS = " + json.dumps(meta, ensure_ascii=False) + ";\n"
           "window.I18N = " + body + ";\n")
    with open(os.path.join(UI, "i18n.js"), "w", encoding="utf-8") as f:
        f.write(out)

    print("原文 %d 条" % len(sources))
    for code, name, _, missing in langs:
        flag = "" if not missing else "   缺 %d 条" % len(missing)
        print("  %-6s %-10s %s" % (code, name, flag))
        for i in missing[:8]:
            print("        %s  %s" % (i, src_map[i][:56]))
        if len(missing) > 8:
            print("        …还有 %d 条" % (len(missing) - 8))
    print("已生成 ui/i18n.js（%.1f KB）"
          % (os.path.getsize(os.path.join(UI, "i18n.js")) / 1024))


if __name__ == "__main__":
    sys.exit(main() or 0)
