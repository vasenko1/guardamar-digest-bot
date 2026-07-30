from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from guardamar_digest.config import Settings
from guardamar_digest.db import connect
from guardamar_digest.dedupe import dedupe, semantic_dedupe
from guardamar_digest.prefilter import prefilter
from guardamar_digest.validate import validate_period


def make_settings(path: Path, excluded=()) -> Settings:
    return Settings(
        root=path.parent, db_path=path, source_username="MarketGuardamar",
        source_chat_id="-1001", bot_token="", admin_chat_id="",
        gemini_key="", gemini_model="test", openrouter_key="",
        openrouter_model="test", excluded_sender_ids=frozenset(excluded),
    )


def add_message(con, external_id: int, text: str, sender: str = "author",
                published: str = "2026-07-01T10:00:00") -> int:
    cursor = con.execute(
        """INSERT INTO messages
           (chat_id,message_id,published_at,sender_name,sender_id,source_text,source_url)
           VALUES ('-1001',?,?,?,?,?,?)""",
        (external_id, published, sender, sender, text,
         f"https://t.me/MarketGuardamar/{external_id}"),
    )
    con.execute(
        "INSERT INTO entries(message_id,period_key) VALUES (?,'2026-07')",
        (cursor.lastrowid,),
    )
    return cursor.lastrowid


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_prefilter_is_audited_and_preserves_cross_month_date(self):
        settings = make_settings(self.db, {"system"})
        with connect(self.db) as con:
            system = add_message(con, 1, "Погода и пляжи сегодня", "system")
            incomplete = add_message(con, 2, "Возможно торг")
            expired = add_message(con, 3, "Поездка Аликанте — Валенсия 4 июля")
            future = add_message(con, 4, "Сдам с 30.07 по 02.08 квартиру у моря")
        result = prefilter(settings, "2026-07", "2026-07-31")
        self.assertEqual(result["excluded_author"], 1)
        self.assertEqual(result["excluded_incomplete"], 1)
        self.assertEqual(result["excluded_expired"], 1)
        with connect(self.db) as con:
            kept = con.execute(
                "SELECT eligible,excluded_reason FROM entries WHERE message_id=?",
                (future,),
            ).fetchone()
            audits = con.execute(
                "SELECT COUNT(*) FROM editorial_audit WHERE period_key='2026-07'"
            ).fetchone()[0]
        self.assertEqual((kept["eligible"], kept["excluded_reason"]), (1, None))
        self.assertEqual(audits, 4)

    def test_semantic_failure_has_deterministic_zero_unresolved_fallback(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            add_message(con, 10, "Аренда авто в Аликанте от владельца")
            add_message(con, 11, "Аренда авто в Аликанте напрямую от владельца")
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)), \
             patch("guardamar_digest.dedupe._ask_provider",
                   side_effect=ValueError("provider unavailable")):
            result = semantic_dedupe(settings, "2026-07")
        self.assertEqual(result["unresolved_pairs"], 0)
        self.assertEqual(result["fallback_pairs"], 1)
        with connect(self.db) as con:
            statuses = {
                row["status"] for row in con.execute(
                    "SELECT status FROM duplicate_reviews WHERE period_key='2026-07'"
                )
            }
            flags = con.execute(
                "SELECT SUM(needs_duplicate_review) FROM entries WHERE period_key='2026-07'"
            ).fetchone()[0]
        self.assertNotIn("pending", statuses)
        self.assertNotIn("uncertain", statuses)
        self.assertEqual(flags, 0)

    def test_validator_blocks_unresolved_and_title_leaks(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            left = add_message(con, 20, "Услуги массажа")
            right = add_message(con, 21, "Предлагаю массаж")
            con.execute(
                """UPDATE entries SET category_code='services',
                   category_title='Услуги',category_emoji='🛠',
                   short_title='Массаж 20 € +34600111222',classification_run_id='run'
                   WHERE period_key='2026-07'"""
            )
            con.execute(
                """INSERT INTO classification_runs
                   (period_key,run_id,input_signature,categories_json,status)
                   VALUES ('2026-07','run','sig','[]','complete')"""
            )
            con.execute(
                """INSERT INTO duplicate_reviews
                   (period_key,left_message_id,right_message_id,lexical_score,status)
                   VALUES ('2026-07',?,?,0.8,'pending')""",
                (left, right),
            )
        result = validate_period(settings, "2026-07")
        self.assertFalse(result.ok)
        self.assertTrue(any("unresolved" in error for error in result.errors))
        self.assertTrue(any("price leaked" in error for error in result.errors))
        self.assertTrue(any("contact or URL" in error for error in result.errors))

    def test_validator_accepts_completed_current_pipeline(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            message_id = add_message(con, 30, "Предлагаю занятия по шахматам")
        prefilter(settings, "2026-07")
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)):
            semantic_dedupe(settings, "2026-07")
        with connect(self.db) as con:
            con.execute(
                """UPDATE entries SET category_code='education',
                   category_title='Обучение',category_emoji='📚',
                   short_title='Занятия по шахматам',classification_run_id='run'
                   WHERE message_id=?""", (message_id,)
            )
            con.execute(
                """INSERT INTO classification_runs
                   (period_key,run_id,input_signature,categories_json,status)
                   VALUES ('2026-07','run','sig','[]','complete')"""
            )
        part = (
            '📚 <b>обЪявления Гуардамар</b>\n\n'
            '📚 <b>Обучение</b>\n'
            '• Занятия по шахматам <a href="https://t.me/MarketGuardamar/30">↗</a>'
        )
        self.assertTrue(validate_period(settings, "2026-07", [part]).ok)


if __name__ == "__main__":
    unittest.main()
