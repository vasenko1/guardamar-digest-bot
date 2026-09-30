from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .db import connect
from .importer import source_url


MADRID = ZoneInfo("Europe/Madrid")
COVERAGE_GAP = timedelta(hours=23)


def _bot_api(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=40) as response:
        result = json.loads(response.read())
    if not isinstance(result, dict) or result.get("ok") is not True:
        description = result.get("description", "unknown Telegram error") if isinstance(result, dict) else "invalid Telegram response"
        raise RuntimeError(f"Telegram {method} failed: {description}")
    return result


def _fetch_updates(token: str, offset: int | None) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "limit": 100,
        "timeout": 0,
        "allowed_updates": ["message", "edited_message"],
    }
    if offset is not None:
        payload["offset"] = offset
    result = _bot_api(token, "getUpdates", payload).get("result", [])
    if not isinstance(result, list):
        raise RuntimeError("Telegram getUpdates returned a non-list result")
    return [item for item in result if isinstance(item, dict)]


def _sender_id(message: dict[str, Any]) -> str:
    sender_chat = message.get("sender_chat")
    if isinstance(sender_chat, dict) and isinstance(sender_chat.get("id"), int):
        chat_type = sender_chat.get("type")
        raw = str(sender_chat["id"])
        if chat_type in {"channel", "supergroup"} and raw.startswith("-100"):
            return f"channel{raw[4:]}"
        if chat_type == "group":
            return f"chat{raw.lstrip('-')}"
        return f"chat{raw.lstrip('-')}"

    sender = message.get("from")
    if isinstance(sender, dict) and isinstance(sender.get("id"), int):
        return f"user{sender['id']}"
    return ""


def _sender_name(message: dict[str, Any]) -> str:
    sender_chat = message.get("sender_chat")
    if isinstance(sender_chat, dict):
        return str(sender_chat.get("title") or sender_chat.get("username") or "")
    sender = message.get("from")
    if not isinstance(sender, dict):
        return ""
    parts = [
        str(sender.get("first_name") or "").strip(),
        str(sender.get("last_name") or "").strip(),
    ]
    name = " ".join(part for part in parts if part).strip()
    return name or str(sender.get("username") or "")


def _media_type(message: dict[str, Any]) -> str | None:
    for field in (
        "photo", "video", "document", "animation", "audio", "voice",
        "video_note", "sticker",
    ):
        if message.get(field):
            return field
    return None


def _period_and_published(unix_timestamp: int) -> tuple[str, str]:
    published = datetime.fromtimestamp(unix_timestamp, timezone.utc).astimezone(MADRID)
    period = f"{published.year:04d}-{published.month:02d}"
    return period, published.replace(tzinfo=None).isoformat()


def _mark_period_pending(con, period: str) -> None:
    for stage in ("prefilter", "semantic_dedupe"):
        con.execute(
            """INSERT OR REPLACE INTO workflow_runs
               (period_key,stage,rule_version,status,details,completed_at)
               VALUES (?,?,'','pending',NULL,CURRENT_TIMESTAMP)""",
            (period, stage),
        )


