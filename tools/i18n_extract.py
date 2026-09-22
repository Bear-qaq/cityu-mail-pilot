#!/usr/bin/env python3
"""抽出「公开面上所有该翻译的原文」，写成 `pilot_app/static/i18n/keys.json`。

为什么要有这个工具
------------------
多语言最容易烂掉的方式不是「没翻完」，而是**没人知道还剩多少没翻**。于是
界面上这里英文那里中文，改了三个月也没人说得清是设计还是漏了。所以：

* 键是从**源码里现抽**的，不是手抄的清单——手抄的清单第二天就过期；
* `test_i18n` 拿它当棘轮：**已校对的语言必须 100% 覆盖**，差哪条点名说哪条；
* 词典里有、源码里已经没有的条目（孤儿）也报出来：那句话改了，旧译文永远
  匹配不上，留着只会让人以为「这句译过了」。

用法::

    .venv-pilot/bin/python tools/i18n_extract.py            # 只看报告，不写文件
    .venv-pilot/bin/python tools/i18n_extract.py --write    # 写回 keys.json
    .venv-pilot/bin/python tools/i18n_extract.py --missing en   # 列出英文还差哪些

扫描范围是**显式清单**（下面 `HTML_SOURCES` / `CODE_SOURCES`）。不做全仓库
扫描是有意的：范围列出来的才受棘轮约束，也就不会有「抽了一堆管理后台的句子，
逼着这一轮把后台也译完」这种事。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pilot_app import i18n  # noqa: E402

#: 这一轮承诺覆盖的页面。**加一行 = 承诺这一页全译**，所以是一行一行加的。
#: `pilot_app/static/index.html` 在清单里，但登录之后的界面带着 `data-i18n-skip`
#: （第一轮只做未登录就能看到的公开面），所以它实际只出二十来条。
HTML_SOURCES = [
    "pilot_app/static/landing.html",
    "pilot_app/static/privacy.html",
    "pilot_app/static/terms.html",
    "pilot_app/static/index.html",
]

#: JS/Python 里显式包了 ``t(...)`` 的句子。只有被包过的才会被抽到——这是
#: 有意的：抽不出来就是「还没接 i18n」，不是「漏了一条」。
CODE_SOURCES = [
    "pilot_app/static/landing.js",
    "pilot_app/static/app.js",
    "pilot_app/web.py",
]

#: ``t('…')`` / ``t("…")``。只认单行字面量：带插值的句子本来就该走 ``{name}``
#: 参数形式，而不是靠拼字符串。``i18n.t(...)`` 这种带前缀的**不算**——那是我
#: 在 web.py 的 dispatch 里统一翻译 API 报错用的，不是一条待译文案。
_CALL = re.compile(r"(?<![\w.])t\(\s*(\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*')\s*(?:,|\))")
_ESCAPE = re.compile(r"\\(.)")


def code_keys(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    out: list[str] = []
    for match in _CALL.finditer(text):
        literal = match.group(1)[1:-1]
        value = _ESCAPE.sub(r"\1", literal)
        if i18n.has_cjk(value) and value not in out:
            out.append(value)
    return out


def collect() -> dict:
    required: list[str] = []
    fallback: list[str] = []
    per_file: dict[str, list[str]] = {}
    for relative in HTML_SOURCES:
        path = ROOT / relative
        if not path.is_file():
            continue
        must, extra = i18n.unit_keys(path.read_text(encoding="utf-8"))
        per_file[relative] = must
        for key in must:
            if key not in required:
                required.append(key)
        for key in extra:
            if key not in fallback and key not in required:
                fallback.append(key)
    code: dict[str, list[str]] = {}
    for relative in CODE_SOURCES:
        path = ROOT / relative
        if not path.is_file():
            continue
        found = code_keys(path)
        if found:
            code[relative] = found
        for key in found:
            if key not in required:
                required.append(key)
    return {
        "note": "由 tools/i18n_extract.py 生成；键就是中文原文。手改这份文件没有意义。",
        "keys": required,
        "required": required,
        "fallback": fallback,
        "pages": per_file,
        "code": code,
    }


def report(data: dict, locale: str = "") -> None:
    keys = list(data["keys"])
    print("公开面需要翻译的原文：%d 条（另有无块级标签切碎的兜底 %d 条）"
          % (len(keys), len(data["fallback"])))
    for relative, items in (data["pages"] or {}).items():
        print("  %-38s %4d 条" % (relative, len(items)))
    for relative, items in (data["code"] or {}).items():
        print("  %-38s %4d 条（t() 包过的）" % (relative, len(items)))
    if not locale:
        return
    table = i18n.catalog(locale)
    missing = [key for key in keys if key not in table]
    done = len(keys) - len(missing)
    percent = (100.0 * done / len(keys)) if keys else 0.0
    print("\n%s：%d/%d（%.1f%%）%s"
          % (i18n.label_of(locale), done, len(keys), percent,
             "" if i18n.reviewed(locale) else "  ← 未校对"))
    for key in missing[:20]:
        print("  缺：%s" % key.replace("\n", " ")[:96])
    if len(missing) > 20:
        print("  …还有 %d 条" % (len(missing) - 20))
    extra = sorted(key for key in table if key not in set(keys))
    if extra:
        print("词典里有、源码里已找不到的（孤儿）%d 条：" % len(extra))
        for key in extra[:10]:
            print("  ? %s" % key.replace("\n", " ")[:96])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="写回 pilot_app/static/i18n/keys.json")
    parser.add_argument("--missing", default="", help="列出这门语言还差哪些条目")
    options = parser.parse_args()

    data = collect()
    report(data, options.missing)
    if options.write:
        i18n.KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        i18n.KEYS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("\n已写入 %s" % i18n.KEYS_FILE.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
