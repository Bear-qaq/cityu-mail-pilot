import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from pilot_app.database import Database
from pilot_app.security import hash_password, token_hash


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / "pilot.sqlite3")
        self.db.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def user(self, email: str, code: str) -> dict:
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash(code), expires))
        return self.db.create_user(email, hash_password("long-enough-password"), token_hash(code))

    def test_invite_is_single_use(self):
        self.user("one@example.com", "invite")
        with self.assertRaises(ValueError):
            self.db.create_user("two@example.com", hash_password("long-enough-password"), token_hash("invite"))

    def test_reports_are_isolated_by_user(self):
        one = self.user("one@example.com", "one")
        two = self.user("two@example.com", "two")
        self.db.create_report(user_id=one["id"], message_id=None, kind="test", subject="private one", body=b"cipher-a", sent_to="one@example.com")
        self.db.create_report(user_id=two["id"], message_id=None, kind="test", subject="private two", body=b"cipher-b", sent_to="two@example.com")
        self.assertEqual([row["subject"] for row in self.db.list_reports(one["id"])], ["private one"])
        self.assertEqual([row["subject"] for row in self.db.list_reports(two["id"])], ["private two"])

    def test_profile_lists_round_trip(self):
        user = self.user("one@example.com", "one")
        self.db.upsert_profile(user["id"], {"school_email": "student@my.cityu.edu.hk", "major": "通信工程", "courses_json": json.dumps(["C++", "密码学"]), "updated_at": "ignored"})
        profile = self.db.get_profile(user["id"])
        self.assertEqual(profile["major"], "通信工程")
        self.assertEqual(profile["school_email"], "student@my.cityu.edu.hk")
        self.assertEqual(profile["courses"], ["C++", "密码学"])

    def test_changing_mailbox_identity_resets_uid_state_but_keeps_reports(self):
        user = self.user("owner@example.com", "mailbox-change")
        common = {
            "report_to": "owner@example.com", "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"placeholder", "enabled": True,
        }
        mailbox_id = self.db.upsert_mailbox(user["id"], {**common, "email": "old@qq.com"})
        self.db.update_mailbox_poll(mailbox_id, last_uid=88, uid_validity="123")
        message_id = self.db.insert_message(user["id"], mailbox_id, "123", 88, {
            "subject": "old message", "received": "2026-09-13T00:00:00+00:00", "body": "old",
        })
        report_id = self.db.create_report(
            user_id=user["id"], message_id=message_id, kind="immediate", subject="kept report",
            body="kept", sent_to="owner@example.com",
        )

        # Updating only credentials/config keeps the ingestion cursor.
        self.db.upsert_mailbox(user["id"], {**common, "email": "old@qq.com", "encrypted_password": b"new"})
        self.assertEqual(self.db.get_mailbox(user["id"])["last_uid"], 88)

        # Connecting another inbox resets UID identity and detaches, but does
        # not erase, the report already delivered to the user.
        self.db.upsert_mailbox(user["id"], {**common, "email": "new@qq.com"})
        mailbox = self.db.get_mailbox(user["id"])
        self.assertEqual(mailbox["last_uid"], 0)
        self.assertEqual(mailbox["uid_validity"], "")
        with self.db.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM messages WHERE mailbox_id=?", (mailbox_id,)
            ).fetchone()[0], 0)
            report = connection.execute("SELECT message_id FROM reports WHERE id=?", (report_id,)).fetchone()
        self.assertIsNone(report["message_id"])

    def test_second_copy_of_the_same_mail_is_not_stored_again(self):
        """Two forwarding rules deliver one mail twice with different UIDs.

        IMAP UIDs differ, so only the RFC 5322 Message-ID can tell the copies
        apart from genuinely new mail; without this the user gets two reports.
        """
        user = self.user("dup@example.com", "dup")
        mailbox_id = self.db.upsert_mailbox(user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com", "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465, "encrypted_password": b"cipher",
        })
        first = self.db.insert_message(user["id"], mailbox_id, "v1", 100, {
            "subject": "Course deadline", "received": "2026-09-13T00:00:00+00:00", "body": "cipher-a",
            "message_key": "<20260913091828.5982EBAE32@smtp82.ad.cityu.edu.hk>",
        })
        second = self.db.insert_message(user["id"], mailbox_id, "v1", 101, {
            "subject": "Course deadline", "received": "2026-09-13T00:00:04+00:00", "body": "cipher-a",
            "message_key": "<20260913091828.5982EBAE32@smtp82.ad.cityu.edu.hk>",
        })
        self.assertIsInstance(first, str)
        self.assertIsNone(second)
        with self.db.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=?", (user["id"],)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_different_mails_without_message_id_are_both_kept(self):
        """A missing Message-ID must never drop a real, distinct email."""
        user = self.user("nokey@example.com", "nokey")
        mailbox_id = self.db.upsert_mailbox(user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com", "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465, "encrypted_password": b"cipher",
        })
        for uid in (200, 201):
            self.db.insert_message(user["id"], mailbox_id, "v1", uid, {
                "subject": f"Notice {uid}", "received": "2026-09-13T00:00:00+00:00", "body": "cipher",
            })
        with self.db.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=?", (user["id"],)
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_same_message_id_from_another_user_is_kept(self):
        """Per-user isolation: one user's dedup key must not hide another's mail."""
        one = self.user("dup-one@example.com", "dup-one")
        two = self.user("dup-two@example.com", "dup-two")
        key = "<shared-forward@smtp82.ad.cityu.edu.hk>"
        stored = []
        for user, email in ((one, "one@qq.com"), (two, "two@qq.com")):
            mailbox_id = self.db.upsert_mailbox(user["id"], {
                "email": email, "report_to": email, "imap_host": "imap.qq.com", "imap_port": 993,
                "smtp_host": "smtp.qq.com", "smtp_port": 465, "encrypted_password": b"cipher",
            })
            stored.append(self.db.insert_message(user["id"], mailbox_id, "v1", 300, {
                "subject": "Shared notice", "received": "2026-09-13T00:00:00+00:00", "body": "cipher",
                "message_key": key,
            }))
        self.assertTrue(all(isinstance(value, str) for value in stored))

    def test_initialize_migrates_a_premessagekey_database(self):
        """Regression: the schema index on messages.message_key must not run
        before the column exists, or every existing deployment fails to start."""
        import sqlite3
        legacy = Path(self.temporary.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL);
            CREATE TABLE mailboxes(id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL, report_to TEXT NOT NULL, imap_host TEXT NOT NULL,
                imap_port INTEGER NOT NULL, smtp_host TEXT NOT NULL, smtp_port INTEGER NOT NULL,
                encrypted_password BLOB NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                uid_validity TEXT NOT NULL DEFAULT '', last_uid INTEGER NOT NULL DEFAULT 0,
                last_polled_at TEXT, last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
            CREATE TABLE messages(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mailbox_id TEXT NOT NULL,
                uid_validity TEXT NOT NULL DEFAULT '', imap_uid INTEGER NOT NULL,
                subject TEXT NOT NULL, sender_name TEXT NOT NULL DEFAULT '',
                sender_address TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL,
                importance TEXT NOT NULL DEFAULT 'normal', body BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT, last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                UNIQUE(mailbox_id, uid_validity, imap_uid));
            INSERT INTO users VALUES('usr','legacy@example.com','hash','active','2026-09-01T00:00:00+00:00');
            INSERT INTO mailboxes VALUES('mbx','usr','me@qq.com','me@qq.com','imap.qq.com',993,
                'smtp.qq.com',465,X'00',1,'',0,NULL,'','2026-09-01T00:00:00+00:00');
            INSERT INTO messages VALUES('msg','usr','mbx','',7,'Old mail','','','2026-09-01T00:00:00+00:00',
                'normal',X'00','sent',0,NULL,'','2026-09-01T00:00:00+00:00');
            """
        )
        connection.commit()
        connection.close()

        migrated = Database(legacy)
        migrated.initialize()
        migrated.initialize()  # must be idempotent
        with migrated.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
            mailbox_columns = {row[1] for row in connection.execute("PRAGMA table_info(mailboxes)")}
            kept = connection.execute("SELECT subject FROM messages").fetchone()[0]
        self.assertIn("message_key", columns)
        self.assertIn("last_verified_at", mailbox_columns)
        self.assertIn("last_verify_error", mailbox_columns)
        self.assertEqual(kept, "Old mail")

    def test_initialize_migrates_a_preschoolemail_database(self):
        import sqlite3
        legacy = Path(self.temporary.name) / "legacy-profile.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL);
            CREATE TABLE profiles(user_id TEXT PRIMARY KEY, major TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL);
            """
        )
        connection.commit()
        connection.close()
        migrated = Database(legacy)
        migrated.initialize()
        with migrated.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(profiles)")}
        self.assertIn("school_email", columns)

    def test_initialize_relaxes_an_old_status_check_and_keeps_data(self):
        """Regression: an older DB only allowed 5 statuses, so writing the new
        'skipped' status failed. SQLite cannot ALTER a CHECK, so initialize()
        rebuilds the table; it must stay idempotent and lose nothing."""
        import sqlite3
        legacy = Path(self.temporary.name) / "legacy-status.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL);
            CREATE TABLE mailboxes(id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL, report_to TEXT NOT NULL, imap_host TEXT NOT NULL,
                imap_port INTEGER NOT NULL, smtp_host TEXT NOT NULL, smtp_port INTEGER NOT NULL,
                encrypted_password BLOB NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                uid_validity TEXT NOT NULL DEFAULT '', last_uid INTEGER NOT NULL DEFAULT 0,
                last_polled_at TEXT, last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
            CREATE TABLE messages(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mailbox_id TEXT NOT NULL,
                uid_validity TEXT NOT NULL DEFAULT '', imap_uid INTEGER NOT NULL,
                subject TEXT NOT NULL, sender_name TEXT NOT NULL DEFAULT '',
                sender_address TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL,
                importance TEXT NOT NULL DEFAULT 'normal', body BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','processing','sent','failed')),
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
                last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                UNIQUE(mailbox_id, uid_validity, imap_uid));
            CREATE TABLE reports(id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                message_id TEXT REFERENCES messages(id) ON DELETE SET NULL, kind TEXT NOT NULL,
                subject TEXT NOT NULL, body_markdown BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'generated', sent_to TEXT NOT NULL DEFAULT '',
                report_date TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, sent_at TEXT,
                UNIQUE(user_id, kind, report_date, message_id));
            INSERT INTO users VALUES('usr','old@example.com','hash','active','2026-09-01T00:00:00+00:00');
            INSERT INTO mailboxes VALUES('mbx','usr','me@qq.com','me@qq.com','imap.qq.com',993,
                'smtp.qq.com',465,X'00',1,'',0,NULL,'','2026-09-01T00:00:00+00:00');
            INSERT INTO messages VALUES('msg1','usr','mbx','',7,'CityU notice','','','2026-09-01T00:00:00+00:00',
                'normal',X'00','sent',0,NULL,'','2026-09-01T00:00:00+00:00');
            INSERT INTO reports VALUES('rpt1','usr','msg1','immediate','subj',X'00','sent','me@qq.com','','',
                '2026-09-01T00:00:00+00:00',NULL);
            """
        )
        connection.commit()
        connection.close()

        migrated = Database(legacy)
        migrated.initialize()
        migrated.initialize()  # idempotent

        with migrated.connect() as conn:
            definition = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
            kept = conn.execute("SELECT status,subject FROM messages").fetchone()
            linked = conn.execute("SELECT COUNT(*) FROM reports WHERE message_id IS NOT NULL").fetchone()[0]
            integrity = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertIn("skipped", definition, "约束必须已放宽")
        self.assertEqual(kept["subject"], "CityU notice")
        self.assertEqual(linked, 1, "reports 与 messages 的关联必须保留")
        self.assertEqual(integrity, [])
        # The new status must now be writable.
        migrated.mark_message_skipped_by_uid("mbx", "", 7, "测试原因")
        with migrated.connect() as conn:
            row = conn.execute("SELECT status,skip_reason FROM messages WHERE id='msg1'").fetchone()
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["skip_reason"], "测试原因")


if __name__ == "__main__":
    unittest.main()
