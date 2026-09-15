"""Tests for token accounting and cost estimation.

The numbers here are money, so the tests are about two things: that the token
counts we store are the ones the provider reported (including cached and
reasoning tokens, which change the bill), and that an unknown price produces
"no cost" rather than a confident zero.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import tempfile
import unittest

from pilot_app import pricing
from pilot_app.database import Database

DEEPSEEK_USAGE = {
    "input": 778, "output": 589, "total": 1367, "cached_input": 640, "reasoning": 0,
}
OFF_PEAK = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc)   # Monday midday UTC
PEAK = dt.datetime(2026, 9, 14, 2, 0, tzinfo=dt.timezone.utc)        # Monday 02:00 UTC
WEEKEND_PEAK_HOUR = dt.datetime(2026, 9, 19, 2, 0, tzinfo=dt.timezone.utc)  # Saturday


class PriceTests(unittest.TestCase):
    def test_deepseek_cost_splits_cache_hit_and_miss(self):
        price = pricing.lookup("deepseek", "deepseek-flash")
        cost = pricing.estimate(DEEPSEEK_USAGE, price, OFF_PEAK)
        # 640 cached at $0.003/1M, 138 uncached at $0.15/1M, 589 out at $0.6/1M
        self.assertAlmostEqual(cost["input_cache_hit_cost"], 640 / 1e6 * 0.003, places=9)
        self.assertAlmostEqual(cost["input_cache_miss_cost"], 138 / 1e6 * 0.15, places=9)
        self.assertAlmostEqual(cost["output_cost"], 589 / 1e6 * 0.6, places=9)
        self.assertAlmostEqual(cost["total_cost"], 0.000376, places=6)

    def test_peak_hours_double_the_price(self):
        price = pricing.lookup("deepseek", "deepseek-flash")
        off = pricing.estimate(DEEPSEEK_USAGE, price, OFF_PEAK)
        peak = pricing.estimate(DEEPSEEK_USAGE, price, PEAK)
        self.assertFalse(off["peak"])
        self.assertTrue(peak["peak"])
        self.assertAlmostEqual(peak["total_cost"], off["total_cost"] * 2, places=9)
        # Weekends are off-peak all day, even inside the weekday peak windows.
        weekend = pricing.estimate(DEEPSEEK_USAGE, price, WEEKEND_PEAK_HOUR)
        self.assertFalse(weekend["peak"])

    def test_cached_tokens_never_exceed_the_input_total(self):
        price = pricing.lookup("deepseek", "deepseek-flash")
        weird = {"input": 100, "output": 10, "cached_input": 999}
        cost = pricing.estimate(weird, price, OFF_PEAK)
        self.assertAlmostEqual(cost["input_cache_miss_cost"], 0.0, places=9)

    def test_an_unknown_model_has_no_price_and_no_cost(self):
        """A missing price must not become a zero: a total that is silently too
        low is worse than an obvious gap."""
        self.assertIsNone(pricing.lookup("openai", "gpt-does-not-exist"))
        self.assertIsNone(pricing.estimate(DEEPSEEK_USAGE, None))
        self.assertIsNone(pricing.estimate(None, pricing.lookup("deepseek", "deepseek-flash")))

    def test_operator_override_wins_and_can_be_cleared(self):
        override = {("deepseek", "deepseek-flash"): {
            "input_cache_hit": 1.0, "input_cache_miss": 2.0, "output": 4.0, "currency": "CNY"}}
        price = pricing.lookup("deepseek", "deepseek-flash", override)
        self.assertEqual(price["currency"], "CNY")
        self.assertEqual(price["output"], 4.0)
        # A half-filled override is ignored rather than half-applied.
        broken = {("deepseek", "deepseek-flash"): {"input_cache_hit": 1.0}}
        self.assertEqual(pricing.lookup("deepseek", "deepseek-flash", broken)["output"], 0.6)


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "usage.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()
        invite = self.db.create_invite("usage-test", 1)
        from pilot_app.security import hash_password, token_hash
        self.user = self.db.create_user("usage@example.com", hash_password("a-long-enough-password"),
                                        token_hash(invite))
        self.other = self.db.create_user("other@example.com", hash_password("a-long-enough-password"),
                                         token_hash(self.db.create_invite("usage-test-2", 1)))

    def _record(self, user_id: str, usage: dict, *, kind: str = "immediate", model: str = "deepseek-flash",
                cost: dict | None = None, price: dict | None = None):
        return self.db.record_usage(user_id=user_id, kind=kind, provider="deepseek", model=model,
                                    usage=usage, cost=cost, price=price)

    def test_tokens_are_stored_per_user_per_day(self):
        price = pricing.lookup("deepseek", "deepseek-flash")
        cost = pricing.estimate(DEEPSEEK_USAGE, price, OFF_PEAK)
        self._record(self.user["id"], DEEPSEEK_USAGE, cost=cost, price=price)
        self._record(self.user["id"], {**DEEPSEEK_USAGE, "output": 411, "total": 1189},
                     kind="brief", cost=cost, price=price)
        self._record(self.other["id"], {"input": 10, "output": 5, "total": 15}, cost=None)

        page = self.db.usage_overview(days=30)
        mine = next(row for row in page["users"] if row["user_id"] == self.user["id"])
        theirs = next(row for row in page["users"] if row["user_id"] == self.other["id"])
        self.assertEqual(mine["calls"], 2)
        self.assertEqual(mine["input_tokens"], 1556)
        self.assertEqual(mine["cached_input_tokens"], 1280)
        self.assertEqual(mine["output_tokens"], 1000)
        self.assertEqual(len(mine["daily"]), 1, "同一天的两条要并成一行")
        self.assertGreater(mine["cost"], 0)
        self.assertEqual(mine["unpriced_calls"], 0)
        self.assertEqual(theirs["calls"], 1)
        self.assertEqual(theirs["unpriced_calls"], 1)
        self.assertEqual(theirs["cost"], 0.0)
        self.assertEqual(page["grand_total"]["calls"], 3)
        self.assertGreater(page["grand_total"]["cost"], 0)
        self.assertEqual(page["grand_total"]["unpriced_calls"], 1)

    def test_a_user_with_no_calls_is_not_reported_as_unpriced(self):
        """Every account is listed, including idle ones. The LEFT JOIN gives an
        idle user one all-NULL row, which must not read as "1 call we could not
        price" — that made an untouched account look like a billing problem."""
        price = pricing.lookup("deepseek", "deepseek-flash")
        self._record(self.user["id"], DEEPSEEK_USAGE, cost=pricing.estimate(DEEPSEEK_USAGE, price, OFF_PEAK), price=price)
        page = self.db.usage_overview(days=30)
        idle = next(row for row in page["users"] if row["user_id"] == self.other["id"])
        self.assertEqual(idle["calls"], 0)
        self.assertEqual(idle["unpriced_calls"], 0)
        self.assertEqual(idle["total_tokens"], 0)
        self.assertEqual(idle["cost"], 0.0)
        self.assertEqual(page["grand_total"]["unpriced_calls"], 0)

    def test_the_price_at_call_time_is_frozen(self):
        """Editing a price must not rewrite what yesterday cost."""
        cheap = {"input_cache_hit": 0.003, "input_cache_miss": 0.15, "output": 0.6,
                 "currency": "USD", "source": "test"}
        self._record(self.user["id"], DEEPSEEK_USAGE,
                     cost=pricing.estimate(DEEPSEEK_USAGE, cheap, OFF_PEAK), price=cheap)
        self.db.set_model_price("deepseek", "deepseek-flash", input_cache_hit=99, input_cache_miss=99, output=99)
        page = self.db.usage_overview(days=30)
        mine = next(row for row in page["users"] if row["user_id"] == self.user["id"])
        self.assertLess(mine["cost"], 0.01, "改价不应改写历史成本")
        with self.db.connect() as connection:
            stored = connection.execute(
                "SELECT price_json FROM token_usage WHERE user_id=?", (self.user["id"],)).fetchone()[0]
        self.assertIn("0.6", stored, "当时用的价目要冻结在记录里")

    def test_days_window_excludes_older_calls(self):
        from pilot_app.database import utc_now
        self._record(self.user["id"], DEEPSEEK_USAGE)
        with self.db.connect() as connection:
            connection.execute("UPDATE token_usage SET created_at=?",
                               ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat(timespec="seconds"),))
        recent = self.db.usage_overview(days=7)
        older = self.db.usage_overview(days=30)
        mine_recent = next(row for row in recent["users"] if row["user_id"] == self.user["id"])
        mine_older = next(row for row in older["users"] if row["user_id"] == self.user["id"])
        self.assertEqual(mine_recent["calls"], 0)
        self.assertEqual(mine_older["calls"], 1)

    def test_price_overrides_round_trip(self):
        self.assertEqual(self.db.list_model_prices(), [])
        self.db.set_model_price("deepseek", "deepseek-chat", input_cache_hit=0.5,
                                input_cache_miss=1.5, output=3.0, currency="CNY")
        rows = self.db.list_model_prices()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["currency"], "CNY")
        page = self.db.usage_overview(days=1)
        self.assertEqual(len(page["users"]), 2)
        self.db.delete_model_price("deepseek", "deepseek-chat")
        self.assertEqual(self.db.list_model_prices(), [])


