# -*- coding: utf-8 -*-
"""`$变量` 紧跟中文标点会把标点吞进变量名 —— 这条坑的守卫。

2026-09-20 由用户的终端发现：他敲 `dorm watch`，得到

    tools/dorm.sh: line 470: target?: unbound variable

那一段源码是 `say "（没给会话，就看最新那个任务：$target）"`。在 **multibyte locale**
下（他的 macOS Terminal 会给 `LC_CTYPE=UTF-8`），bash 的变量名解析会把紧随其后那个
**多字节字符的字节**也算进名字里，于是它去找一个叫 ``target）`` 的变量 —— 名字从没
被赋值过，脚本里又有 `set -u`，直接致命。同一个文件在 `LC_ALL=C` 下完全正常，
所以这类 bug **只在写它的人的环境之外出现**，本地测试全绿。

后果分两档：带 `set -u` 的脚本**当场死**；不带的只是把那句话里的标点默默吃掉
（少一个「）」你不会发现）。所以判据是「一个都不许有」，不是「别崩就行」。

修法是 `$name` → `${name}`（只在后面紧跟非 ASCII 字符时才有必要）。
这些测试钉住三件事：现在的树是干净的、这条判据真的会红（拿一段坏样本喂给扫描器）、
以及扫描器看的是树的实际内容而不是「文档里写过」。
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]

# `$name` 后面紧跟一个非 ASCII 字节。已写成 `${name}` 的不算 —— 花括号天生把名字关死。
UNSAFE = re.compile(rb"\$([A-Za-z_][A-Za-z0-9_]*)(?=[\x80-\xff])")

SKIP_DIRS = {".git", "node_modules", ".venv", ".venv-pilot", "dist", "__pycache__"}


def shell_scripts() -> list[pathlib.Path]:
    """仓库里所有 shell 脚本（排除产物目录）。"""
    return [
        path for path in sorted(ROOT.rglob("*.sh"))
        if not any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)
    ]


def unsafe_hits(data: bytes) -> list[str]:
    """`$name` 紧跟非 ASCII 的行号 + 变量名；坏样本测试也用它，所以抽出来。"""
    return [
        f"第 {data.count(chr(10).encode() + b'', 0, m.start()) + 1} 行：${m.group(1).decode()}"
        for m in UNSAFE.finditer(data)
    ]


class ShellInterpolationTests(unittest.TestCase):
    def test_no_variable_is_glued_to_a_non_ascii_character(self):
        """一个都不许有：带 `set -u` 的会当场死，不带的会默默吃掉标点。"""
        offenders = []
        for path in shell_scripts():
            hits = unsafe_hits(path.read_bytes())
            if hits:
                rel = path.relative_to(ROOT)
                offenders.extend(f"{rel} {hit}" for hit in hits)
        self.assertEqual(
            offenders, [],
            "这些地方 `$变量` 后面紧跟了非 ASCII 字符，请写成 `${变量}`：\n"
            + "\n".join(offenders),
        )

    def test_the_scanner_actually_catches_the_broken_shape(self):
        """反向验证：喂一段坏样本，扫描器必须报出来（否则它只是「永远绿」）。"""
        broken = (
            'set -u\n'
            'target="x"\n'
            'echo "（没给会话，就看最新那个任务：$target）"\n'
        ).encode("utf-8")
        self.assertEqual(len(unsafe_hits(broken)), 1)
        fixed = broken.replace(b"$target", b"${target}")
        self.assertEqual(unsafe_hits(fixed), [])

    def test_the_scanner_reads_the_real_tree(self):
        """别把「文档里写过」当豁免：扫描器看的是文件字节，脚本确实被找到了。"""
        names = {path.name for path in shell_scripts()}
        for expected in ("dorm.sh", "deploy_prod.sh", "run_browser_checks.sh"):
            self.assertIn(expected, names, f"{expected} 没被扫到 —— 扫描范围有问题")


if __name__ == "__main__":
    unittest.main()
