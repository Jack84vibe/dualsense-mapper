"""界面语言相关的测试。

词典本身是生成的（tools/build_i18n.py），但生成物必须满足几条硬约束，
否则界面会静默地翻一半、或者切回中文时残留英文。这里把这些约束钉住。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ps5mapper import config as cfgmod

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "ps5mapper", "ui")


LANG_DIR = os.path.join(UI, "lang")


def load_dict():
    """读生成出来的 i18n.js（运行时真正用的那份）。"""
    src = open(os.path.join(UI, "i18n.js"), encoding="utf-8").read()
    body = src.split("window.I18N = ", 1)[1].rstrip().rstrip(";")
    return json.loads(body)


def load_table(name):
    with open(os.path.join(LANG_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def sid(text):
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def test_default_language_is_simplified():
    assert cfgmod.default_config()["language"] == "zh-CN"


def test_language_survives_save_and_load(tmp_path):
    cfg = cfgmod.default_config()
    cfg["language"] = "en"
    p = str(tmp_path / "c.json")
    assert cfgmod.save(cfg, p)
    assert cfgmod.load(p)["language"] == "en"


def test_old_config_without_language_gets_the_default(tmp_path):
    cfg = cfgmod.default_config()
    cfg.pop("language", None)
    p = str(tmp_path / "old.json")
    cfgmod.save(cfg, p)
    assert cfgmod.load(p)["language"] == "zh-CN"


def test_dictionary_has_both_languages():
    d = load_dict()
    assert set(d) == {"zh-TW", "en"}
    assert len(d["en"]) > 300
    assert len(d["zh-TW"]) > 250


def test_every_language_is_a_separate_table_keyed_by_id():
    """一个语言一个 json，按 id 对齐 —— 加语言不该要改代码。"""
    src = load_table("_source.json")
    assert src["_name"] == "简体中文"
    for name in ("en.json", "zh-TW.json"):
        t = load_table(name)
        assert t.get("_name"), "%s 少了 _name（设置页要显示它）" % name
        stray = [k for k in t if k != "_name" and k not in src]
        assert not stray, "%s 里有原文已不存在的 id：%s" % (name, stray[:5])


def test_ids_are_derived_from_the_source_text():
    """id = 原文的 sha1 前 8 位。

    这让 id 可复现，也让「中文改了译文自动作废」成为必然结果，
    而不是靠人记得去改。
    """
    src = load_table("_source.json")
    for i, text in src.items():
        if i == "_name":
            continue
        assert i == sid(text), "id %s 和原文对不上" % i


def test_language_list_is_exposed_for_the_ui():
    """设置页的语言选项从这里读，所以新语言不用改界面代码。"""
    js = open(os.path.join(UI, "i18n.js"), encoding="utf-8").read()
    meta = json.loads(js.split("window.I18N_LANGS = ", 1)[1].split(";", 1)[0])
    codes = {m["code"] for m in meta}
    assert codes == {"zh-CN", "en", "zh-TW"}
    for m in meta:
        assert m["name"]


def test_missing_translations_fall_back_to_simplified():
    """译文留空的条目不该进生成物 —— 进去就会变成空白界面。"""
    for lang, table in load_dict().items():
        blank = [k for k, v in table.items() if not v]
        assert not blank, "%s 有空译文：%s" % (lang, blank[:3])


def test_english_has_no_chinese_left():
    """英文词典里不该再有中文——漏翻会让界面中英夹杂。

    例外只有语言名本身：语言选择器上「简体中文 / 繁體中文 / English」
    永远写各自的母语，这是惯例。
    """
    exempt = {"简体中文", "繁體中文"}
    bad = [(k, v) for k, v in load_dict()["en"].items()
           if k not in exempt and re.search(r'[一-鿿]', v)]
    assert not bad, "英文词典里还有中文：%s" % bad[:5]


def test_traditional_is_actually_converted():
    tw = load_dict()["zh-TW"]
    assert tw["触摸板"] == "觸控板"
    assert tw["设置"] == "設定"
    assert tw["默认关闭，避免和玩游戏冲突"].startswith("預設")


def test_traditional_does_not_use_the_maths_word_for_mapping():
    """opencc 会把「映射」转成「對映」——那是数学用词。

    手柄改键的语境下台湾讲「映射」或「對應」，「對映」读起来莫名其妙。
    生成脚本里有一张纠正表，这条守着它别被删掉。
    """
    tw = load_dict()["zh-TW"]
    assert not [v for v in tw.values() if "對映" in v]
    assert tw["按键映射"] == "按鍵映射"


def test_markup_is_preserved_in_translations():
    """带 <b>/<br>/<svg> 的条目，译文必须保留同样的标记。

    少一个闭合标签，整块界面的排版就会塌掉。
    """
    d = load_dict()
    for lang in ("en", "zh-TW"):
        for k, v in d[lang].items():
            for tag in ("<b>", "</b>", "<br>", "<em>", "</em>"):
                assert k.count(tag) == v.count(tag), \
                    "%s 译文里 %s 数量对不上：%r" % (lang, tag, k[:40])
            if k.startswith("<svg"):
                assert v.startswith("<svg") and "</svg>" in v, k[:40]


def test_translation_tables_are_complete():
    """两门已有语言都不该缺条目 —— 缺了界面就会中英夹杂。"""
    src = {k for k in load_table("_source.json") if k != "_name"}
    for name in ("en.json", "zh-TW.json"):
        t = load_table(name)
        missing = [i for i in src if not t.get(i)]
        assert not missing, "%s 缺 %d 条" % (name, len(missing))


def test_runtime_and_generator_share_one_rule():
    """翻译单元的判定规则在两处各有一份，必须一致。

    不一致的话，生成出来的 key 和运行时查的 key 对不上，词典整张失效，
    而且界面看起来只是「有些地方没翻」，很难想到是这个原因。
    """
    html = open(os.path.join(UI, "index.html"), encoding="utf-8").read()
    gen = open(os.path.join(os.path.dirname(UI), "..", "tools", "build_i18n.py"),
               encoding="utf-8").read()
    for frag in ("[data-dyn],[data-rename]",
                 "classList.contains('val')",
                 ".k,.v,.pill",
                 "svg,i",
                 "[data-nolocalize]",
                 "h1,h2,h3,p,small,b,span,button,option,label,div.empty,div.note>div"):
        assert frag in html, "index.html 里少了规则片段 %r" % frag
        assert frag in gen, "build_i18n.py 里少了规则片段 %r" % frag


def test_language_picker_is_built_from_the_tables():
    """选项不再写死在界面里，而是从 I18N_LANGS 来。"""
    html = open(os.path.join(UI, "index.html"), encoding="utf-8").read()
    assert "window.I18N_LANGS" in html
    assert 'seg("language"' in html
    assert '["zh-TW","繁體中文"]' not in html, "语言选项又被写死了"


def test_translated_elements_stay_translatable():
    """翻成英文之后元素就不含中文了。

    判定单元时只看「含不含中文」的话，这些元素会从此被漏掉 ——
    切回中文时，导航和页头会永远卡在英文。用户实测撞到过这个。
    """
    html = open(os.path.join(UI, "index.html"), encoding="utf-8").read()
    assert 'el.hasAttribute("data-i18n")' in html


# ---------------------------------------------------------------- 运行时字符串

def _script():
    return open(os.path.join(UI, "index.html"), encoding="utf-8").read().split("<script>", 1)[1]


def _wrapped_literals():
    """代码里被 t() / toast() 直接包住的中文字面量。

    这类字符串是 JS 在运行时拼出来的，不会整块出现在 DOM 里，无头浏览器抓不到，
    只能靠 lang/_runtime.json 手工登记。登记漏了的后果是**静默不翻** ——
    界面照常显示，只是那一句是中文，没有任何报错。
    「麦克风键」就是这么漏掉一整版的。
    """
    pat = re.compile(r'(?<![A-Za-z0-9_$.])(?:t|toast)\(\s*(["\'])((?:(?!\1)[^\\]|\\.)*)\1')
    out = set()
    for m in pat.finditer(_script()):
        s = m.group(2)
        if re.search(r'[一-鿿]', s):
            out.add(s)
    return out


def test_every_runtime_string_is_registered():
    """t()/toast() 用到的每一句中文，都必须在 _runtime.json 里登记。"""
    registered = set(load_table("_runtime.json"))
    missing = sorted(_wrapped_literals() - registered)
    assert not missing, (
        "这些字符串在代码里用 t() 包着，但没登记进 lang/_runtime.json，"
        "生成器看不到它们，界面上会一直是中文：%s" % missing)


def test_registered_runtime_strings_have_translations():
    """登记了就必须有译文，两门语言都要有。"""
    src = load_table("_source.json")
    by_text = {v: k for k, v in src.items() if k != "_name"}
    tables = {code: load_table(code + ".json") for code in ("en", "zh-TW")}
    bad = []
    for s in load_table("_runtime.json"):
        i = by_text.get(s)
        if i is None:
            bad.append(("不在原文表里", s))
            continue
        for code, tbl in tables.items():
            if not tbl.get(i):
                bad.append((code + " 缺译文", s))
    assert not bad, bad


def test_translator_name_is_not_shadowed():
    """别在调用 t() 的作用域里用 t 当局部变量名。

    renderTriggers 里原本有个 `var t = p.triggers[id]`，后来往同一个函数里加了
    t("…")，整页渲染当场炸成「t is not a function」——不是少翻一句，是整页空白。
    这里只做一个粗筛：出现 `var t=` 或 `, t=` 的行，同一个函数体里不许再有 t(" 。
    """
    script = _script()
    # 按顶层 function 切开，逐段看
    parts = re.split(r'\nfunction ', script)
    bad = []
    for p in parts:
        name = p.split("(", 1)[0].strip()[:40]
        if re.search(r'(?:var|,)\s*t\s*=', p) and re.search(r'(?<![A-Za-z0-9_$.])t\(\s*["\']', p):
            bad.append(name)
    assert not bad, "这些函数里 t 既当局部变量又当翻译函数，会炸：%s" % bad
