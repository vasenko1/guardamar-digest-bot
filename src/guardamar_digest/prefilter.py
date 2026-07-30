from __future__ import annotations

import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .db import connect


VERSION = "2026-07-30.4"
GENERIC_ONLY = re.compile(
    r"^(?:прода[её]тся|продам|торг|возможен торг|возможно торг|"
    r"актуально|срочно|подробности в личк[еу]|пишите в личк[еу])[\s.!?,…-]*$",
    re.I,
)
SUBJECT_WORD = re.compile(r"[\wа-яёіїєґáéíóúüñ]{3,}", re.I)
DATED_ACTIVITY = re.compile(
    r"\b(?:поездк\w*|еду|їхат\w*|попут\w*|мероприят\w*|событи\w*|"
    r"игр\w*|мафи\w*|мастер[\s-]?класс\w*|ретрит\w*|экскурси\w*|"
    r"спектакл\w*|концерт\w*|лагер\w*|"
    r"доступн[а-яіїєґ]*\s+с|сдам\s+с)\b",
    re.I,
)
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}


def _period_cutoff(period: str, as_of: str | None) -> date:
    if as_of:
        return date.fromisoformat(as_of)
    # Expiration is relative to the actual preparation/publication day, not to
    # the formal end of a historical month.
    return datetime.now(ZoneInfo("Europe/Madrid")).date()


def _explicit_dates(text: str, period: str) -> list[date]:
    year, default_month = map(int, period.split("-"))
    found: list[date] = []
    for day, month, explicit_year in re.findall(
        r"(?<!\d)([0-3]?\d)[./-]([01]?\d)(?:[./-](20\d{2}))?(?!\d)", text
    ):
        try:
            parsed_month = int(month)
            parsed_year = int(explicit_year or (year + 1 if default_month == 12 and parsed_month == 1 else year))
            found.append(date(parsed_year, parsed_month, int(day)))
        except ValueError:
            pass
    month_names = "|".join(map(re.escape, MONTHS))
    for day, month_name in re.findall(
        rf"(?<!\d)([0-3]?\d)\s+({month_names})\b", text.casefold()
    ):
        try:
            parsed_month = MONTHS[month_name]
            parsed_year = year + 1 if default_month == 12 and parsed_month == 1 else year
            found.append(date(parsed_year, parsed_month, int(day)))
        except ValueError:
            pass
    # A bare day is intentionally not parsed: it is too easy to mistake a
    # price, quantity or address for a date.
    return found


def _audit(con, period: str, message_id: int, decision: str, code: str,
           detail: str, confidence: str = "high") -> None:
    con.execute(
        """INSERT OR REPLACE INTO editorial_audit
           (period_key,message_id,stage,decision,reason_code,reason_detail,
            confidence,provider,rule_version)
           VALUES (?,?,'prefilter',?,?,?,?, 'rule',?)""",
        (period, message_id, decision, code, detail, confidence, VERSION),
    )


def prefilter(settings, period: str, as_of: str | None = None) -> dict[str, int]:
    """Apply only deterministic, high-precision exclusions and audit each one."""
    cutoff = _period_cutoff(period, as_of)
    counts = {"examined": 0, "excluded_author": 0, "excluded_incomplete": 0,
              "excluded_expired": 0, "kept": 0}
    with connect(settings.db_path) as con:
        rows = con.execute(
            """SELECT m.id,m.sender_id,m.source_text,e.excluded_reason,e.dedupe_reason
               FROM messages m JOIN entries e ON e.message_id=m.id
               WHERE e.period_key=? ORDER BY m.message_id""",
            (period,),
        ).fetchall()
        for row in rows:
            counts["examined"] += 1
            # An editor's explicit exclusion is immutable. Automatic duplicate
            # marks are re-examined because the source text may have changed
            # between exports.
            if row["excluded_reason"] and row["dedupe_reason"] in {
                "editor exclusion", "import reconciliation",
            }:
                _audit(con, period, row["id"], "preserve", "existing_decision",
                       row["excluded_reason"])
                continue
            text = " ".join(row["source_text"].split())
            code = detail = ""
            if row["sender_id"] in settings.excluded_sender_ids:
                code, detail = "author", f"sender_id={row['sender_id']}"
            elif GENERIC_ONLY.fullmatch(text) or not SUBJECT_WORD.search(text):
                code, detail = "incomplete", "no identifiable product, service or request"
            else:
                dates = _explicit_dates(text, period)
                # Exclude only clearly date-bound activities when every explicit
                # date is already over. A range reaching the cutoff/next month
                # remains eligible.
                if dates and DATED_ACTIVITY.search(text) and max(dates) < cutoff:
                    code, detail = "expired", f"latest explicit date={max(dates).isoformat()}; as_of={cutoff.isoformat()}"
            if code:
                con.execute(
                    """UPDATE entries SET eligible=0,excluded_reason=?,
                       duplicate_of=NULL,dedupe_reason=?,dedupe_confidence=NULL,
                       dedupe_version=NULL,needs_duplicate_review=0
                       WHERE message_id=?""",
                    (code, f"prefilter:{code}", row["id"]),
                )
                counts[f"excluded_{code}"] += 1
                _audit(con, period, row["id"], "exclude", code, detail)
            else:
                # Clear only a previous decision made by this exact stage.
                con.execute(
                    """UPDATE entries SET eligible=1,excluded_reason=NULL,
                       dedupe_reason=NULL WHERE message_id=?
                       AND dedupe_reason LIKE 'prefilter:%'""",
                    (row["id"],),
                )
                counts["kept"] += 1
                _audit(con, period, row["id"], "keep", "passed",
                       "no high-confidence exclusion rule matched")
        # Any changed eligibility can alter duplicate clusters. A fresh
        # semantic stage is mandatory before publication.
        con.execute(
            """INSERT OR REPLACE INTO workflow_runs
               (period_key,stage,rule_version,status,details,completed_at)
               VALUES (?,'prefilter',?,'complete',?,CURRENT_TIMESTAMP)""",
            (period, VERSION, json.dumps({"as_of": cutoff.isoformat()})),
        )
        con.execute(
            """INSERT OR REPLACE INTO workflow_runs
               (period_key,stage,rule_version,status,details,completed_at)
               VALUES (?,'semantic_dedupe','', 'pending',NULL,CURRENT_TIMESTAMP)""",
            (period,),
        )
    return counts


def audit_report(db_path, period: str) -> str:
    with connect(db_path) as con:
        rows = con.execute(
            """SELECT a.decision,a.reason_code,COUNT(*) AS count
               FROM editorial_audit a WHERE a.period_key=? AND a.stage='prefilter'
               GROUP BY a.decision,a.reason_code ORDER BY a.decision,a.reason_code""",
            (period,),
        ).fetchall()
    return "\n".join(
        f"{row['decision']}: {row['reason_code']} — {row['count']}" for row in rows
    ) or "Аудит предфильтра пуст."
