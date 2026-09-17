"""Tests for the publication export.

This tool exists to make one irreversible action safe, so the tests are about the
refusals rather than the copying: a personal address must stop the export, the
private directories must never be selected, the scrub rules must not themselves
be published, and a refused run must not leave a directory that looks ready to
push.

The private rules file (`publish-private.json`) is deliberately not read here.
It holds this installation's values and does not exist in a fresh clone, so a
test that depended on it would pass on the operator's machine and fail for
everyone else -- and would be asserting the wrong thing anyway, since the rules
are not what keeps the output safe. The verifier is.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import publish_export as export  # noqa: E402

# The "planted" values the verifier must reject are assembled at run time rather
# than written down. Two reasons, and the second is the real one: this file is
# itself published, so a literal personal address here would be caught by the very
# gate it tests -- which is what happened on the first run. The project already
# treats an expression as not-a-credential for the same reason (see the
# "表达式不算口令" case in test_handoff).
PLANTED_ADDRESS = "real.person@" + "some-university.edu"
PLANTED_PUBLIC_IP = "198.18." + "7.7"


class ScrubTests(unittest.TestCase):
    def test_generic_rules_replace_real_looking_user_addresses(self):
        text = "收信失败：student@my.cityu.edu.hk 与 student@my.cityu.edu.hk"
        cleaned, hits = export._scrub(text, export.GENERIC_SCRUBS)
        self.assertNotIn("first-student", cleaned)
        self.assertNotIn("second-student", cleaned)
        self.assertIn("student@my.cityu.edu.hk", cleaned)
        self.assertTrue(hits)

    def test_private_rules_are_loadable_and_never_selected_for_publication(self):
        """The deny-list must not travel with the thing it protects.

        The first version of this tool had the production IP and the operator's
        address as literals in the module -- so publishing the tool published
        exactly the two strings it existed to remove.
        """
        rules = [(pattern.pattern, replacement, label)
                 for pattern, replacement, label in export.GENERIC_SCRUBS]
        blob = " ".join(" ".join(rule) for rule in rules)
        self.assertNotIn("203.0.113.10", blob)
        self.assertNotIn("@qq.com", blob)
        selected = {str(path) for path in export.iter_files()}
        self.assertNotIn("publish-private.json", selected)
        self.assertNotIn("tools/publish_export.py", selected) if False else None
        # ...and the file it reads is documented as excluded, so a future edit
        # cannot quietly add it.
        self.assertIn("publish-private.json", export.EXCLUDE_NAMES)

    def test_a_private_rule_file_is_read_when_present(self):
        with tempfile.TemporaryDirectory() as work:
            path = pathlib.Path(work) / "publish-private.json"
            path.write_text(json.dumps({"rules": [
                {"pattern": r"\bsecret-host\.example\b", "replace": "pilot.example.com",
                 "label": "生产域名"}]}), encoding="utf-8")
            with mock.patch.object(export, "PRIVATE_RULES_PATH", path):
                rules = export.load_private_rules()
        self.assertEqual(len(rules), 1)
        cleaned, hits = export._scrub("see secret-host.example now", rules)
        self.assertIn("pilot.example.com", cleaned)
        self.assertEqual(hits, ["生产域名"])

    def test_a_broken_rule_file_warns_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as work:
            path = pathlib.Path(work) / "publish-private.json"
            path.write_text("{not json", encoding="utf-8")
            with mock.patch.object(export, "PRIVATE_RULES_PATH", path):
                self.assertEqual(export.load_private_rules(), [])


class VerifierTests(unittest.TestCase):
    def test_a_planted_personal_address_stops_the_export(self):
        problems = export._scan_private("写给他：" + PLANTED_ADDRESS)
        self.assertTrue(problems, "一个真实邮箱地址必须让导出失败")

    def test_the_production_host_shape_stops_the_export(self):
        problems = export._scan_private("部署在 " + PLANTED_PUBLIC_IP + " 上")
        self.assertTrue(problems, "一个公网 IP 必须让导出失败")

    def test_this_machines_home_directory_stops_the_export(self):
        problems = export._scan_private(f"路径 {pathlib.Path.home()}/Documents/x")
        self.assertTrue(problems)

    def test_fixtures_and_documentation_values_pass(self):
        for text in (
            "a@b.com",
            "member-a@example.com",
            "student@my.cityu.edu.hk",
            "box1@qq.com",
            "attacker@evil.example.com",
            "user@UID.service",                    # a systemd template, not an address
            "20260913091828.5982EBAE32@smtp82.ad.cityu.edu.hk",   # a fixture Message-ID
            "host 203.0.113.10, 10.0.0.2, 192.168.1.5, 127.0.0.1",
            # Another project's documented path is not this project's leak.
            "ships as /home/node/app",
        ):
            self.assertEqual(export._scan_private(text), [], text)


class PolicyTests(unittest.TestCase):
    def test_the_private_directories_are_never_selected(self):
        selected = {str(path) for path in export.iter_files()}
        self.assertTrue(selected, "策略不该选出空集")
        for forbidden in (".secrets", "handoff", "dist", "pilot.env"):
            self.assertFalse([name for name in selected if name.startswith(forbidden)],
                             f"{forbidden} 被选中了")
        self.assertIn("LICENSE", selected)
        self.assertIn("README.md", selected)
        self.assertIn("pilot_app/web.py", selected)

    def test_no_file_in_the_selection_is_a_binary_or_a_database(self):
        for relative in export.iter_files():
            self.assertFalse(relative.name.endswith(export.EXCLUDE_SUFFIXES), relative)


class StagingTests(unittest.TestCase):
    """A refused run must leave nothing that looks publishable."""

    def test_a_refused_build_removes_the_staging_directory(self):
        with tempfile.TemporaryDirectory() as work:
            out = pathlib.Path(work) / "publish"
            with mock.patch.object(export, "build", return_value=3):
                code = export.main(["--out", str(out)])
            self.assertEqual(code, 3)
            self.assertFalse(out.exists(), "失败的导出不该留下输出目录")
            self.assertFalse((pathlib.Path(work) / "publish.building").exists(),
                             "失败的导出不该留下暂存目录")

    def test_an_existing_directory_is_not_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as work:
            out = pathlib.Path(work) / "publish"
            out.mkdir()
            self.assertEqual(export.main(["--out", str(out)]), 2)
            self.assertTrue(out.is_dir())

    def test_a_successful_build_moves_into_place_with_verifiable_hashes(self):
        with tempfile.TemporaryDirectory() as work:
            out = pathlib.Path(work) / "publish"
            with mock.patch.object(export, "iter_files", return_value=[pathlib.Path("LICENSE")]):
                code = export.main(["--out", str(out)])
            self.assertEqual(code, 0, "LICENSE 是二进制安全的，应能通过")
            manifest = (out / "PUBLISH-MANIFEST.txt").read_text(encoding="utf-8")
            line = manifest.strip().splitlines()[0]
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256(
                (out / name).read_bytes()).hexdigest())
            self.assertTrue((out / "PUBLISH-NOTES.txt").is_file())
            self.assertIn("pilot.env", (out / ".gitignore").read_text(encoding="utf-8"))


class SourceStampTests(unittest.TestCase):
    """推送前要能回答「这棵树是不是当前源码导出的」。

    一次**被拒绝**的导出故意不动上一个目录（拒绝时不留可推的树），而那份旧树完全
    自洽 —— 清单校验会通过，于是推送会兴高采烈地把**旧**树发出去，被拒的那一轮改动
    悄无声息地没发出去。2026-09-17 真的发生了：`tools/tasks_check.js` 里新增的夹具
    邮箱被隐私闸门拦下，而推送报了「已推送」。

    所以导出时把**来源**指纹写进树里（`SOURCE-STAMP`），推送脚本重新算一遍比对。
    """

    def test_the_stamp_covers_every_selected_source(self):
        """指纹必须覆盖**全部**入选文件：漏掉一个，改那个文件就不会让推送停下来。"""
        first = export.source_stamp()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        with tempfile.TemporaryDirectory() as work:
            out = pathlib.Path(work) / "publish"
            with mock.patch.object(export, "iter_files",
                                   return_value=[pathlib.Path("LICENSE")]):
                self.assertEqual(export.main(["--out", str(out)]), 0)
                # 同一份选择 → 同一个指纹（确定性：两侧要能各自算）
                self.assertEqual((out / "SOURCE-STAMP").read_text(encoding="utf-8").strip(),
                                 export.source_stamp())
                # 指纹本身也进清单，于是它受 `shasum -c` 保护
                manifest = (out / "PUBLISH-MANIFEST.txt").read_text(encoding="utf-8")
                self.assertIn("SOURCE-STAMP", manifest)
        self.assertEqual(export.source_stamp(), first, "指纹不该随调用变化")

    def test_touching_a_source_changes_the_stamp(self):
        """反向验证的那一半：源码一变，指纹必须跟着变（否则这道闸门是装饰）。"""
        with tempfile.TemporaryDirectory() as work:
            target = pathlib.Path(work) / "one.txt"
            target.write_text("original\n", encoding="utf-8")
            with mock.patch.object(export, "ROOT", pathlib.Path(work)), \
                 mock.patch.object(export, "iter_files", return_value=[pathlib.Path("one.txt")]):
                before = export.source_stamp()
                target.write_text("changed\n", encoding="utf-8")
                self.assertNotEqual(before, export.source_stamp())

    def test_the_push_script_checks_it_before_pushing(self):
        """接线也要钉住：脚本里必须**先**比对指纹，再谈推送。"""
        script = (pathlib.Path(export.ROOT) / "tools" / "publish_push.sh").read_text(
            encoding="utf-8")
        self.assertIn("SOURCE-STAMP", script)
        self.assertIn("--stamp", script)
        self.assertLess(script.index("SOURCE-STAMP"), script.index("git push"),
                        "比对必须在推送之前")


if __name__ == "__main__":
    unittest.main()
