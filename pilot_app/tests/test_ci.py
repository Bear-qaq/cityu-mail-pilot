# -*- coding: utf-8 -*-
"""Tests for the CI setup and for the one thing that made CI impossible.

The browser suites used to open with a hard-coded
``require('/tmp/pw/node_modules/playwright')``: the scratch directory this
project installed Playwright into once, on one laptop. Everything else about the
suites was portable; that line was not, and it is why "run the checks" only ever
meant "run the checks on the machine where they were written".

The tests below are mostly about **reachability**, because that is the failure
mode a CI configuration has: a workflow that is not published never runs, one
that repeats the suite list drifts from it, and one that can reach production is
a liability rather than a check. None of these fail loudly on their own -- the
build is green either way.
"""

import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TOOLS = ROOT / "tools"


class PlaywrightResolutionTests(unittest.TestCase):
    def test_nothing_hard_codes_the_scratch_install(self):
        """One line, once, decided that 20 suites could only run in one place."""
        offenders = sorted(
            path.name for path in TOOLS.glob("*.js")
            if "/tmp/pw/node_modules/playwright" in path.read_text(encoding="utf-8")
            # pw.js is allowed to name it: it is the file whose job is to look
            # there. Nothing else may.
            and path.name != "pw.js"
        )
        self.assertEqual(offenders, [], f"这些文件又写死了 Playwright 的路径：{offenders}")

    def test_every_suite_that_drives_a_browser_goes_through_the_resolver(self):
        users = [path.name for path in TOOLS.glob("*.js")
                 if "require('./pw')" in path.read_text(encoding="utf-8")
                 and path.name != "pw.js"]
        self.assertGreaterEqual(len(users), 20, f"只有 {len(users)} 个文件用了解析器")
        self.assertIn("landing_check.js", users)
        self.assertIn("metrics_check.js", users)

    def test_the_resolver_still_offers_the_documented_locations(self):
        text = (TOOLS / "pw.js").read_text(encoding="utf-8")
        self.assertIn("'playwright'", text, "普通 node_modules 必须排在第一位")
        self.assertIn("node_modules", text)
        # A resolver that fails with MODULE_NOT_FOUND teaches the reader nothing.
        self.assertIn("npx playwright install chromium", text)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(WORKFLOW.is_file(), f"缺少 {WORKFLOW}")
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_the_workflow_is_published_so_it_can_ever_run(self):
        """Not runnable from the repo it lives in is the same as not existing."""
        import sys
        sys.path.insert(0, str(ROOT))
        from tools import publish_export as export

        self.assertIn(".github", export.INCLUDE_DIRS)
        selected = {str(path) for path in export.iter_files()}
        self.assertIn(".github/workflows/ci.yml", selected)

    def test_it_runs_the_suite_script_instead_of_repeating_the_suite_list(self):
        """Two lists of 20 names drift; the second one drifts silently."""
        self.assertIn("tools/run_browser_checks.sh", self.text)
        runner = (TOOLS / "run_browser_checks.sh").read_text(encoding="utf-8")
        suites = re.findall(r"^\s{2}([a-z_]+_check)$", runner, re.MULTILINE)
        self.assertGreaterEqual(len(suites), 20)
        for name in suites:
            self.assertNotIn(
                f"{name}.js", self.text,
                f"工作流里又抄了一遍套件清单（{name}）——应该只调用 run_browser_checks.sh",
            )

    def test_the_python_matrix_covers_the_two_pythons_this_project_runs_on(self):
        server = re.search(r'python:\s*\[([^\]]*)\]', self.text)
        self.assertIsNotNone(server, "工作流里没有 python 矩阵")
        versions = {part.strip().strip('"\'') for part in server.group(1).split(",")}
        self.assertIn("3.14", versions, "生产是 3.14")
        self.assertIn("3.9", versions, "开发机是 3.9，而它只靠 future annotations 撑着")

    def test_ci_cannot_touch_production(self):
        """A pipeline that needs a secret is not one a contributor can run.

        Deploying is an operator action with a backup and a rollback attached;
        it has no business happening because someone opened a pull request.
        """
        for forbidden in ("secrets.", "ssh ", "scp ", "appleboy", "deploy_pilot.sh"):
            self.assertNotIn(forbidden, self.text, f"工作流里出现了 {forbidden!r}")
        self.assertIn("permissions:", self.text)
        self.assertIn("contents: read", self.text)

    def test_it_installs_only_the_browser_the_suites_use(self):
        """All 20 suites drive chromium; pulling three browsers triples the
        slowest step of the job for nothing."""
        self.assertIn("install --with-deps chromium", self.text)
        for unused in ("install --with-deps firefox", "install --with-deps webkit",
                       "playwright install firefox", "playwright install webkit"):
            self.assertNotIn(unused, self.text)

    def test_an_npm_install_inside_tools_cannot_be_published(self):
        """CI teaches people to run `npm install`, and npm installs where you
        run it. Without this exclusion, one `npm install` typed in tools/ would
        put tens of thousands of foreign files into the public tree."""
        import sys
        sys.path.insert(0, str(ROOT))
        from tools import publish_export as export

        self.assertIn("node_modules", export.EXCLUDE_NAMES)


