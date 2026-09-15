"""How the worker logs a failure -- which is a decision about what a log is for.

A log is read to answer one question: *what is wrong that I did not already
know?* A mailbox whose owner typed the wrong auth code is not that. The owner is
told inside the app, the admin panel shows it as a red light, and the sentinel
reports `mailbox_error`; the worker re-discovering it every poll cycle adds
nothing except volume.

On 2026-09-15 one such account produced ~750 lines of identical traceback per
day in `journalctl -u cityu-mail-pilot-worker`. That is not merely untidy: the
whole point of a traceback is that it is rare enough to be worth reading, and a
familiar wall of it is how a real one gets scrolled past.

So the rule is narrow and structural, not a level tweak: a `MailError` means the
mail server rejected *this account* -- one line. Anything else is a failure
nobody anticipated, and it keeps its traceback.

The rule has four consumers and this file checks all four. That is deliberate:
v0.59.1 is the round where the same "timestamp is not success" rule had been
taught to the panel and the sentinel and *not* to the health card, and the bug
survived every test. Teaching the poller alone would be that mistake again.
"""

import logging
import secrets
import unittest
from unittest import mock

from pilot_app import mailio, worker
from pilot_app.security import SecretBox
from pilot_app.service import PilotService


class PollFailureLoggingTests(unittest.TestCase):
    def _poll_raising(self, exc: Exception) -> list[logging.LogRecord]:
        service = mock.Mock()
        service.db.active_mailboxes.return_value = [{"id": "mbx_1"}]
        service.poll_mailbox.side_effect = exc
        with self.assertLogs(level=logging.WARNING) as captured:
            worker.poll_all(service)
        return captured.records

    def test_a_rejected_account_is_one_quiet_line(self):
        """The observed case: a wrong auth code, every cycle."""
        records = self._poll_raising(
            mailio.MailError("授权码（应用专用密码）不正确或已失效，请在邮箱设置里重新生成一个再试。"))
        self.assertEqual(len(records), 1, "一个已知的账号问题不该产生多条日志")
        self.assertEqual(records[0].levelno, logging.WARNING)
        self.assertIsNone(records[0].exc_info, "这里是堆栈唯一真正多余的地方")
        self.assertIn("mbx_1", records[0].getMessage())
        self.assertIn("授权码", records[0].getMessage(), "原因要留在那一行里")

    def test_an_unexpected_failure_keeps_its_traceback(self):
        """The other half. Downgrading everything would be the same bug mirrored:
        a genuine bug would become one line nobody can act on."""
        records = self._poll_raising(AttributeError("boom"))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].levelno, logging.ERROR)
        self.assertIsNotNone(records[0].exc_info, "没想到的失败必须留堆栈")
        traceback_text = logging.Formatter().formatException(records[0].exc_info)
        self.assertIn("AttributeError", traceback_text)

    def test_the_failed_mailbox_is_still_recorded_and_reported(self):
        """Downgrading the log line must not downgrade the bookkeeping: the
        panel, the red light and the backoff all read these values."""
        service = mock.Mock()
        service.db.active_mailboxes.return_value = [{"id": "mbx_1"}]
        service.poll_mailbox.side_effect = mailio.MailError("授权码不正确。")
        with self.assertLogs(level=logging.WARNING):
            result = worker.poll_all(service)
        self.assertEqual(result["failed"], ["mbx_1"])
        self.assertEqual(len(result["errors"]), 1)
        recorded = service.db.update_mailbox_poll.call_args.kwargs
        self.assertEqual(recorded["error"], "授权码不正确。")


class EveryConsumerIsTaughtTests(unittest.TestCase):
    """Each per-cycle path that can see a `MailError` must use the same rule."""

    def setUp(self):
        self.db = mock.MagicMock()
        self.service = PilotService(self.db, SecretBox(secrets.token_bytes(32)))

    def test_the_thread_pool_queue_path(self):
        service = mock.Mock()
        service.db.due_messages.return_value = [{"id": "msg_1", "user_id": "usr_1"}]
        service.process_message.side_effect = mailio.MailError("邮箱配置已不存在。")
        with self.assertLogs(level=logging.WARNING) as captured:
            worker.process_due(service)
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.WARNING)
        self.assertIsNone(captured.records[0].exc_info)

    def test_the_single_threaded_poll_reference_path(self):
        self.db.active_mailboxes.return_value = [{"id": "mbx_1"}]
        self.service.poll_mailbox = mock.Mock(side_effect=mailio.MailError("授权码不正确。"))
        with self.assertLogs(level=logging.WARNING) as captured:
            self.service.poll_all()
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.WARNING)
        self.assertIsNone(captured.records[0].exc_info)

    def test_the_message_processing_path_itself(self):
        """`service.process_message` swallows the exception and returns False, so
        the worker's own `except` above never sees the common case -- if only the
        worker were fixed, production would look unchanged."""
        self.db.mark_message_processing.return_value = True
        self.db.get_mailbox.return_value = None  # -> MailError("邮箱配置已不存在。")
        with self.assertLogs(level=logging.WARNING) as captured:
            self.assertFalse(self.service.process_message(
                {"id": "msg_1", "user_id": "usr_1", "attempts": 0}))
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.WARNING)
        self.assertIsNone(captured.records[0].exc_info)

    def test_an_unexpected_failure_on_the_queue_path_is_not_swallowed_quietly(self):
        """The rule must not become "the queue never logs loudly"."""
        self.db.mark_message_processing.return_value = True
        self.db.get_mailbox.side_effect = RuntimeError("db is on fire")
        with self.assertLogs(level=logging.WARNING) as captured:
            self.assertFalse(self.service.process_message(
                {"id": "msg_1", "user_id": "usr_1", "attempts": 0}))
        record = captured.records[0]
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertIsNotNone(record.exc_info)


if __name__ == "__main__":
    unittest.main()
