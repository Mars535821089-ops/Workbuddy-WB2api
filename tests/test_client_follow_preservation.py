"""Conversation-preservation regressions using only synthetic temporary state."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from admin import client_follow


class FollowSessionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db_path = self.root / "workbuddy.db"
        self.auth = self.root / "auth.info"
        self.write(self.auth, {"account": {"uid": "old"}})
        with sqlite3.connect(self.db_path) as db:
            db.execute("""CREATE TABLE sessions (
                id TEXT PRIMARY KEY, user_id TEXT, transport TEXT, status TEXT,
                deleted_at INTEGER, cwd TEXT, title TEXT, custom_title TEXT,
                last_activity_at INTEGER, created_at INTEGER, updated_at INTEGER,
                is_playground INTEGER, project_id TEXT, session_settings TEXT)""")
            for id_, owner, transport, status, deleted in (
                ("current", "old", "local", "completed", None),
                ("other", "older", "local", "pending", -1),
                ("target", "new", "local", "error", None),
                ("archived", "old", "local", "archived", -1),
                ("deleted", "old", "local", "completed", 42),
                ("cloud", "old", "cloud", "working", None),
            ):
                db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (id_, owner, transport, status, deleted, "/synthetic",
                            id_, "", 5, 1, 2, 0, "project", '{"keep":true}'))
        self.source_pins = self.root / "storage/user-old-personal/global/conversations.json"
        self.target_pins = self.root / "storage/user-new-personal/global/conversations.json"
        self.write(self.source_pins, {"pinned": [{"id": "current"}, {"id": "archived"},
                                               {"id": "deleted"}, {"id": "cloud"}]})
        self.write(self.root / "storage/user-older-personal/global/conversations.json",
                   {"pinned": [{"id": "other"}]})
        self.write(self.target_pins, {"pinned": [{"id": "target"}], "unrelated": "preserved"})
        self.write(self.root / "old/startup-route.json", {"sessionId": "current"})
        self.provider = self.root / "storage/user-new-personal/global/providers.json"
        self.write(self.provider, {"synthetic": "do-not-change"})
        self.body = self.root / "sessions/current/transcript.jsonl"
        self.body.parent.mkdir(parents=True)
        self.body.write_text('{"text":"synthetic conversation body"}\n')
        self.original_rows = self.rows()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def rows(self):
        with sqlite3.connect(self.db_path) as db:
            return db.execute("SELECT * FROM sessions ORDER BY id").fetchall()

    def switch(self):
        return client_follow.follow_sessions(self.db_path, "new", "old", auth_file=self.auth)

    def test_all_local_sessions_follow_without_changing_content_or_cloud_tasks(self):
        body = self.body.read_bytes()
        providers = self.provider.read_bytes()
        result = self.switch()
        self.assertEqual(result["session_count"], 5)
        self.assertEqual(result["active_count"], 3)
        self.assertEqual(result["archived_count"], 1)
        self.assertEqual(result["pinned_count"], 3)
        self.assertEqual(result["rebound_count"], 4)
        for before, after in zip(self.original_rows, self.rows()):
            if before[2] == "local":
                self.assertEqual(after[1], "new")
                self.assertEqual(before[:1] + before[2:], after[:1] + after[2:])
            else:
                self.assertEqual(before, after)
        self.assertEqual(self.body.read_bytes(), body)
        self.assertEqual(self.provider.read_bytes(), providers)
        self.assertTrue(result["session_ids_preserved"])
        self.assertTrue(result["transcripts_unchanged"])
        pins = json.loads(self.target_pins.read_text())
        self.assertEqual([v["id"] for v in pins["pinned"]], ["current", "target", "other"])
        self.assertEqual(pins["unrelated"], "preserved")
        snapshot = json.loads((self.root / "new/sidebar-list-snapshot.json").read_text())
        self.assertEqual({v["id"] for v in snapshot["items"]}, {"current", "other", "target"})
        self.assertEqual(json.loads((self.root / "new/startup-route.json").read_text()),
                         {"sessionId": "current"})

    def test_interrupted_switch_rolls_back_before_credentials_commit(self):
        original_pins = self.target_pins.read_bytes()
        self.switch()
        self.assertEqual(client_follow.recover_pending(self.db_path), "rolled_back")
        self.assertEqual(self.rows(), self.original_rows)
        self.assertEqual(self.target_pins.read_bytes(), original_pins)
        self.assertFalse((self.root / "new/sidebar-list-snapshot.json").exists())
        self.assertFalse((self.root / "new/startup-route.json").exists())
        self.assertIsNone(client_follow.recover_pending(self.db_path))

    def test_interrupted_switch_keeps_history_after_credentials_commit(self):
        self.switch()
        switched_rows = self.rows()
        self.write(self.auth, {"account": {"uid": "new"}})
        self.assertEqual(client_follow.recover_pending(self.db_path), "committed")
        self.assertEqual(self.rows(), switched_rows)
        self.assertIsNone(client_follow.recover_pending(self.db_path))

    def test_unexpected_account_change_stops_recovery_without_touching_history(self):
        self.switch()
        switched_rows = self.rows()
        self.write(self.auth, {"account": {"uid": "unexpected"}})
        with self.assertRaises(client_follow.FollowError):
            client_follow.recover_pending(self.db_path)
        self.assertEqual(self.rows(), switched_rows)

    def test_repeated_switch_preserves_ids_and_does_not_duplicate_pins(self):
        self.switch()
        self.write(self.auth, {"account": {"uid": "new"}})
        client_follow.recover_pending(self.db_path)
        result = client_follow.follow_sessions(self.db_path, "new", "new", auth_file=self.auth)
        self.assertEqual(result["rebound_count"], 0)
        self.assertEqual(result["pinned_count"], 3)
        self.assertEqual(len(self.rows()), len(self.original_rows))
        pins = json.loads(self.target_pins.read_text())["pinned"]
        self.assertEqual(len({v["id"] for v in pins}), 3)


if __name__ == "__main__":
    unittest.main()
