import datetime as dt
import secrets
import unittest
from unittest import mock

from pilot_app import providers
from pilot_app import service as service_mod
from pilot_app.security import SecretBox
from pilot_app.service import PilotService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = mock.MagicMock()
        self.box = SecretBox(secrets.token_bytes(32))
        self.service = PilotService(self.db, self.box)

    def test_filtered_generated_messages_still_advance_imap_cursor(self):
        mailbox = {
            "id": "mbx", "user_id": "usr", "last_uid": 10, "uid_validity": "123",
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        with mock.patch("pilot_app.service.mailio.fetch_new_messages", return_value=("123", [], 42)):
            count = self.service.poll_mailbox(mailbox)
        self.assertEqual(count, 0)
        self.db.update_mailbox_poll.assert_called_once_with("mbx", last_uid=42, uid_validity="123")

    def test_failed_smtp_retry_reuses_generated_report_without_second_model_call(self):
        message = {"id": "msg", "user_id": "usr", "subject": "Course", "attempts": 1}
        mailbox = {
            "id": "mbx", "user_id": "usr", "email": "me@example.com", "report_to": "me@example.com",
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.mark_message_processing.return_value = True
        self.db.get_mailbox.return_value = mailbox
        self.db.get_profile.return_value = {"timezone": "Asia/Hong_Kong"}
        self.db.report_for_message.return_value = {
            "id": "rpt", "status": "failed", "subject": "summary", "body_markdown": "existing report",
        }
        with mock.patch.object(self.service, "_analyse") as analyse, mock.patch("pilot_app.service.mailio.send_report") as send:
            self.assertTrue(self.service.process_message(message))
        analyse.assert_not_called()
        self.assertEqual(send.call_args.args, (mailbox, "pw", "summary", "existing report"))
        rendered = send.call_args.kwargs
        self.assertIn("你应该做什么", rendered["html_body"])
        self.assertIn("邮件讲了什么", rendered["html_body"])
        self.assertIn("existing report", rendered["text_body"])
        self.db.mark_report_sent.assert_called_once_with("rpt")
        self.db.finish_message.assert_called_once_with("msg")

    def test_process_message_tolerates_missing_message_fields(self):
        """Worker rows can lack optional columns; rendering must not explode."""
        message = {"id": "msg2", "user_id": "usr", "subject": "Bare", "attempts": 0,
                   "sender_name": "", "sender_address": "", "received_at": "2026-09-13T00:00:00+00:00",
                   "importance": "normal", "body": self.box.encrypt("raw", context="message:usr")}
        mailbox = {
            "id": "mbx", "user_id": "usr", "email": "me@example.com", "report_to": "me@example.com",
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.mark_message_processing.return_value = True
        self.db.get_mailbox.return_value = mailbox
        self.db.get_profile.return_value = {}
        self.db.report_for_message.return_value = None
        self.db.messages_between.return_value = []
        with mock.patch.object(self.service, "_analyse", return_value="## 1. 重要程度\n- 等级：高"), \
                mock.patch("pilot_app.service.mailio.send_report"):
            self.assertTrue(self.service.process_message(message))

    def test_daily_retry_reuses_report_and_uses_hong_kong_day_window(self):
        user = {"id": "usr", "timezone": "Asia/Hong_Kong", "report_to": "me@example.com"}
        mailbox = {
            "id": "mbx", "user_id": "usr", "email": "me@example.com", "report_to": "me@example.com",
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.get_profile.return_value = {}
        self.db.daily_report_for_date.return_value = {
            "id": "daily", "status": "failed", "subject": "daily summary", "body_markdown": "existing daily",
        }
        self.db.get_mailbox.return_value = mailbox
        self.db.messages_between.return_value = []
        with mock.patch("pilot_app.service.providers.generate_text") as generate, mock.patch("pilot_app.service.mailio.send_report"):
            self.assertTrue(self.service.send_daily(user, "2026-09-13"))
        generate.assert_not_called()
        args = self.db.messages_between.call_args.args
        self.assertEqual(args[1], "2026-09-12T16:00:00+00:00")
        self.assertEqual(args[2], "2026-09-13T16:00:00+00:00")
        self.db.mark_report_sent.assert_called_once_with("daily")

    def test_daily_digest_needs_no_model_and_never_drops_a_message(self):
        """The 22:00 brief is composed locally so no mail can be lost by a model."""
        user = {"id": "usr", "timezone": "Asia/Hong_Kong", "report_to": "me@example.com"}
        mailbox = {
            "id": "mbx", "user_id": "usr", "email": "me@example.com", "report_to": "me@example.com",
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.get_profile.return_value = {}
        self.db.daily_report_for_date.return_value = None
        self.db.get_mailbox.return_value = mailbox
        good = "## 1. 重要程度\n- 等级：高\n- 结论：要交作业\n## 2. 必须采取的行动\n- 周五前提交"
        rows = [
            {"id": "m1", "subject": "Course deadline", "sender_name": "Teacher", "sender_address": "t@x.hk",
             "received_at": "2026-09-13T02:00:00+00:00", "importance": "normal", "status": "sent",
             "last_error": "", "body_markdown": self.box.encrypt(good, context="report:usr")},
            {"id": "m2", "subject": "Promo", "sender_name": "Shop", "sender_address": "s@x.hk",
             "received_at": "2026-09-13T03:00:00+00:00", "importance": "low", "status": "failed",
             "last_error": "smtp down", "body_markdown": None},
        ]
        self.db.messages_between.return_value = rows
        with mock.patch("pilot_app.service.providers.generate_text") as generate, \
                mock.patch("pilot_app.service.mailio.send_report") as send:
            self.assertTrue(self.service.send_daily(user, "2026-09-13"))
        generate.assert_not_called()
        html = send.call_args.kwargs["html_body"]
        text = send.call_args.kwargs["text_body"]
        for subject in ("Course deadline", "Promo"):
            self.assertIn(subject, html)
            self.assertIn(subject, text)
        self.assertIn("smtp down", html)
        self.assertIn("今天有 1 件事需要处理", html)


    # -- native search replaces the second API key --------------------------

    def _model_connection(self, provider):
        return {
            "kind": "model", "user_id": "usr", "provider": provider, "model": "test-model", "base_url": "",
            "enabled": 1, "config_json": "{}",
            "encrypted_api_key": self.box.encrypt("model-key", context="connection:usr:model"),
        }

    def test_analyse_prefers_native_search_and_skips_external_api(self):
        self.db.get_profile.return_value = {}
        sources = [{"title": "Source", "url": "https://example.com/a", "summary": ""}]
        self.db.get_connection.side_effect = lambda user_id, kind: (
            self._model_connection("openai") if kind == "model" else None
        )
        with mock.patch(
            "pilot_app.service.providers.generate",
            return_value=providers.Generation("## 1. 邮件内容总结\nsee https://example.com/a", sources, "native"),
        ) as generate, mock.patch("pilot_app.service.providers.web_search") as web_search:
            report = self.service._analyse("usr", {"subject": "Course update", "body": "hello"})
        web_search.assert_not_called()
        self.assertTrue(generate.call_args.kwargs["native_search"])
        self.assertIsInstance(report, str)
        self.assertIn("https://example.com/a", report)

    def test_analyse_survives_native_search_failure(self):
        self.db.get_profile.return_value = {}
        self.db.get_connection.side_effect = lambda user_id, kind: (
            self._model_connection("anthropic") if kind == "model" else None
        )

        def fake_generate(**kwargs):
            if kwargs.get("native_search"):
                raise providers.ProviderError("native search exploded")
            return providers.Generation("## 3. 邮件内容总结\nno live verification", [], "none")

        with mock.patch("pilot_app.service.providers.generate", side_effect=fake_generate) as generate:
            report = self.service._analyse("usr", {"subject": "Course update", "body": "hello"})
        self.assertIsInstance(report, str)
        self.assertTrue(report.strip())
        # First attempt used native search, the retry did not.
        self.assertTrue(generate.call_args_list[0].kwargs["native_search"])
        self.assertFalse(generate.call_args_list[1].kwargs.get("native_search"))

    def test_analyse_still_uses_external_search_for_plain_providers(self):
        self.db.get_profile.return_value = {}
        connections = {
            "model": self._model_connection("deepseek"),
            "search": {
                "kind": "search", "user_id": "usr", "provider": "tavily", "enabled": 1,
                "encrypted_api_key": self.box.encrypt("search-key", context="connection:usr:search"),
            },
        }
        self.db.get_connection.side_effect = lambda user_id, kind: connections.get(kind)
        hits = [{"title": "Source", "url": "https://example.com/a", "summary": ""}]
        with mock.patch("pilot_app.service.providers.web_search", return_value=hits) as web_search, \
                mock.patch("pilot_app.service.providers.generate",
                           return_value=providers.Generation("## 3. 邮件内容总结\nsee https://example.com/a", [], "none")) as generate:
            self.service._analyse("usr", {"subject": "Course update", "body": "hello"})
        web_search.assert_called_once()
        self.assertFalse(generate.call_args.kwargs.get("native_search"))


    def test_native_failure_falls_back_to_external_search(self):
        self.db.get_profile.return_value = {}
        connections = {
            "model": self._model_connection("gemini"),
            "search": {
                "kind": "search", "user_id": "usr", "provider": "tavily", "enabled": 1,
                "encrypted_api_key": self.box.encrypt("search-key", context="connection:usr:search"),
            },
        }
        self.db.get_connection.side_effect = lambda user_id, kind: connections.get(kind)

        def fake_generate(**kwargs):
            if kwargs.get("native_search"):
                raise providers.ProviderError("native search unavailable")
            return providers.Generation("## 5. 联网搜索\nsee https://example.com/e", [], "none")

        hits = [{"title": "Source", "url": "https://example.com/e", "summary": ""}]
        with mock.patch("pilot_app.service.providers.generate", side_effect=fake_generate), \
                mock.patch("pilot_app.service.providers.web_search", return_value=hits) as web_search:
            report = self.service._analyse("usr", {"subject": "Course update", "body": "hello"})
        web_search.assert_called_once()
        self.assertIn("https://example.com/e", report)

    def test_model_test_rejects_an_empty_answer(self):
        """The connection test used to pass on an empty answer, which is how a
        reasoning model that answered every real prompt with "" looked healthy."""
        self.db.get_connection.return_value = {
            "user_id": "usr", "kind": "model",
            "provider": "deepseek", "model": "deepseek-flash", "base_url": "",
            "config_json": "{}", "enabled": 1,
            "encrypted_api_key": self.box.encrypt("k", context="connection:usr:model"),
        }
        with mock.patch("pilot_app.service.providers.generate_text", return_value="   "):
            with self.assertRaises(providers.ProviderError) as caught:
                self.service.test_model("usr")
        self.assertIn("没有返回任何文本", str(caught.exception))

    def test_model_test_accepts_a_real_answer(self):
        self.db.get_connection.return_value = {
            "user_id": "usr", "kind": "model",
            "provider": "deepseek", "model": "deepseek-chat", "base_url": "",
            "config_json": "{}", "enabled": 1,
            "encrypted_api_key": self.box.encrypt("k", context="connection:usr:model"),
        }
        with mock.patch("pilot_app.service.providers.generate_text", return_value="连接成功"):
            self.assertEqual(self.service.test_model("usr"), "连接成功")

    # -- transient provider failures ---------------------------------------

    def test_transient_model_failure_is_retried_once(self):
        """A dropped long generation must not lose the email."""
        calls = []

        def flaky(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise providers.TransientProviderError("接口连接被中断（长回答可能超时）")
            return providers.Generation("## 3. 内容\nok", [], "none")

        with mock.patch("pilot_app.service.time.sleep") as pause, \
                mock.patch("pilot_app.service.providers.generate", side_effect=flaky):
            result = self.service._generate_with_retry("usr", provider="deepseek", model="m", api_key="k", prompt="p")
        self.assertEqual(result.text, "## 3. 内容\nok")
        self.assertEqual(len(calls), 2)
        pause.assert_called_once()

    def test_permanent_model_failure_is_not_retried(self):
        with mock.patch("pilot_app.service.providers.generate",
                        side_effect=providers.ProviderError("API 返回 HTTP 401: bad key")) as generate:
            with self.assertRaises(providers.ProviderError):
                self.service._generate_with_retry("usr", provider="deepseek", model="m", api_key="k", prompt="p")
        self.assertEqual(generate.call_count, 1)

    def test_a_timeout_is_not_retried_immediately(self):
        """A timeout already spent the whole budget (~234 s of a 300 s ceiling
        for a real report), so retrying it would hold that generation slot for
        twice as long. The message is marked failed and the queue's backoff
        retries it instead."""
        with mock.patch("pilot_app.service.providers.generate",
                        side_effect=providers.ProviderTimeout("接口响应超时")) as generate:
            with self.assertRaises(providers.ProviderTimeout):
                self.service._generate_with_retry("usr", provider="deepseek", model="m", api_key="k", prompt="p")
        self.assertEqual(generate.call_count, 1, "超时不应立即重试")
        # Still classified as transient, so the rest of the system treats it as
        # a retryable failure rather than a permanent one.
        self.assertTrue(self.service._transient(providers.ProviderTimeout("x")))
        self.assertTrue(issubclass(providers.ProviderTimeout, providers.TransientProviderError))

    def test_retry_gives_up_after_the_configured_attempts(self):
        with mock.patch.dict("os.environ", {"INFE_PILOT_MODEL_ATTEMPTS": "3"}), \
                mock.patch("pilot_app.service.time.sleep"), \
                mock.patch("pilot_app.service.providers.generate",
                           side_effect=providers.TransientProviderError("超时")) as generate:
            with self.assertRaises(providers.TransientProviderError):
                self.service._generate_with_retry("usr", provider="deepseek", model="m", api_key="k", prompt="p")
        self.assertEqual(generate.call_count, 3)

    def test_transient_classification(self):
        self.assertTrue(issubclass(providers.TransientProviderError, providers.ProviderError))
        self.assertTrue(self.service._transient(TimeoutError("timed out")))
        self.assertFalse(self.service._transient(ValueError("not transient")))

    # -- sender allow-list at ingestion ------------------------------------

    def test_poll_skips_non_allowed_senders_without_queueing_them(self):
        """Personal mail is stored and marked, but never becomes a report."""
        mailbox = {
            "id": "mbx", "user_id": "usr", "last_uid": 10, "uid_validity": "123",
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        allowed = {"subject": "Tutorial notice", "sender_name": "CityU", "sender_address": "student@my.cityu.edu.hk",
                   "received": "2026-09-13T02:00:00+00:00", "importance": "normal", "body": "b",
                   "message_key": "<a@cityu>"}
        personal = {"subject": "50% off", "sender_name": "Grammarly", "sender_address": "hello@mail.grammarly.com",
                    "received": "2026-09-13T02:01:00+00:00", "importance": "normal", "body": "b",
                    "message_key": "<b@grammarly>"}
        self.db.insert_message.return_value = "msg_ok"
        with mock.patch.object(service_mod, "ALLOWED_SENDER_DOMAINS", ("cityu.edu.hk",)), \
                mock.patch("pilot_app.service.mailio.fetch_new_messages",
                           return_value=("123", [(11, allowed), (12, personal)], 12)):
            stored = self.service.poll_mailbox(mailbox)

        self.assertEqual(stored, 1, "只应入库 1 封（CityU），另一封被跳过")
        self.assertEqual(self.db.insert_message.call_count, 2, "两封都要落库以便审计")
        skipped_calls = self.db.mark_message_skipped_by_uid.call_args_list
        self.assertEqual(len(skipped_calls), 1)
        args = skipped_calls[0].args
        self.assertEqual(args[0], "mbx")
        self.assertEqual(args[1], "123")
        self.assertEqual(args[2], 12)
        self.assertIn("hello@mail.grammarly.com", args[3])
        # The privacy policy says a skipped mail keeps metadata only. Encrypting
        # the body and then marking the row skipped would leave an unread body
        # in the database, so the discarded body must never reach storage.
        stored_messages = [call.args[4] for call in self.db.insert_message.call_args_list]
        self.assertEqual(stored_messages[1]["body"], b"", "非本校邮件的正文不得入库")
        self.assertNotEqual(stored_messages[0]["body"], b"", "本校邮件的正文要加密保存，否则无法生成报告")
        self.assertEqual(stored_messages[1]["subject"], "50% off", "跳过的邮件仍要留元数据以便如实汇报")

    def test_poll_processes_everything_when_allow_list_is_empty(self):
        mailbox = {
            "id": "mbx", "user_id": "usr", "last_uid": 10, "uid_validity": "123",
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        personal = {"subject": "Promo", "sender_name": "Shop", "sender_address": "promo@shop.example",
                    "received": "2026-09-13T02:00:00+00:00", "importance": "normal", "body": "b",
                    "message_key": "<c@shop>"}
        self.db.insert_message.return_value = "msg_ok"
        with mock.patch.object(service_mod, "ALLOWED_SENDER_DOMAINS", ()), \
                mock.patch("pilot_app.service.mailio.fetch_new_messages",
                           return_value=("123", [(11, personal)], 11)):
            stored = self.service.poll_mailbox(mailbox)
        self.assertEqual(stored, 1)
        self.db.mark_message_skipped_by_uid.assert_not_called()


if __name__ == "__main__":
    unittest.main()
