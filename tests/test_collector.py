from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from guardamar_digest.collector import collect, collector_status
from guardamar_digest.config import Settings
from guardamar_digest.db import connect


def make_settings(path: Path) -> Settings:
    return Settings(
        root=path.parent,
        db_path=path,
        source_username="MarketGuardamar",
        source_chat_id="-1001",
        bot_token="token",
        admin_chat_id="",
        gemini_key="",
        gemini_model="test",
        openrouter_key="",
        openrouter_model="test",
        excluded_sender_ids=frozenset(),
    )


def message_update(
    update_id: int,
    message_id: int,
    text: str,
    *,
    chat_id: int = -1001,
    user_id: int = 42,
    is_bot: bool = False,
    edited: bool = False,
    timestamp: int = 1790809200,
) -> dict:
    message = {
        "message_id": message_id,
        "date": timestamp,
        "chat": {"id": chat_id, "type": "supergroup", "title": "MarketGuardamar"},
        "from": {"id": user_id, "is_bot": is_bot, "first_name": "Author"},
        "text": text,
    }
    return {
        "update_id": update_id,
        "edited_message" if edited else "message": message,
    }


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "digest.sqlite3"
        self.settings = make_settings(self.db)
        self.start = datetime(2026, 9, 30, 22, 44, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collects_source_messages_and_ignores_other_noise(self):
        updates = [
            message_update(1, 100, "Продам велосипед"),
            message_update(2, 101, "Не наш чат", chat_id=-1002),
            message_update(3, 102, "Сообщение бота", user_id=99, is_bot=True),
            {
                "update_id": 4,
                "message": {
                    "message_id": 103,
                    "date": 1790809200,
                    "chat": {"id": -1001, "type": "supergroup"},
                    "from": {"id": 77, "is_bot": False, "first_name": "Photo"},
                    "photo": [{"file_id": "x"}],
                },
            },
        ]
        result = collect(
            self.settings,
            now=self.start,
            fetcher=lambda token, offset: updates,
        )
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["ignored_chat"], 1)
        self.assertEqual(result["ignored_bot"], 1)
        self.assertEqual(result["ignored_empty"], 1)
        self.assertEqual(result["last_update_id"], 4)
        self.assertEqual(result["coverage_status"], "ok")

        with connect(self.db) as con:
            row = con.execute(
                """SELECT m.message_id,m.sender_id,m.source_text,m.source_url,e.period_key
                   FROM messages m JOIN entries e ON e.message_id=m.id"""
            ).fetchone()
        self.assertEqual(row["message_id"], 100)
        self.assertEqual(row["sender_id"], "user42")
        self.assertEqual(row["source_text"], "Продам велосипед")
        self.assertEqual(row["source_url"], "https://t.me/MarketGuardamar/100")
        self.assertEqual(row["period_key"], "2026-10")

    def test_edit_updates_same_record_and_marks_pipeline_pending(self):
        first = message_update(10, 200, "Сдам квартиру")
        collect(
            self.settings,
            now=self.start,
            fetcher=lambda token, offset: [first],
        )

        seen_offsets = []
        edited = message_update(
            11, 200, "Сдам квартиру до декабря", edited=True
        )

        def fetcher(token, offset):
            seen_offsets.append(offset)
            return [edited]

        result = collect(
            self.settings,
            now=self.start + timedelta(minutes=5),
            fetcher=fetcher,
        )
        self.assertEqual(seen_offsets, [11])
        self.assertEqual(result["updated"], 1)

        with connect(self.db) as con:
            rows = con.execute(
                "SELECT source_text FROM messages WHERE message_id=200"
            ).fetchall()
            stages = {
                row["stage"]: row["status"]
                for row in con.execute(
                    "SELECT stage,status FROM workflow_runs WHERE period_key='2026-10'"
                )
            }
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_text"], "Сдам квартиру до декабря")
        self.assertEqual(stages["prefilter"], "pending")
        self.assertEqual(stages["semantic_dedupe"], "pending")

    def test_empty_edit_excludes_and_later_text_restores_record(self):
        collect(
            self.settings,
            now=self.start,
            fetcher=lambda token, offset: [message_update(15, 250, "Подпись")],
        )
        empty_edit = message_update(16, 250, "", edited=True)
        result = collect(
            self.settings,
            now=self.start + timedelta(minutes=5),
            fetcher=lambda token, offset: [empty_edit],
        )
        self.assertEqual(result["excluded_empty_edit"], 1)
        with connect(self.db) as con:
            row = con.execute(
                """SELECT e.eligible,e.excluded_reason,e.dedupe_reason
                   FROM entries e JOIN messages m ON m.id=e.message_id
                   WHERE m.message_id=250"""
            ).fetchone()
        self.assertEqual(row["eligible"], 0)
        self.assertEqual(row["dedupe_reason"], "collector edit")

        restored = message_update(17, 250, "Новая подпись", edited=True)
        collect(
            self.settings,
            now=self.start + timedelta(minutes=10),
            fetcher=lambda token, offset: [restored],
        )
        with connect(self.db) as con:
            row = con.execute(
                """SELECT m.source_text,e.eligible,e.excluded_reason,e.dedupe_reason
                   FROM entries e JOIN messages m ON m.id=e.message_id
                   WHERE m.message_id=250"""
            ).fetchone()
        self.assertEqual(row["source_text"], "Новая подпись")
        self.assertEqual(row["eligible"], 1)
        self.assertIsNone(row["excluded_reason"])
        self.assertIsNone(row["dedupe_reason"])

    def test_repeat_delivery_is_idempotent(self):
        collect(
            self.settings,
            now=self.start,
            fetcher=lambda token, offset: [message_update(20, 300, "Маникюр")],
        )
        result = collect(
            self.settings,
            now=self.start + timedelta(minutes=5),
            fetcher=lambda token, offset: [message_update(21, 300, "Маникюр")],
        )
        self.assertEqual(result["unchanged"], 1)
        with connect(self.db) as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1
            )

    def test_long_gap_makes_coverage_uncertain_and_it_stays_sticky(self):
        collect(
            self.settings,
            now=self.start,
            fetcher=lambda token, offset: [],
        )
        late = self.start + timedelta(hours=24)
        result = collect(
            self.settings,
            now=late,
            fetcher=lambda token, offset: [],
        )
        self.assertEqual(result["coverage_status"], "uncertain")
        self.assertIn("24.0 hours", result["coverage_reason"])

        later = collect(
            self.settings,
            now=late + timedelta(minutes=5),
            fetcher=lambda token, offset: [],
        )
        self.assertEqual(later["coverage_status"], "uncertain")
        status = collector_status(
            self.settings, now=late + timedelta(minutes=5)
        )
        self.assertEqual(status["coverage_status"], "uncertain")


if __name__ == "__main__":
    unittest.main()
