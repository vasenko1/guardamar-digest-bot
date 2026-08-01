from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from guardamar_digest.config import Settings
from guardamar_digest.db import connect
from guardamar_digest.dedupe import VERSION as DEDUPE_VERSION
from guardamar_digest.dedupe import _date_features, _route_features
from guardamar_digest.dedupe import dedupe, semantic_dedupe
from guardamar_digest.importer import import_export
from guardamar_digest.llm import (
    _json,
    _ensure_editorial_categories,
    _sanitize_showcase_title,
    _validate_category_assignment,
    _validate_showcase_title,
    classify,
    classification_signature,
    prepare_rows,
)
from guardamar_digest.prefilter import prefilter
from guardamar_digest.publisher import publish_parts
from guardamar_digest.render import render
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


def current_classification_signature(con, period: str) -> str:
    records = con.execute(
        """SELECT m.id,m.source_text FROM messages m JOIN entries e ON e.message_id=m.id
           WHERE e.period_key=? AND e.manual_title IS NULL
             AND e.excluded_reason IS NULL AND e.needs_duplicate_review=0
           ORDER BY m.message_id""",
        (period,),
    ).fetchall()
    return classification_signature(prepare_rows(records)) if records else ""


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_stop_features_normalize_date_and_route_formatting(self):
        self.assertEqual(_date_features("Поездка 4.07"), _date_features("Поездка 04/07"))
        self.assertEqual(
            _route_features("Торревьеха-Валенсия"),
            _route_features("Торревьеха → Валенсия"),
        )
        self.assertEqual(
            _route_features("Торревьеха ↔ Валенсия"),
            _route_features("Валенсия ↔ Торревьеха"),
        )
        self.assertEqual(_route_features("контент-план и SMM-сопровождение"), set())
        self.assertEqual(_route_features("навчання з будь-якої точки"), set())

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

    def test_last_day_event_expires_after_month_but_cross_month_range_stays(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            last_day = add_message(con, 5, "Игра Мафия 31.07")
            crossing = add_message(con, 6, "Лагерь с 31.07 по 02.08")
        prefilter(settings, "2026-07", "2026-08-01")
        with connect(self.db) as con:
            rows = {
                row["message_id"]: row
                for row in con.execute(
                    "SELECT message_id,eligible,excluded_reason FROM entries"
                )
            }
        self.assertEqual(
            (rows[last_day]["eligible"], rows[last_day]["excluded_reason"]),
            (0, "expired"),
        )
        self.assertEqual(
            (rows[crossing]["eligible"], rows[crossing]["excluded_reason"]),
            (1, None),
        )

    def test_prefilter_removes_vague_price_only_and_expired_relative_posts(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            vague = add_message(con, 50, "Продам\n50 евро")
            rental = add_message(con, 51, "Для летней или краткосрочной аренды только")
            today = add_message(
                con, 52, "Сегодня еду из Валенсии в Торревьеху",
                published="2026-07-28T10:00:00",
            )
            weekday = add_message(
                con, 53, "В эту среду еду из Гуардамара в Бенидорм",
                published="2026-07-20T10:00:00",
            )
            flowers = add_message(
                con, 54, "Акция субботы, только сегодня: букет роз",
                published="2026-07-25T10:00:00",
            )
        prefilter(settings, "2026-07", "2026-07-31")
        with connect(self.db) as con:
            reasons = {
                row["message_id"]: row["excluded_reason"]
                for row in con.execute(
                    "SELECT message_id,excluded_reason FROM entries"
                )
            }
        self.assertEqual(reasons[vague], "incomplete")
        self.assertEqual(reasons[rental], "incomplete")
        self.assertEqual(reasons[today], "expired")
        self.assertEqual(reasons[weekday], "expired")
        self.assertEqual(reasons[flowers], "expired")

    def test_semantic_failure_has_deterministic_zero_unresolved_fallback(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            add_message(con, 10, "Аренда авто в Аликанте от владельца")
            add_message(con, 11, "Аренда авто в Аликанте напрямую от владельца")
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)), \
             patch("guardamar_digest.dedupe._ask_provider") as ask:
            result = semantic_dedupe(settings, "2026-07")
        self.assertEqual(result["unresolved_pairs"], 0)
        ask.assert_not_called()
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

    def test_same_author_smm_campaign_is_deduplicated_without_llm(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            add_message(con, 55, "Ведение Instagram и создание Reels для бизнеса")
            add_message(
                con, 56,
                "Разберу профиль и помогу привлекать клиентов через SMM",
                published="2026-07-20T10:00:00",
            )
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)), \
             patch("guardamar_digest.dedupe._ask_provider",
                   side_effect=ValueError("provider unavailable")):
            result = semantic_dedupe(settings, "2026-07")
        self.assertEqual(result["unresolved_pairs"], 0)
        with connect(self.db) as con:
            published = con.execute(
                "SELECT COUNT(*) FROM entries WHERE period_key='2026-07' AND excluded_reason IS NULL"
            ).fetchone()[0]
        self.assertEqual(published, 1)

    def test_duplicate_chain_is_flattened_to_active_canonical(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            first = add_message(
                con, 18, "Аренда авто в Аликанте от владельца",
                published="2026-07-01T10:00:00",
            )
            second = add_message(
                con, 19, "Аренда авто в Аликанте от владельца",
                published="2026-07-02T10:00:00",
            )
            final = add_message(
                con, 20, "Аренда авто в Аликанте напрямую от владельца",
                published="2026-07-03T10:00:00",
            )
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)), \
             patch("guardamar_digest.dedupe._ask_provider",
                   side_effect=ValueError("provider unavailable")):
            semantic_dedupe(settings, "2026-07")
        with connect(self.db) as con:
            rows = {
                row["message_id"]: row
                for row in con.execute(
                    """SELECT message_id,eligible,excluded_reason,duplicate_of
                       FROM entries WHERE message_id IN (?,?,?)""",
                    (first, second, final),
                )
            }
        self.assertEqual(rows[final]["eligible"], 1)
        self.assertIsNone(rows[final]["excluded_reason"])
        self.assertEqual(rows[first]["duplicate_of"], final)
        self.assertEqual(rows[second]["duplicate_of"], final)

    def test_dedupe_preserves_resumable_classification_but_reopens_old_decision(self):
        with connect(self.db) as con:
            left = add_message(con, 12, "Аренда авто в Аликанте от владельца")
            right = add_message(con, 13, "Аренда авто в Аликанте напрямую от владельца")
            con.execute(
                """UPDATE entries SET short_title='Старый заголовок',
                   classification_run_id='old-run' WHERE period_key='2026-07'"""
            )
        dedupe(self.db, "2026-07")
        with connect(self.db) as con:
            signature = current_classification_signature(con, "2026-07")
            con.execute(
                """UPDATE duplicate_reviews SET status='same',confidence='high',
                   provider='gemini',rule_version='old-version'
                   WHERE period_key='2026-07'"""
            )
        dedupe(self.db, "2026-07")
        with connect(self.db) as con:
            entries = con.execute(
                """SELECT short_title,classification_run_id FROM entries
                   WHERE message_id IN (?,?)""", (left, right)
            ).fetchall()
            status = con.execute(
                "SELECT status FROM duplicate_reviews WHERE period_key='2026-07'"
            ).fetchone()["status"]
        self.assertTrue(all(row["short_title"] == "Старый заголовок" for row in entries))
        self.assertTrue(all(row["classification_run_id"] == "old-run" for row in entries))
        self.assertEqual(status, "pending")

    def test_dedupe_does_not_reinclude_llm_rejected_candidate(self):
        with connect(self.db) as con:
            left = add_message(con, 46, "Аренда авто в Аликанте от владельца")
            add_message(con, 47, "Аренда авто в Аликанте напрямую от владельца")
        dedupe(self.db, "2026-07")
        with connect(self.db) as con:
            con.execute(
                """UPDATE entries SET eligible=0,short_title='Не объявление',
                   classification_run_id='run' WHERE message_id=?""",
                (left,),
            )
        dedupe(self.db, "2026-07")
        with connect(self.db) as con:
            row = con.execute(
                "SELECT eligible,classification_run_id FROM entries WHERE message_id=?",
                (left,),
            ).fetchone()
        self.assertEqual((row["eligible"], row["classification_run_id"]), (0, "run"))

    def test_manual_duplicate_decision_survives_rule_upgrade(self):
        with connect(self.db) as con:
            add_message(con, 14, "Уроки шахмат для детей")
            add_message(con, 15, "Уроки шахмат для детей онлайн")
        dedupe(self.db, "2026-07")
        with connect(self.db) as con:
            con.execute(
                """UPDATE duplicate_reviews SET status='different',
                   confidence='editor',provider='manual',rule_version='old'
                   WHERE period_key='2026-07'"""
            )
        dedupe(self.db, "2026-07")
        with connect(self.db) as con:
            row = con.execute(
                """SELECT status,provider FROM duplicate_reviews
                   WHERE period_key='2026-07'"""
            ).fetchone()
        self.assertEqual((row["status"], row["provider"]), ("different", "manual"))

    def test_superseded_pair_reactivates_when_message_returns(self):
        with connect(self.db) as con:
            add_message(con, 16, "Аренда авто в Аликанте от владельца")
            add_message(con, 17, "Аренда авто в Аликанте напрямую от владельца")
        dedupe(self.db, "2026-07")
        with connect(self.db) as con:
            con.execute(
                """UPDATE duplicate_reviews SET status='superseded',
                   provider='rule',rule_version=? WHERE period_key='2026-07'""",
                (DEDUPE_VERSION,),
            )
        dedupe(self.db, "2026-07")
        with connect(self.db) as con:
            status = con.execute(
                "SELECT status FROM duplicate_reviews WHERE period_key='2026-07'"
            ).fetchone()["status"]
        self.assertEqual(status, "pending")

    def test_reimport_marks_deleted_message_without_deleting_raw_record(self):
        first = Path(self.tmp.name) / "first.json"
        second = Path(self.tmp.name) / "second.json"
        common = {
            "type": "message", "from": "Автор", "from_id": "author",
        }
        first.write_text(json.dumps({"messages": [
            {**common, "id": 40, "date": "2026-07-01T10:00:00", "text": "Продам стол"},
            {**common, "id": 41, "date": "2026-07-10T10:00:00", "text": "Продам стул"},
            {**common, "id": 42, "date": "2026-07-20T10:00:00", "text": "Продам шкаф"},
        ]}), encoding="utf-8")
        second.write_text(json.dumps({"messages": [
            {**common, "id": 40, "date": "2026-07-01T10:00:00", "text": "Продам стол"},
            {**common, "id": 42, "date": "2026-07-20T10:00:00", "text": "Продам шкаф"},
        ]}), encoding="utf-8")
        import_export(self.db, first, "2026-07", "-1001", "MarketGuardamar")
        import_export(self.db, second, "2026-07", "-1001", "MarketGuardamar")
        with connect(self.db) as con:
            raw = con.execute(
                "SELECT COUNT(*) FROM messages WHERE message_id=41"
            ).fetchone()[0]
            entry = con.execute(
                """SELECT eligible,excluded_reason,dedupe_reason FROM entries e
                   JOIN messages m ON m.id=e.message_id WHERE m.message_id=41"""
            ).fetchone()
        self.assertEqual(raw, 1)
        self.assertEqual(
            (entry["eligible"], entry["excluded_reason"], entry["dedupe_reason"]),
            (0, "missing from latest export", "import reconciliation"),
        )

    def test_import_uses_madrid_timezone_at_month_boundary(self):
        export = Path(self.tmp.name) / "timezone.json"
        common = {
            "type": "message", "from": "Автор", "from_id": "author",
        }
        export.write_text(json.dumps({"messages": [
            {**common, "id": 44, "date": "2026-06-30T22:30:00Z",
             "text": "Продам стол"},
            {**common, "id": 45, "date": "2026-07-31T22:30:00+00:00",
             "text": "Продам шкаф"},
        ]}), encoding="utf-8")
        imported = import_export(
            self.db, export, "2026-07", "-1001", "MarketGuardamar"
        )
        self.assertEqual(imported, 1)
        with connect(self.db) as con:
            row = con.execute(
                "SELECT message_id,published_at FROM messages"
            ).fetchone()
        self.assertEqual(row["message_id"], 44)
        self.assertEqual(row["published_at"], "2026-07-01T00:30:00")

    def test_render_blocks_null_classification_run_id(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            add_message(con, 43, "Услуги массажа")
            con.execute(
                """INSERT INTO classification_runs
                   (period_key,run_id,input_signature,categories_json,status)
                   VALUES ('2026-07','run','sig','[]','complete')"""
            )
        with self.assertRaisesRegex(RuntimeError, "Classification is incomplete"):
            render(settings, "2026-07")

    def test_publication_resume_skips_already_sent_parts(self):
        settings = make_settings(self.db)
        calls: list[str] = []

        def sender(token, destination, text):
            calls.append(text)
            return {"ok": True, "result": {"message_id": 100 + len(calls)}}

        first = publish_parts(settings, "2026-07", ["one", "two"], sender)
        second = publish_parts(settings, "2026-07", ["one", "two"], sender)
        self.assertEqual(first, {"sent": 2, "skipped": 0, "parts": 2})
        self.assertEqual(second, {"sent": 0, "skipped": 2, "parts": 2})
        self.assertEqual(calls, ["one", "two"])

    def test_publication_blocks_changed_already_sent_part(self):
        settings = make_settings(self.db)

        def sender(token, destination, text):
            return {"ok": True, "result": {"message_id": 101}}

        publish_parts(settings, "2026-07", ["original"], sender)
        with self.assertRaisesRegex(RuntimeError, "already sent part 1 changed"):
            publish_parts(settings, "2026-07", ["changed"], sender)

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
            signature = current_classification_signature(con, "2026-07")
            con.execute(
                """UPDATE entries SET category_code='education',
                   category_title='Обучение',category_emoji='📚',
                   short_title='Занятия по шахматам',classification_run_id='run'
                   WHERE message_id=?""", (message_id,)
            )
            con.execute(
                """INSERT INTO classification_runs
                   (period_key,run_id,input_signature,categories_json,status)
                   VALUES ('2026-07','run',?,'[]','complete')""",
                (signature,),
            )
        part = (
            '📚 <b>обЪявления Гуардамар</b>\n\n'
            '📚 <b>Обучение</b>\n'
            '• Занятия по шахматам <a href="https://t.me/MarketGuardamar/30">↗</a>'
        )
        self.assertTrue(validate_period(settings, "2026-07", [part]).ok)

    def test_validator_rejects_changed_text_with_old_classification_run(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            message_id = add_message(con, 36, "Уроки шахмат")
        prefilter(settings, "2026-07")
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)):
            semantic_dedupe(settings, "2026-07")
        with connect(self.db) as con:
            signature = current_classification_signature(con, "2026-07")
            con.execute(
                """UPDATE entries SET eligible=1,category_code='education',
                   category_title='Обучение',category_emoji='📚',
                   short_title='Уроки шахмат',classification_run_id='run'
                   WHERE message_id=?""", (message_id,)
            )
            con.execute(
                """INSERT INTO classification_runs
                   (period_key,run_id,input_signature,categories_json,status)
                   VALUES ('2026-07','run',?,'[]','complete')""",
                (signature,),
            )
            con.execute(
                "UPDATE messages SET source_text='Уроки испанского' WHERE id=?",
                (message_id,),
            )
        result = validate_period(settings, "2026-07")
        self.assertTrue(any("signature is stale" in error for error in result.errors))

    def test_validator_rejects_stale_prefilter_date(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            add_message(con, 31, "Предлагаю занятия по шахматам")
        prefilter(settings, "2026-07", "2026-07-01")
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)):
            semantic_dedupe(settings, "2026-07")
        result = validate_period(settings, "2026-07")
        self.assertTrue(any("publication date" in error for error in result.errors))

    def test_classification_requires_completed_preparation(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            add_message(con, 32, "Предлагаю занятия по шахматам")
        with self.assertRaisesRegex(RuntimeError, "must complete before classification"):
            classify(settings, "2026-07")

    def test_validator_reports_missing_prefilter_instead_of_crashing(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            add_message(con, 37, "Предлагаю занятия по шахматам")
        result = validate_period(settings, "2026-07")
        self.assertFalse(result.ok)
        self.assertTrue(any("prefilter" in error for error in result.errors))

    def test_validator_accepts_other_safe_exclusion_for_blocked_author(self):
        settings = make_settings(self.db, {"system"})
        with connect(self.db) as con:
            message_id = add_message(con, 38, "Погода и события", "system")
            con.execute(
                """UPDATE entries SET eligible=0,excluded_reason='missing from latest export',
                   dedupe_reason='import reconciliation' WHERE message_id=?""",
                (message_id,),
            )
        result = validate_period(settings, "2026-07")
        self.assertFalse(any("excluded-author" in error for error in result.errors))

    def test_showcase_title_allows_brand_but_rejects_ukrainian_prose(self):
        self.assertEqual(_validate_showcase_title("BMW 520 TD"), "BMW 520 TD")
        with self.assertRaisesRegex(ValueError, "not normalized to Russian"):
            _validate_showcase_title("Заняття для дітей")
        with self.assertRaisesRegex(ValueError, "unbalanced punctuation"):
            _validate_showcase_title("Жильё в Пунто Прима (Торревьеха")

    def test_editorial_title_rules_preserve_intent_and_facts(self):
        with self.assertRaisesRegex(ValueError, "request into an offer"):
            _validate_showcase_title(
                "Аренда квартиры для семьи",
                "Мы в активном поиске квартиры для семьи",
            )
        with self.assertRaisesRegex(ValueError, "bedrooms into rooms"):
            _validate_showcase_title(
                "Аренда 4-комнатной квартиры",
                "Сдаётся квартира с четырьмя отдельными спальнями",
            )
        with self.assertRaisesRegex(ValueError, "bedrooms into rooms"):
            _validate_showcase_title(
                "Аренда 4-комнатной квартиры",
                "Apartment with 4 bedrooms near the sea",
            )
        with self.assertRaisesRegex(ValueError, "promotional detail"):
            _validate_showcase_title(
                "Курсы английского, первый урок в подарок"
            )
        with self.assertRaisesRegex(ValueError, "dangling word"):
            _validate_showcase_title("Пошив одежды и штор в")
        with self.assertRaisesRegex(ValueError, "unnatural search phrase"):
            _validate_showcase_title("Поиск услуг по аренде автомобилей")
        with self.assertRaisesRegex(ValueError, "adjacent prepositions"):
            _validate_showcase_title("Сниму видеоролик за в Торревьехе")
        with self.assertRaisesRegex(ValueError, "omitted the outside location"):
            _validate_showcase_title(
                "Снимаю и монтирую видеоролики",
                "РИЛС #ТОРРЕВЬЕХА. Сниму для тебя любой видеоролик",
            )
        self.assertEqual(
            _validate_showcase_title(
                "Трансферы по Испании на комфортном автомобиле",
                "Предоставляю услуги трансфера\nАэропорты Аликанте, Валенсия, Мурсия",
            ),
            "Трансферы по Испании на комфортном автомобиле",
        )
        self.assertEqual(
            _sanitize_showcase_title(
                "Поездка Торревьеха — Валенсия (ДП Документ) 3 августа",
                "Торревьеха-Валенсия (ДП Документ), возьму попутчиков",
            ),
            "Поездка Торревьеха — Валенсия 3 августа",
        )
        with self.assertRaisesRegex(ValueError, "outside their digest section"):
            _validate_category_assignment("Домашние торты", "Товары")
        _validate_category_assignment("Домашние торты", "Еда и цветы")
        _validate_category_assignment("Установка розеток", "Бытовые услуги")
        self.assertEqual(
            _validate_showcase_title(
                "Вакансии кузовщика и сварщика",
                "В автосервис требуются кузовщик и сварщик",
            ),
            "Вакансии кузовщика и сварщика",
        )
        self.assertEqual(
            _validate_showcase_title(
                "Ремонт квартир под ключ",
                "Если вам нужны ремонтные работы, обращайтесь",
            ),
            "Ремонт квартир под ключ",
        )
        self.assertEqual(
            _validate_showcase_title(
                "Съёмка и монтаж видеороликов в Торревьехе",
                "Сниму для тебя любой видеоролик: режиссура, съёмка и монтаж",
            ),
            "Съёмка и монтаж видеороликов в Торревьехе",
        )
        with self.assertRaisesRegex(ValueError, "request into an offer"):
            _validate_showcase_title(
                "Аренда квартиры в Торревьехе",
                "Сниму студию или квартиру в Торревьехе на длительный срок",
            )
        food, added = _ensure_editorial_categories([], [{"text": "Домашние торты"}])
        self.assertTrue(added)
        self.assertEqual(food[0]["title"], "Еда и доставка")
        flowers, _ = _ensure_editorial_categories([], [{"text": "Доставка букетов"}])
        self.assertEqual(flowers[0]["title"], "Цветы и букеты")
        both, _ = _ensure_editorial_categories(
            [], [{"text": "Домашние торты"}, {"text": "Доставка букетов"}]
        )
        self.assertEqual(both[0]["title"], "Еда и цветы")

    def test_model_title_is_safely_cleaned_before_checkpoint(self):
        self.assertEqual(
            _sanitize_showcase_title(
                "Массаж в Аликанте — 30 € · +34 633 114 577 · @master"
            ),
            "Массаж в Аликанте",
        )
        self.assertEqual(
            _sanitize_showcase_title("Трансфер — 633 114 577)"),
            "Трансфер",
        )
        self.assertEqual(
            _sanitize_showcase_title("Hyundai i20 2014 1.2 127 тыс.км"),
            "Hyundai i20 2014 1.2 127 тыс.км",
        )
        long_title = (
            "Индивидуальный пошив и ремонт одежды, работа с кожей и мехом, "
            "пошив штор, чехлов для мебели и выезд мастера для примерки"
        )
        shortened = _sanitize_showcase_title(long_title)
        self.assertLessEqual(len(shortened), 100)
        self.assertTrue(shortened.startswith("Индивидуальный пошив"))

    def test_single_object_json_array_is_unwrapped(self):
        self.assertEqual(_json('[{"entries": []}]'), {"entries": []})

    def test_classification_rejects_string_false_from_model(self):
        base = make_settings(self.db)
        settings = Settings(
            base.root, base.db_path, base.source_username, base.source_chat_id,
            "", "", "gemini-key", "test-model", "", "test", base.excluded_sender_ids,
        )
        with connect(self.db) as con:
            add_message(con, 33, "Предлагаю занятия по шахматам")
        prefilter(settings, "2026-07")
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)):
            semantic_dedupe(settings, "2026-07")
        responses = [
            {"candidates": [{"content": {"parts": [{"text":
                '{"categories":[{"code":"education","title":"Обучение","emoji":"📚"}]}'
            }]}}]},
            {"candidates": [{"content": {"parts": [{"text":
                '{"entries":[{"id":1,"include":"false","category":"education",'
                '"title":"Занятия по шахматам","confidence":"high"}]}'
            }]}}]},
            {"candidates": [{"content": {"parts": [{"text":
                '{"entries":[{"id":1,"include":"false","category":"education",'
                '"title":"Занятия по шахматам","confidence":"high"}]}'
            }]}}]},
        ]
        with patch("guardamar_digest.llm._post", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "non-boolean include"):
                classify(settings, "2026-07")
        with connect(self.db) as con:
            status = con.execute(
                "SELECT status FROM classification_runs WHERE period_key='2026-07'"
            ).fetchone()["status"]
        self.assertEqual(status, "running")

    def test_classification_retries_invalid_model_content_for_one_entry(self):
        base = make_settings(self.db)
        settings = Settings(
            base.root, base.db_path, base.source_username, base.source_chat_id,
            "", "", "gemini-key", "test-model", "", "test",
            base.excluded_sender_ids,
        )
        with connect(self.db) as con:
            add_message(con, 39, "Предлагаю услуги маникюра")
        prefilter(settings, "2026-07")
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)):
            semantic_dedupe(settings, "2026-07")
        category = (
            '{"categories":[{"code":"beauty","title":"Красота",'
            '"emoji":"💅"}]}'
        )
        responses = [
            {"candidates": [{"content": {"parts": [{"text": category}]}}]},
            {"candidates": [{"content": {"parts": [{"text":
                '{"entries":[]}'
            }]}}]},
            {"candidates": [{"content": {"parts": [{"text":
                '{"entries":[{"id":1,"include":true,"category":"beauty",'
                '"title":"Маникюр","confidence":"high"}]}'
            }]}}]},
        ]
        with patch("guardamar_digest.llm._post", side_effect=responses) as post:
            self.assertEqual(classify(settings, "2026-07"), "gemini")
        self.assertEqual(post.call_count, 3)
        with connect(self.db) as con:
            row = con.execute(
                "SELECT short_title,provider FROM entries WHERE period_key='2026-07'"
            ).fetchone()
        self.assertEqual(dict(row), {"short_title": "Маникюр", "provider": "gemini"})

    def test_classification_repairs_only_invalid_completed_checkpoint(self):
        base = make_settings(self.db)
        settings = Settings(
            base.root, base.db_path, base.source_username, base.source_chat_id,
            "", "", "gemini-key", "test-model", "", "test",
            base.excluded_sender_ids,
        )
        with connect(self.db) as con:
            message_id = add_message(con, 63, "Ищем квартиру для семьи")
        prefilter(settings, "2026-07")
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)):
            semantic_dedupe(settings, "2026-07")
        categories = [{"code": "realestate", "title": "Недвижимость", "emoji": "🏠"}]
        with connect(self.db) as con:
            signature = current_classification_signature(con, "2026-07")
            con.execute(
                """INSERT INTO classification_runs
                   (period_key,run_id,input_signature,categories_json,status)
                   VALUES ('2026-07','run',?,?, 'complete')""",
                (signature, json.dumps(categories, ensure_ascii=False)),
            )
            con.execute(
                """UPDATE entries SET eligible=1,category_code='realestate',
                   category_title='Недвижимость',category_emoji='🏠',
                   short_title='Аренда квартиры для семьи',classification_run_id='run'
                   WHERE message_id=?""",
                (message_id,),
            )
        response = {"candidates": [{"content": {"parts": [{"text":
            '{"entries":[{"id":1,"include":true,"category":"realestate",'
            '"title":"Семья ищет квартиру","confidence":"high"}]}'
        }]}}]}
        with patch("guardamar_digest.llm._post", return_value=response) as post:
            self.assertEqual(classify(settings, "2026-07"), "gemini")
        self.assertEqual(post.call_count, 1)
        with connect(self.db) as con:
            row = con.execute(
                "SELECT short_title,classification_run_id FROM entries WHERE message_id=?",
                (message_id,),
            ).fetchone()
        self.assertEqual(row["short_title"], "Семья ищет квартиру")
        self.assertEqual(row["classification_run_id"], "run")

    def test_classification_adds_food_section_and_reuses_other_checkpoint(self):
        base = make_settings(self.db)
        settings = Settings(
            base.root, base.db_path, base.source_username, base.source_chat_id,
            "", "", "gemini-key", "test-model", "", "test",
            base.excluded_sender_ids,
        )
        with connect(self.db) as con:
            chair = add_message(con, 64, "Продам стул IKEA")
            cakes = add_message(con, 65, "Домашние торты и пирожные")
        prefilter(settings, "2026-07")
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)):
            semantic_dedupe(settings, "2026-07")
        categories = [{"code": "goods", "title": "Товары", "emoji": "🛍"}]
        with connect(self.db) as con:
            signature = current_classification_signature(con, "2026-07")
            con.execute(
                """INSERT INTO classification_runs
                   (period_key,run_id,input_signature,categories_json,status)
                   VALUES ('2026-07','old',?,?, 'complete')""",
                (signature, json.dumps(categories, ensure_ascii=False)),
            )
            for message_id, title in ((chair, "Стул IKEA"), (cakes, "Домашние торты и пирожные")):
                con.execute(
                    """UPDATE entries SET eligible=1,category_code='goods',
                       category_title='Товары',category_emoji='🛍',short_title=?,
                       classification_run_id='old' WHERE message_id=?""",
                    (title, message_id),
                )
        response = {"candidates": [{"content": {"parts": [{"text":
            '{"entries":[{"id":2,"include":true,"category":"food_flowers",'
            '"title":"Домашние торты и пирожные","confidence":"high"}]}'
        }]}}]}
        with patch("guardamar_digest.llm._post", return_value=response) as post:
            self.assertEqual(classify(settings, "2026-07"), "gemini")
        self.assertEqual(post.call_count, 1)
        with connect(self.db) as con:
            rows = con.execute(
                "SELECT short_title,category_title FROM entries ORDER BY message_id"
            ).fetchall()
            run = con.execute(
                "SELECT categories_json,status FROM classification_runs WHERE period_key='2026-07'"
            ).fetchone()
        self.assertEqual(
            [dict(row) for row in rows],
            [
                {"short_title": "Стул IKEA", "category_title": "Товары"},
                {"short_title": "Домашние торты и пирожные", "category_title": "Еда и доставка"},
            ],
        )
        self.assertIn('"title": "Еда и доставка"', run["categories_json"])
        self.assertEqual(run["status"], "complete")

    def test_changed_month_input_reuses_unchanged_classification_checkpoints(self):
        base = make_settings(self.db)
        settings = Settings(
            base.root, base.db_path, base.source_username, base.source_chat_id,
            "", "", "gemini-key", "test-model", "", "test",
            base.excluded_sender_ids,
        )
        with connect(self.db) as con:
            add_message(con, 60, "Продам стул IKEA")
            add_message(con, 61, "Предлагаю лечебный массаж")
        prefilter(settings, "2026-07")
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)):
            semantic_dedupe(settings, "2026-07")
        category = '{"categories":[{"code":"items","title":"Объявления","emoji":"📦"}]}'
        first_responses = [
            {"candidates": [{"content": {"parts": [{"text": category}]}}]},
            {"candidates": [{"content": {"parts": [{"text":
                '{"entries":[{"id":1,"include":true,"category":"items",'
                '"title":"Стул IKEA","confidence":"high"}]}'
            }]}}]},
            {"candidates": [{"content": {"parts": [{"text":
                '{"entries":[{"id":2,"include":true,"category":"items",'
                '"title":"Лечебный массаж","confidence":"high"}]}'
            }]}}]},
        ]
        with patch("guardamar_digest.llm._post", side_effect=first_responses):
            classify(settings, "2026-07")

        with connect(self.db) as con:
            add_message(con, 62, "Ищу детское автокресло")
        prefilter(settings, "2026-07")
        dedupe(self.db, "2026-07")
        with patch("guardamar_digest.dedupe.discover_topics", return_value=(0, 0)):
            semantic_dedupe(settings, "2026-07")
        second_responses = [
            {"candidates": [{"content": {"parts": [{"text": category}]}}]},
            {"candidates": [{"content": {"parts": [{"text":
                '{"entries":[{"id":3,"include":true,"category":"items",'
                '"title":"Ищу детское автокресло","confidence":"high"}]}'
            }]}}]},
        ]
        with patch("guardamar_digest.llm._post", side_effect=second_responses) as post:
            classify(settings, "2026-07")
        self.assertEqual(post.call_count, 2)
        with connect(self.db) as con:
            titles = [row["short_title"] for row in con.execute(
                "SELECT short_title FROM entries ORDER BY message_id"
            )]
        self.assertEqual(titles, ["Стул IKEA", "Лечебный массаж", "Ищу детское автокресло"])

    def test_render_uses_plan_order_russian_month_and_linked_footer(self):
        settings = make_settings(self.db)
        with connect(self.db) as con:
            goods = add_message(con, 34, "Продам стул")
            services = add_message(con, 35, "Предлагаю массаж")
            con.execute(
                """UPDATE entries SET eligible=1,category_code='goods',
                   category_title='Товары',category_emoji='🛍',
                   short_title='Стул IKEA',classification_run_id='run'
                   WHERE message_id=?""", (goods,)
            )
            con.execute(
                """UPDATE entries SET eligible=1,category_code='services',
                   category_title='Услуги',category_emoji='🛠',
                   short_title='Лечебный массаж',classification_run_id='run'
                   WHERE message_id=?""", (services,)
            )
            con.execute(
                """INSERT INTO classification_runs
                   (period_key,run_id,input_signature,categories_json,status)
                   VALUES ('2026-07','run','sig',?,'complete')""",
                (json.dumps([
                    {"code": "services", "title": "Услуги", "emoji": "🛠"},
                    {"code": "goods", "title": "Товары", "emoji": "🛍"},
                ], ensure_ascii=False),)
            )
        output = "\n".join(render(settings, "2026-07"))
        self.assertLess(output.index("Услуги"), output.index("Товары"))
        self.assertIn("Июль 2026", output)
        self.assertIn(
            '<a href="https://t.me/MarketGuardamar">обЪявления Гуардамар</a>',
            output,
        )


if __name__ == "__main__":
    unittest.main()
