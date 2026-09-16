# -*- coding: utf-8 -*-
"""Tests for the read-only demo at `/demo`.

The demo's value is that it is the **real** interface with fabricated data, and
its safety is that it has no way to reach anything real. Both halves are pinned
here: the fixture must stay generic (no address of ours, no address of anybody's),
and the page must not create a session or call the API.
"""

import datetime as dt
import http.cookiejar
import json
import os
import pathlib
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/demo.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import demo  # noqa: E402
from pilot_app import web  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path):
        try:
            with self.opener.open(self.base + path, timeout=20) as response:
                return response.status, response.read().decode("utf-8", "replace"), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace"), dict(error.headers)


class FixtureTests(unittest.TestCase):
    """What the fixture is allowed to contain."""

    def test_it_carries_exactly_the_endpoints_the_demo_claims(self):
        payload = demo.responses()
        self.assertEqual(set(payload["responses"]), set(demo.PATHS))
        self.assertEqual(set(demo.SECTIONS), {"dashboard", "reports"})
        self.assertTrue(payload["readOnly"])

    def test_no_address_of_ours_or_anybody_elses(self):
        """A demo that leaked a real address would be the worst kind of bug."""
        blob = json.dumps(demo.responses(), ensure_ascii=False)
        addresses = set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", blob))
        self.assertTrue(addresses, "夹具里应该有邮箱，否则这条测试没在测东西")
        # `student@my.cityu.edu.hk` is allowed and is the point: it is this
        # project's own placeholder for a CityU address (the publish exporter
        # replaces every real one with exactly this), so the demo shows what the
        # field looks like without naming anybody.
        for address in addresses:
            self.assertTrue(
                address.endswith("@example.com") or address == "student@my.cityu.edu.hk",
                address)
        for secret in ("203.0.113.10", "sslip.io"):
            self.assertNotIn(secret, blob, secret)

    def test_it_holds_no_key_shaped_string(self):
        blob = json.dumps(demo.responses(), ensure_ascii=False)
        self.assertNotIn("INFE_PILOT_MASTER_KEY", blob)
        self.assertIsNone(re.search(r"\bsk-[A-Za-z0-9]{16,}", blob))

    def test_two_calls_do_not_share_objects(self):
        """The page renders two sections from one fixture; aliasing would let the
        first one's mutations show up in the second."""
        first = demo.responses()["responses"]
        second = demo.responses()["responses"]
        first["/api/tasks"]["tasks"].append({"fake": True})
        self.assertNotIn({"fake": True}, second["/api/tasks"]["tasks"])


class DateShiftTests(unittest.TestCase):
    def test_the_dates_move_to_today(self):
        """"今天要处理的事" dated last month reads as a broken product."""
        later = dt.datetime(2026, 10, 1, 3, 0, tzinfo=dt.timezone.utc)
        payload = demo.payload(later)
        self.assertEqual(payload["/api/tasks"]["day"], "2026-10-01")
        self.assertEqual(payload["/api/dashboard"]["local_date"], "2026-10-01")
        self.assertTrue(payload["/api/tasks"]["is_today"])
        # The deadline inside a task line moved with it: 09-18 → 10-03.
        self.assertIn("2026-10-03", payload["/api/tasks"]["tasks"][0]["action"])

    def test_the_chinese_date_form_moves_too(self):
        later = dt.datetime(2026, 9, 26, 3, 0, tzinfo=dt.timezone.utc)
        blob = json.dumps(demo.payload(later), ensure_ascii=False)
        self.assertIn("9月26日", blob)
        self.assertNotIn("9月16日", blob)

    def test_nothing_moves_before_the_capture_date(self):
        """A clock behind the capture date must not produce negative dates."""
        earlier = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(demo.days_since_capture(earlier), 0)
        self.assertEqual(demo.payload(earlier)["/api/tasks"]["day"], demo.CAPTURED_ON)

    def test_shift_leaves_text_alone(self):
        self.assertEqual(demo.shift("没有日期的一段话", 5), "没有日期的一段话")


class DemoRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_the_page_is_the_real_shell_in_demo_mode(self):
        status, body, headers = Client(self.base).get("/demo")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("/demo-data.js", body)
        self.assertIn("/app.js", body)
        self.assertLess(body.index("/demo-data.js"), body.index("/app.js"),
                        "数据脚本必须在 app.js 之前，否则 boot 时读不到")
        self.assertNotIn("window.PILOT_DEMO=", body,
                         "内联脚本会被 CSP（script-src 'self'）静默拦掉，必须走外部文件")

    def test_the_data_is_a_separate_uncached_script(self):
        status, body, headers = Client(self.base).get("/demo-data.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store",
                         "缓存住的话日期会退回到捕获那天")
        self.assertTrue(body.startswith("window.PILOT_DEMO="))
        self.assertTrue(body.rstrip().endswith(";"))
        payload = json.loads(body.strip()[len("window.PILOT_DEMO="):-1])
        self.assertEqual(set(payload["responses"]), set(demo.PATHS))

    def test_visiting_the_demo_does_not_log_anybody_in(self):
        """No session, and none created: the next request is still anonymous."""
        client = Client(self.base)
        client.get("/demo")
        client.get("/demo-data.js")
        status, _, _ = client.get("/api/me")
        self.assertEqual(status, 401, "演示不能顺手给访客一个会话")

    def test_the_demo_is_not_blocked_from_search_engines(self):
        """The opposite treatment from `/app`: this one is worth finding."""
        status, body, _ = Client(self.base).get("/robots.txt")
        self.assertEqual(status, 200)
        self.assertNotIn("Disallow: /demo", body)
        self.assertIn("Disallow: /app", body)


class ClientContractTests(unittest.TestCase):
    """The two halves have to keep matching, and a shell edit could break it."""

    def setUp(self):
        self.app_js = (ROOT / "pilot_app" / "static" / "app.js").read_text(encoding="utf-8")

    def test_the_api_helper_short_circuits_before_fetching(self):
        match = re.search(r"async function api\(path, options = \{\}\) \{\n(.*?)\n", self.app_js)
        self.assertIsNotNone(match, "找不到 api()")
        self.assertIn("window.PILOT_DEMO", match.group(1),
                      "api() 必须在 fetch 之前就把演示挡掉")

    def test_a_write_is_refused_rather_than_faked(self):
        """A button that looks like it worked is worse than one that says no."""
        self.assertIn("只读演示", self.app_js)
        self.assertIn("method !== 'GET'", self.app_js)

    def test_the_banner_says_the_data_is_made_up(self):
        self.assertIn("数据是编的", self.app_js)

    def test_the_shell_links_to_the_demo(self):
        landing = (ROOT / "pilot_app" / "static" / "landing.html").read_text(encoding="utf-8")
        self.assertIn('href="/demo"', landing, "官网上没有任何入口指向演示")


if __name__ == "__main__":
    unittest.main()
