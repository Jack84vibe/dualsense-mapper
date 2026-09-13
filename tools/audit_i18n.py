"""反着查翻译：把界面切成英文，看哪里**还剩中文**。

# 为什么需要这个

收集原文（tools/build_i18n.py）靠的是「把界面跑起来、点一遍、遍历 DOM 捞文字」。
这个办法有个天生的洞：**我忘了点开的界面状态，它的文字就等于不存在** ——
不报错、不警告，只是那块地方永远显示中文。

按键编辑器就这么漏了一整版：脚本里只有一句 click('.tag')，
只打开了第一个热点，剩下三种形态（扳机、摇杆、十字键）一句都没收到。
一百多处中文，而且全都「看起来像是还没翻」，没人能一眼看出是脚本的问题。

正着查（「表里有哪些条目」）永远发现不了这种洞，因为漏掉的那些根本不在表里。
所以这个脚本反着来：**切成英文，凡是还显示中文的就是问题**，
然后再分类它到底是「没收进表」还是「收了但没套上」。

# 两轮检查

  第一轮  默认配置，把 8 个页面 + 每一个按键热点 + 十字键四个方向都点一遍
  第二轮  改绑定、清绑定、关摇杆、切双段扳机 —— 专查「内容一变就翻不了」的串

第二轮不能省。像「复制 · Ctrl+C」这种运行时拼出来的整句，要是整句进了翻译表，
默认配置下看着好好的，你一改绑定它就变成另一句、表里那条立刻作废、退回中文。
只跑默认配置的审计永远看不见这个。

用法：
    python tools/audit_i18n.py          # 有残留就非零退出
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
UI = os.path.join(ROOT, "ps5mapper", "ui")
LANG_DIR = os.path.join(UI, "lang")

from build_i18n import RULE, _stub  # noqa: E402

# 找出「显示出来的、还含中文的」翻译单元。判定规则复用 build_i18n 注入的
# window.__i18nUnits，和界面里那份是同一套，不然两边对不上。
SCAN = r"""
() => {
  const CJK = /[一-鿿]/;
  const out = [];
  const vis = el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden';
  };
  window.__i18nUnits(document.body).filter(vis).forEach(el => {
    const txt = (el.textContent || '').trim();
    if (!CJK.test(txt)) return;
    let where = [], p = el;
    while (p && p !== document.body) {
      if (p.id) { where.unshift('#' + p.id); break; }
      if (p.classList && p.classList.length) where.unshift('.' + p.classList[0]);
      p = p.parentElement;
    }
    out.push({html: el.innerHTML.trim(), where: where.slice(0, 4).join(' > '),
              skip: !!el.closest('[data-nolocalize]')});
  });
  document.body.querySelectorAll('[aria-label],[placeholder],[title]').forEach(el => {
    ['aria-label','placeholder','title'].forEach(a => {
      const v = el.getAttribute(a);
      if (v && CJK.test(v)) out.push({html: v, where: 'attr:' + a, skip: false});
    });
  });
  return out;
}
"""


def sid(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


async def open_ui(pw):
    br = await pw.chromium.launch()
    pg = await br.new_page(viewport={"width": 1400, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    await pg.add_init_script(_stub())
    await pg.add_init_script(RULE)
    await pg.goto("file://" + os.path.join(UI, "index.html"))
    await pg.wait_for_timeout(1200)
    # 走界面自己那条路切语言（页面整体包在 IIFE 里，外面碰不到内部变量，
    # 而且用真实路径切换才和用户的实际操作一致）
    await pg.click('#nav button[data-go="set"]')
    await pg.wait_for_timeout(350)
    await pg.click('.seg[data-path="language"] button[data-v="en"]')
    await pg.wait_for_timeout(450)
    return br, pg, errs


async def pass_one(found):
    """默认配置，走遍所有页面和所有按键热点。"""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        br, pg, errs = await open_ui(pw)

        async def scan(tag):
            for r in await pg.evaluate(SCAN):
                if not r["skip"]:
                    found.setdefault(r["html"], (tag, r["where"]))

        navs = await pg.eval_on_selector_all('#nav button', '(b)=>b.map(x=>x.dataset.go)')
        for go in navs:
            await pg.click('#nav button[data-go="%s"]' % go)
            await pg.wait_for_timeout(320)
            await scan("页面:" + go)

        await pg.click('#nav button[data-go="map"]')
        await pg.wait_for_timeout(320)
        n = await pg.eval_on_selector_all('.tag', '(t)=>t.length')
        for i in range(n):
            await pg.evaluate("(i)=>document.querySelectorAll('.tag')[i].click()", i)
            await pg.wait_for_timeout(260)
            await scan("编辑器#%d" % i)
            m = await pg.eval_on_selector_all('#editor [data-sub]', '(s)=>s.length')
            for j in range(m):
                await pg.evaluate(
                    "(j)=>document.querySelectorAll('#editor [data-sub]')[j].click()", j)
                await pg.wait_for_timeout(200)
                await scan("编辑器#%d>方向%d" % (i, j))
                back = await pg.query_selector('#editor [data-act="back"]')
                if not back:
                    break
                await back.click()
                await pg.wait_for_timeout(150)
            await pg.keyboard.press("Escape")
            await pg.wait_for_timeout(90)

        await br.close()
        return errs


async def pass_two(found):
    """改绑定、清绑定、关摇杆、切双段扳机之后再看一遍。"""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        br, pg, errs = await open_ui(pw)

        async def scan(tag):
            for r in await pg.evaluate(SCAN):
                if not r["skip"]:
                    found.setdefault(r["html"], (tag, r["where"]))

        await pg.click('#nav button[data-go="map"]')
        await pg.wait_for_timeout(320)
        # 换成一个默认配置里没用过的功能
        await pg.evaluate("()=>document.querySelectorAll('.tag')[1].click()")
        await pg.wait_for_timeout(300)
        await pg.evaluate("""()=>{const b=[...document.querySelectorAll('#editor .chip')]
            .find(x=>/Screenshot|截图/.test(x.textContent)); if(b) b.click();}""")
        await pg.wait_for_timeout(350)
        await scan("改绑之后")
        # 清掉绑定
        await pg.evaluate("()=>{const c=document.querySelector('#editor .clear'); if(c) c.click();}")
        await pg.wait_for_timeout(350)
        await scan("清除绑定")
        await pg.keyboard.press("Escape")
        await pg.wait_for_timeout(250)
        await scan("按键页(有未绑定)")
        # 摇杆关掉
        await pg.evaluate("""()=>{const t=[...document.querySelectorAll('.tag')]
            .find(x=>/L-STICK/.test(x.textContent)); if(t) t.click();}""")
        await pg.wait_for_timeout(300)
        await pg.evaluate("""()=>{const b=document.querySelector('#editor [data-stickmode="off"]');
            if(b) b.click();}""")
        await pg.wait_for_timeout(350)
        await pg.keyboard.press("Escape")
        await pg.wait_for_timeout(250)
        await scan("摇杆关闭")
        # 扳机双段 + 自定义触发点
        await pg.click('#nav button[data-go="trig"]')
        await pg.wait_for_timeout(300)
        await pg.evaluate("""()=>{document.querySelectorAll(
            '.seg[data-path$=".mode"] button[data-v="dual"]').forEach(b=>b.click());}""")
        await pg.wait_for_timeout(400)
        await scan("扳机双段")
        await pg.evaluate("""()=>{document.querySelectorAll(
            '.seg[data-path$=".fire_mode"] button[data-v="custom"]').forEach(b=>b.click());}""")
        await pg.wait_for_timeout(400)
        await scan("扳机自定义触发点")

        await br.close()
        return errs


def main():
    found = {}
    errs = asyncio.run(pass_one(found)) + asyncio.run(pass_two(found))

    src = json.load(open(os.path.join(LANG_DIR, "_source.json"), encoding="utf-8"))
    en = json.load(open(os.path.join(LANG_DIR, "en.json"), encoding="utf-8"))
    tw = json.load(open(os.path.join(LANG_DIR, "zh-TW.json"), encoding="utf-8"))

    print("=" * 70)
    print("英文模式下仍显示中文的元素：%d 个" % len(found))
    print("=" * 70)

    if errs:
        print("\n!! 页面报错，下面的结果不可信，先修界面：%s" % errs[:3])

    a = [(h, v) for h, v in found.items() if sid(h) not in src]
    b = [(h, v) for h, v in found.items() if sid(h) in src]

    if a:
        print("\n[A] 原文表里没有这句 —— 收集脚本没走到那个界面 (%d)" % len(a))
        for h, (tag, where) in sorted(a, key=lambda x: x[1][0]):
            print("  %-22s %-30s %s" % (tag, where[:30], h[:60].replace("\n", " ")))
    if b:
        print("\n[B] 表里有、译文也有，但没被套上 —— 那块 DOM 画完没人 localize (%d)" % len(b))
        for h, (tag, where) in sorted(b, key=lambda x: x[1][0]):
            print("  %-22s %-30s %s" % (tag, where[:30], h[:60].replace("\n", " ")))

    miss_en = [i for i in src if i != "_name" and not en.get(i)]
    miss_tw = [i for i in src if i != "_name" and not tw.get(i)]
    if miss_en or miss_tw:
        print("\n[C] 原文表里有、但缺译文：en 缺 %d，zh-TW 缺 %d"
              % (len(miss_en), len(miss_tw)))
        for i in miss_en[:20]:
            print("   en 缺：%s  %s" % (i, src[i][:50]))

    ok = not found and not miss_en and not miss_tw and not errs
    print("\n" + ("全部干净。" if ok else "有问题，见上。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