def _apply_message(
    con, settings, message: dict[str, Any], *, is_edited: bool = False
) -> tuple[str, str | None]:
    chat = message.get("chat")
    if not isinstance(chat, dict) or str(chat.get("id", "")) != str(settings.source_chat_id):
        return "ignored_chat", None

    sender = message.get("from")
    sender_chat = message.get("sender_chat")
    if (
        isinstance(sender, dict)
        and sender.get("is_bot") is True
        and not isinstance(sender_chat, dict)
    ):
        return "ignored_bot", None

    message_id = message.get("message_id")
    timestamp = message.get("date")
    if not isinstance(message_id, int) or not isinstance(timestamp, int):
        return "ignored_invalid", None

    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text:
        if not is_edited:
            return "ignored_empty", None
        existing = con.execute(
            """SELECT m.id,e.period_key FROM messages m
               JOIN entries e ON e.message_id=m.id
               WHERE m.chat_id=? AND m.message_id=?""",
            (str(settings.source_chat_id), message_id),
        ).fetchone()
        if existing is None:
            return "ignored_empty", None
        con.execute(
            """UPDATE entries SET eligible=0,excluded_reason='source text removed',
               dedupe_reason='collector edit',duplicate_of=NULL,
               dedupe_confidence=NULL,dedupe_version=NULL,
               needs_duplicate_review=0 WHERE message_id=?""",
            (existing["id"],),
        )
        con.execute(
            "DELETE FROM editorial_audit WHERE period_key=? AND message_id=?",
            (existing["period_key"], existing["id"]),
        )
        _mark_period_pending(con, existing["period_key"])
        return "excluded_empty_edit", existing["period_key"]

    period, published_at = _period_and_published(timestamp)
    values = {
        "published_at": published_at,
        "sender_name": _sender_name(message),
        "sender_id": _sender_id(message),
        "source_text": text,
        "source_url": source_url(settings.source_username, message_id),
        "media_type": _media_type(message),
        "media_group_id": str(message.get("media_group_id") or "") or None,
    }
    existing = con.execute(
        """SELECT id,published_at,sender_name,sender_id,source_text,source_url,
                  media_type,media_group_id
           FROM messages WHERE chat_id=? AND message_id=?""",
        (str(settings.source_chat_id), message_id),
    ).fetchone()

    if existing is None:
        cursor = con.execute(
            """INSERT INTO messages
               (chat_id,message_id,published_at,sender_name,sender_id,source_text,
                source_url,media_type,media_group_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                str(settings.source_chat_id), message_id, values["published_at"],
                values["sender_name"], values["sender_id"], values["source_text"],
                values["source_url"], values["media_type"], values["media_group_id"],
            ),
        )
        internal_id = cursor.lastrowid
        con.execute(
            "INSERT INTO entries(message_id,period_key) VALUES (?,?)",
            (internal_id, period),
        )
        _mark_period_pending(con, period)
        return "inserted", period

    changed = any(
        existing[column] != values[column]
        for column in (
            "published_at", "sender_name", "sender_id", "source_text",
            "source_url", "media_type", "media_group_id",
        )
    )
    internal_id = existing["id"]
    con.execute(
        "INSERT OR IGNORE INTO entries(message_id,period_key) VALUES (?,?)",
        (internal_id, period),
    )
    con.execute(
        """UPDATE entries SET eligible=1,excluded_reason=NULL,dedupe_reason=NULL
           WHERE message_id=? AND period_key=?
             AND dedupe_reason IN ('import reconciliation','collector edit')""",
        (internal_id, period),
    )
    if not changed:
        return "unchanged", period

    con.execute(
        """UPDATE messages SET
             published_at=?,sender_name=?,sender_id=?,source_text=?,
             source_url=?,media_type=?,media_group_id=?
           WHERE id=?""",
        (
            values["published_at"], values["sender_name"], values["sender_id"],
            values["source_text"], values["source_url"], values["media_type"],
            values["media_group_id"], internal_id,
        ),
    )
    con.execute(
        "DELETE FROM editorial_audit WHERE period_key=? AND message_id=?",
        (period, internal_id),
    )
    _mark_period_pending(con, period)
    return "updated", period


def _coverage_state(existing, now: datetime) -> tuple[str, str]:
    if existing is None:
        local_now = now.astimezone(MADRID)
        month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elapsed = local_now - month_start
        if elapsed < COVERAGE_GAP:
            return "ok", "collector started within the first 23 hours of the local month"
        return "uncertain", "first collector run started more than 23 hours after local month start"

    status = str(existing["coverage_status"] or "unknown")
    reason = str(existing["coverage_reason"] or "")
    last_success_raw = existing["last_success_at"]
    if last_success_raw:
        last_success = datetime.fromisoformat(last_success_raw)
        if last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=timezone.utc)
        gap = now - last_success.astimezone(timezone.utc)
        if gap >= COVERAGE_GAP:
            return "uncertain", f"collector gap reached {gap.total_seconds() / 3600:.1f} hours"
    if status == "unknown":
        return "ok", "collector resumed without a detected coverage gap"
    return status, reason


def collect(
    settings,
    *,
    now: datetime | None = None,
    fetcher: Callable[[str, int | None], list[dict[str, Any]]] = _fetch_updates,
) -> dict[str, Any]:
    if not settings.bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    if not settings.source_chat_id:
        raise RuntimeError("TELEGRAM_SOURCE_CHAT_ID is required")
    if not settings.source_username:
        raise RuntimeError("TELEGRAM_SOURCE_USERNAME is required")

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    with connect(settings.db_path) as con:
        state = con.execute(
            "SELECT * FROM collector_state WHERE singleton=1"
        ).fetchone()
        last_update_id = state["last_update_id"] if state else None
        coverage_status, coverage_reason = _coverage_state(state, current)

    offset = last_update_id + 1 if isinstance(last_update_id, int) else None
    stats = {
        "updates": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "ignored_chat": 0,
        "ignored_bot": 0,
        "ignored_empty": 0,
        "ignored_invalid": 0,
        "excluded_empty_edit": 0,
    }

    while True:
        updates = fetcher(settings.bot_token, offset)
        if not updates:
            break

        max_update_id = last_update_id
        with connect(settings.db_path) as con:
            for update in updates:
                update_id = update.get("update_id")
                if not isinstance(update_id, int):
                    continue
                stats["updates"] += 1
                edited = isinstance(update.get("edited_message"), dict)
                message = update.get("edited_message") or update.get("message")
                if isinstance(message, dict):
                    outcome, _ = _apply_message(
                        con, settings, message, is_edited=edited
                    )
                    stats[outcome] = stats.get(outcome, 0) + 1
                if max_update_id is None or update_id > max_update_id:
                    max_update_id = update_id

            if max_update_id is not None:
                con.execute(
                    """INSERT INTO collector_state
                       (singleton,last_update_id,last_success_at,coverage_status,
                        coverage_reason,started_at)
                       VALUES (1,?,?,?,?,?)
                       ON CONFLICT(singleton) DO UPDATE SET
                         last_update_id=excluded.last_update_id,
                         coverage_status=excluded.coverage_status,
                         coverage_reason=excluded.coverage_reason""",
                    (
                        max_update_id,
                        state["last_success_at"] if state else None,
                        coverage_status,
                        coverage_reason,
                        state["started_at"] if state else current.isoformat(),
                    ),
                )
        last_update_id = max_update_id
        offset = last_update_id + 1 if isinstance(last_update_id, int) else offset
        if len(updates) < 100:
            break

    with connect(settings.db_path) as con:
        con.execute(
            """INSERT INTO collector_state
               (singleton,last_update_id,last_success_at,coverage_status,
                coverage_reason,started_at)
               VALUES (1,?,?,?,?,?)
               ON CONFLICT(singleton) DO UPDATE SET
                 last_update_id=excluded.last_update_id,
                 last_success_at=excluded.last_success_at,
                 coverage_status=excluded.coverage_status,
                 coverage_reason=excluded.coverage_reason""",
            (
                last_update_id,
                current.isoformat(),
                coverage_status,
                coverage_reason,
                state["started_at"] if state else current.isoformat(),
            ),
        )

    stats["last_update_id"] = last_update_id
    stats["coverage_status"] = coverage_status
    stats["coverage_reason"] = coverage_reason
    return stats


def collector_status(settings, *, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    local_now = current.astimezone(MADRID)
    period = f"{local_now.year:04d}-{local_now.month:02d}"
    with connect(settings.db_path) as con:
        state = con.execute(
            "SELECT * FROM collector_state WHERE singleton=1"
        ).fetchone()
        count = con.execute(
            "SELECT COUNT(*) FROM entries WHERE period_key=?",
            (period,),
        ).fetchone()[0]
    return {
        "period": period,
        "entries": count,
        "last_update_id": state["last_update_id"] if state else None,
        "last_success_at": state["last_success_at"] if state else None,
        "coverage_status": state["coverage_status"] if state else "not_started",
        "coverage_reason": state["coverage_reason"] if state else "collector has not run yet",
        "started_at": state["started_at"] if state else None,
    }