class BuildScriptPortabilityTests(unittest.TestCase):
    """The first CI run failed here, and it was the same bug as `/tmp/pw`.

    `build_release.sh` read the version with ``"$ROOT_DIR/.venv-pilot/bin/python"``
    and wrote the checksum with ``shasum``. Both are facts about one laptop:
    on any other machine the build died on line 5, and on Linux `shasum` is a
    Perl script that may simply not be installed. A release script nobody else
    can run is the thing CI exists to notice.
    """

    def setUp(self):
        self.text = (ROOT / "pilot_app" / "build_release.sh").read_text(encoding="utf-8")

    def test_it_looks_for_an_interpreter_instead_of_naming_one(self):
        self.assertIn("find_python", self.text)
        self.assertIn("python3", self.text, "必须退到 python3，否则只有开发机能打包")
        # The developer's venv may be *tried*, but not relied on.
        self.assertIn('"${PYTHON:-}"', self.text)

    def test_the_checksum_tool_falls_back_to_coreutils(self):
        self.assertIn("sha256sum", self.text, "Linux 上要用 sha256sum")
        self.assertIn("shasum -a 256", self.text, "macOS 上要用 shasum")


def _needs_files_the_package_does_not_ship(path: pathlib.Path) -> bool:
    """Does this test module depend on the repository rather than the product?

    Derived from the source instead of a hand-written list, because a
    hand-written list is exactly what goes stale: the next test file to import
    `tools/` would silently join the release package and break it.
    """
    text = path.read_text(encoding="utf-8")
    if ".github" in text:
        return True
    tree = ast.parse(text)
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        if any(name == "tools" or name.startswith("tools.") for name in names):
            return True
    # The other shape: two modules load their tool by path,
    # `spec_from_file_location("x", ROOT / "tools" / "x.py")`, so there is no
    # import statement to find. Requiring both halves matters: `"tools"` alone is
    # an ordinary payload key in test_providers (the model API takes a list of
    # tools), and treating that as a dependency on tools/ would drag a perfectly
    # package-runnable test out of the package.
    return "spec_from_file_location" in text and any(
        isinstance(node, ast.Constant) and node.value == "tools" for node in ast.walk(tree)
    )


class ReleasePackageTests(unittest.TestCase):
    """The runtime package must not ship a test suite that cannot pass in it.

    The first CI run of the release job unpacked the tarball and ran the suite
    inside it: six modules failed to import, because they test `tools/` and the
    package ships `pilot_app/` only. A package whose own tests fail on unpack is
    worse than one with no tests -- it teaches the reader to ignore red.
    """

    def setUp(self):
        self.script = (ROOT / "pilot_app" / "build_release.sh").read_text(encoding="utf-8")

    def _excluded(self) -> set[str]:
        head = self.script.split("REPO_ONLY_TESTS=(", 1)[1].split(")", 1)[0]
        return set(re.findall(r"(test_[a-z_]+\.py)", head))

    def _repo_only(self) -> set[str]:
        return {path.name for path in (ROOT / "pilot_app" / "tests").glob("test_*.py")
                if _needs_files_the_package_does_not_ship(path)}

    def test_every_repo_only_test_is_kept_out_of_the_package(self):
        repo_only = self._repo_only()
        self.assertTrue(repo_only, "一个都认不出来，说明这个判定坏了")
        missing = sorted(repo_only - self._excluded())
        self.assertEqual(missing, [], f"这些测试要仓库级文件，却没被排除出发布包：{missing}")

    def test_the_exclusion_list_has_no_stale_entries(self):
        """An entry naming a file that no longer needs excluding is a comment
        pretending to be a rule.

        Only files that are actually here count. `test_handoff.py` is not
        published -- it tests a tool that is not published either -- so it is
        present in the development tree and absent from the public one, and this
        test runs in both. Comparing against the directory listing alone made the
        list look stale in exactly one of the two, which is how the first version
        of this check failed CI on the public tree.
        """
        tests = ROOT / "pilot_app" / "tests"
        repo_only = self._repo_only()
        stale = sorted(name for name in self._excluded()
                       if (tests / name).exists() and name not in repo_only)
        self.assertEqual(stale, [], f"这些文件已经不需要排除了：{stale}")

    def test_the_exclusions_actually_reach_tar(self):
        self.assertIn('"${REPO_ONLY_EXCLUDES[@]}"', self.script)


if __name__ == "__main__":
    unittest.main()
