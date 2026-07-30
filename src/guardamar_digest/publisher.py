from __future__ import annotations

from collections.abc import Callable

from .db import connect


def publish_parts(settings, period: str, parts: list[str],
                  sender: Callable[[str, str, str], dict]) -> dict[str, int]:
    """Send only missing parts and checkpoint each successful Telegram result."""
    destination = settings.source_chat_id
    with connect(settings.db_path) as con:
        existing = con.execute(
            """SELECT part_no,rendered_html,sent_at FROM publications
               WHERE period_key=? AND destination=? ORDER BY part_no""",
            (period, destination),
        ).fetchall()
    for row in existing:
        if row["sent_at"] and row["part_no"] > len(parts):
            raise RuntimeError(
                "Publication blocked: already published digest has more parts"
            )
        if row["sent_at"] and row["rendered_html"] != parts[row["part_no"] - 1]:
            raise RuntimeError(
                f"Publication blocked: already sent part {row['part_no']} changed"
            )

    sent = skipped = 0
    for part_no, part in enumerate(parts, 1):
        with connect(settings.db_path) as con:
            row = con.execute(
                """SELECT rendered_html,sent_at FROM publications
                   WHERE period_key=? AND destination=? AND part_no=?""",
                (period, destination, part_no),
            ).fetchone()
            if row and row["sent_at"]:
                skipped += 1
                continue
            con.execute(
                """INSERT INTO publications
                   (period_key,destination,part_no,rendered_html)
                   VALUES (?,?,?,?)
                   ON CONFLICT(period_key,destination,part_no)
                   DO UPDATE SET rendered_html=excluded.rendered_html
                   WHERE publications.sent_at IS NULL""",
                (period, destination, part_no, part),
            )
        response = sender(settings.bot_token, destination, part)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise RuntimeError(f"Telegram rejected publication part {part_no}")
        message_id = (response.get("result") or {}).get("message_id")
        if not isinstance(message_id, int):
            raise RuntimeError(
                f"Telegram response for publication part {part_no} has no message_id"
            )
        with connect(settings.db_path) as con:
            con.execute(
                """UPDATE publications SET telegram_message_id=?,
                   sent_at=CURRENT_TIMESTAMP,rendered_html=?
                   WHERE period_key=? AND destination=? AND part_no=?""",
                (message_id, part, period, destination, part_no),
            )
        sent += 1
    return {"sent": sent, "skipped": skipped, "parts": len(parts)}
