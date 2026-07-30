from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .db import connect


MADRID = ZoneInfo("Europe/Madrid")


def plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in value
        )
    return ""


def month_bounds(period: str) -> tuple[datetime, datetime]:
    year, month = map(int, period.split("-"))
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end


def source_url(username: str, message_id: int) -> str:
    if not username:
        raise ValueError("TELEGRAM_SOURCE_USERNAME is required for direct source links")
    return f"https://t.me/{username}/{message_id}"


def import_export(db_path: Path, export_path: Path, period: str, chat_id: str, username: str) -> int:
    start, end = month_bounds(period)
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    messages: Iterable[dict[str, Any]] = payload.get("messages", [])
    imported = 0
    seen_internal_ids: set[int] = set()
    seen_dates: list[datetime] = []
    with connect(db_path) as con:
        for item in messages:
            if item.get("type") != "message" or not isinstance(item.get("id"), int):
                continue
            try:
                raw_date = item["date"]
                if not isinstance(raw_date, str):
                    continue
                if raw_date.endswith("Z"):
                    raw_date = raw_date[:-1] + "+00:00"
                published = datetime.fromisoformat(raw_date)
            except (KeyError, ValueError):
                continue
            if published.tzinfo is not None:
                published = published.astimezone(MADRID).replace(tzinfo=None)
            if not start <= published < end:
                continue
            text = plain_text(item.get("text")).strip()
            if not text:
                continue
            con.execute(
                """INSERT INTO messages
                (chat_id,message_id,published_at,sender_name,sender_id,source_text,source_url,media_type,media_group_id)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(chat_id,message_id) DO UPDATE SET
                  published_at=excluded.published_at, sender_name=excluded.sender_name,
                  sender_id=excluded.sender_id, source_text=excluded.source_text,
                  source_url=excluded.source_url, media_type=excluded.media_type,
                  media_group_id=excluded.media_group_id""",
                (chat_id, item["id"], published.isoformat(), item.get("from", ""), item.get("from_id", ""), text,
                 source_url(username, item["id"]), item.get("media_type"), item.get("media_group_id")),
            )
            row = con.execute("SELECT id FROM messages WHERE chat_id=? AND message_id=?", (chat_id, item["id"])).fetchone()
            con.execute("INSERT OR IGNORE INTO entries(message_id,period_key) VALUES (?,?)", (row["id"], period))
            con.execute(
                """UPDATE entries SET eligible=1,excluded_reason=NULL,dedupe_reason=NULL
                   WHERE message_id=? AND period_key=?
                     AND dedupe_reason='import reconciliation'""",
                (row["id"], period),
            )
            # Re-import may change text or author. Force every derived stage to
            # re-audit this source record before it can be published.
            con.execute(
                "DELETE FROM editorial_audit WHERE period_key=? AND message_id=?",
                (period, row["id"]),
            )
            seen_internal_ids.add(row["id"])
            seen_dates.append(published)
            imported += 1
        # A later full/partial export can omit messages deleted by their author.
        # Reconcile only inside the time span actually present in this file;
        # messages outside it are untouched and raw records are never deleted.
        if seen_dates:
            coverage_start, coverage_end = min(seen_dates), max(seen_dates)
            con.execute(
                "CREATE TEMP TABLE IF NOT EXISTS import_seen(message_id INTEGER PRIMARY KEY)"
            )
            con.execute("DELETE FROM import_seen")
            con.executemany(
                "INSERT INTO import_seen(message_id) VALUES (?)",
                ((message_id,) for message_id in seen_internal_ids),
            )
            con.execute(
                """UPDATE entries SET eligible=0,excluded_reason=?,dedupe_reason=?,
                   duplicate_of=NULL,dedupe_confidence=NULL,dedupe_version=NULL,
                   needs_duplicate_review=0
                   WHERE period_key=? AND message_id IN (
                     SELECT id FROM messages WHERE chat_id=?
                       AND published_at>=? AND published_at<=?
                   ) AND message_id NOT IN (SELECT message_id FROM import_seen)""",
                ("missing from latest export", "import reconciliation",
                 period, chat_id, coverage_start.isoformat(), coverage_end.isoformat()),
            )
        for stage in ("prefilter", "semantic_dedupe"):
            con.execute(
                """INSERT OR REPLACE INTO workflow_runs
                   (period_key,stage,rule_version,status,details,completed_at)
                   VALUES (?,?,'','pending',NULL,CURRENT_TIMESTAMP)""",
                (period, stage),
            )
    return imported