class UsageEndpointTests(unittest.TestCase):
    """The route itself: operator-only, and honest about unpriced calls."""

    @classmethod
    def setUpClass(cls):
        import http.cookiejar
        import os
        import threading
        import urllib.error
        import urllib.request

        cls._tmp = tempfile.mkdtemp()
        os.environ["INFE_PILOT_DB"] = cls._tmp + "/web.sqlite3"
        os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
        os.environ["INFE_PILOT_MAX_USERS"] = "50"
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = "boss@example.com"
        from pilot_app import web

        cls.web = web
        cls.client_cls = None
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        import http.cookiejar
        import urllib.error
        import urllib.request

        self.db = self.web.db
        for table in ("announcement_deliveries", "announcement_dismissals", "announcements",
                      "token_usage", "model_prices", "feedback", "reports", "messages",
                      "mailboxes", "connections", "sessions", "invites", "profiles", "users"):
            with self.db.connect() as connection:
                connection.execute(f"DELETE FROM {table}")

        class Client:
            def __init__(inner, base):
                inner.base = base
                inner.opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

            def request(inner, method, path, payload=None):
                data = json.dumps(payload).encode("utf-8") if payload is not None else None
                req = urllib.request.Request(inner.base + path, data=data, method=method)
                req.add_header("Content-Type", "application/json")
                try:
                    with inner.opener.open(req, timeout=20) as response:
                        return response.status, json.loads(response.read().decode() or "{}")
                except urllib.error.HTTPError as error:
                    raw = error.read().decode()
                    try:
                        return error.code, json.loads(raw or "{}")
                    except json.JSONDecodeError:
                        return error.code, {"detail": raw}

            def get(inner, path):
                return inner.request("GET", path)

            def put(inner, path, payload=None):
                return inner.request("PUT", path, payload)

            def post(inner, path, payload=None):
                return inner.request("POST", path, payload)

        self.client_cls = Client
        self.client = Client(self.base)

    def _login(self, email: str):
        invite = self.db.create_invite(f"invite-{email}-{dt.datetime.now().timestamp()}", 1)
        status, body = self.client.post("/api/auth/register", {
            "email": email, "password": "a-long-enough-password", "invite_code": invite, "accepted_terms": True})
        self.assertEqual(status, 200, body)
        return self.client

    def test_operator_sees_usage_and_can_set_a_price(self):
        self._login("boss@example.com")
        status, body = self.client.get("/api/admin/usage?days=30")
        self.assertEqual(status, 200, body)
        self.assertIn("grand_total", body)
        self.assertIn("known_prices", body)
        self.assertTrue(any(row["model"] == "deepseek-flash" for row in body["known_prices"]))

        status, saved = self.client.put("/api/admin/prices", {
            "provider": "deepseek", "model": "deepseek-chat",
            "input_cache_hit": 0.5, "input_cache_miss": 1.5, "output": 3.0, "currency": "CNY"})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["prices"][0]["currency"], "CNY")

        status, bad = self.client.put("/api/admin/prices", {
            "provider": "deepseek", "model": "x", "input_cache_hit": "abc",
            "input_cache_miss": 1, "output": 1})
        self.assertEqual(status, 422, bad)

        status, removed = self.client.put("/api/admin/prices", {
            "provider": "deepseek", "model": "deepseek-chat", "remove": True})
        self.assertEqual(removed["prices"], [])

    def test_usage_is_operator_only(self):
        self._login("member-priv@example.com")
        status, _ = self.client.get("/api/admin/usage")
        self.assertEqual(status, 404)
        status, _ = self.client.put("/api/admin/prices", {"provider": "a", "model": "b"})
        self.assertEqual(status, 404)
        status, _ = self.client_cls(self.base).get("/api/admin/usage")
        self.assertEqual(status, 401)

    def test_price_changes_are_audited(self):
        self._login("boss@example.com")
        self.client.put("/api/admin/prices", {
            "provider": "deepseek", "model": "m1", "input_cache_hit": 1, "input_cache_miss": 1, "output": 1})
        actions = {row["action"] for row in self.db.list_audit(20)}
        self.assertIn("price_set", actions)


if __name__ == "__main__":
    unittest.main()
