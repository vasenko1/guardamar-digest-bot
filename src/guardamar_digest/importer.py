from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .db import connect


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
    with connect(db_path) as con:
        for item in messages:
            if item.get("type") != "message" or not isinstance(item.get("id"), int):
                continue
            try:
                published = datetime.fromisoformat(item["date"])
            except (KeyError, ValueError):
                continue
            if not start <= published < end:
                continue
            text = plain_text(item.get("text")).strip()
            if not text:
                continue
            con.execute(
                """INSERT OR IGNORE INTO messages
                (chat_id,message_id,published_at,sender_name,source_text,source_url,media_type,media_group_id)
                VALUES (?,?,?,?,?,?,?,?)""",
                (chat_id, item["id"], item["date"], item.get("from", ""), text,
                 source_url(username, item["id"]), item.get("media_type"), item.get("media_group_id")),
            )
            row = con.execute("SELECT id FROM messages WHERE chat_id=? AND message_id=?", (chat_id, item["id"])).fetchone()
            con.execute("INSERT OR IGNORE INTO entries(message_id,period_key) VALUES (?,?)", (row["id"], period))
            imported += 1
    return imported
