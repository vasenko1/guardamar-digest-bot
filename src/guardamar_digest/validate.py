from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from .db import connect
from .dedupe import VERSION as DEDUPE_VERSION
from .prefilter import VERSION as PREFILTER_VERSION, _period_cutoff
from .render import telegram_length
from .llm import (
    PHONE_NUMBER,
    TITLE_MAX_LENGTH,
    _validate_category_assignment,
    _validate_showcase_title,
    classification_signature,
    prepare_rows,
)


PHONE = PHONE_NUMBER
CONTACT = re.compile(r"https?://|www\.|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|@\w+", re.I)
PRICE = re.compile(r"(?:\d[\d\s.,]*\s*(?:€|eur\b|евро\b|₽|\$|грн\b)|(?:€|\$)\s*\d)", re.I)
GENERIC = re.compile(
    r"^(?:объявление|продажа товара(?:\s+за)?|прода[её]тся|стабільна ціна|"
    r"возможно торг|рекомендация услуг)\W*$", re.I
)
UKRAINIAN_ONLY = re.compile(r"[іїєґ]", re.I)
TELEGRAM_USERNAME = re.compile(r"^[A-Za-z0-9_]{5,}$")
RUSSIAN_LETTER = re.compile(r"[а-яё]", re.I)


class _StrictTelegramHTML(HTMLParser):
    allowed = {"b", "a"}

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.allowed:
            raise ValueError(f"unsupported HTML tag <{tag}>")
        if tag == "a" and not dict(attrs).get("href", "").startswith("https://t.me/"):
            raise ValueError("non-Telegram link")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in self.allowed:
            raise ValueError(f"unsupported HTML tag </{tag}>")
        if not self.stack or self.stack.pop() != tag:
            raise ValueError(f"mismatched closing tag </{tag}>")

    def close(self):
        super().close()
        if self.stack:
            raise ValueError(f"unclosed HTML tag <{self.stack[-1]}>")


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def require_ok(self) -> None:
        if self.errors:
            raise RuntimeError(
                "Publication blocked:\n- " + "\n- ".join(self.errors)
            )


def validate_period(settings, period: str, rendered_parts: list[str] | None = None) -> ValidationResult:
    errors: list[str] = []
    if not TELEGRAM_USERNAME.fullmatch(settings.source_username):
        errors.append("TELEGRAM_SOURCE_USERNAME is missing or invalid")
    with connect(settings.db_path) as con:
        run = con.execute(
            """SELECT run_id,status,input_signature FROM classification_runs
               WHERE period_key=?""", (period,)
        ).fetchone()
        if not run or run["status"] != "complete":
            errors.append("classification run is not complete")
            run_id = ""
        else:
            run_id = run["run_id"]
        signature_records = con.execute(
            """SELECT m.id,m.source_text FROM messages m
               JOIN entries e ON e.message_id=m.id
               WHERE e.period_key=? AND e.manual_title IS NULL
                 AND e.excluded_reason IS NULL AND e.needs_duplicate_review=0
               ORDER BY m.message_id""",
            (period,),
        ).fetchall()
        current_signature = classification_signature(
            prepare_rows(signature_records)
        ) if signature_records else ""
        if run and run["input_signature"] != current_signature:
            errors.append("classification input signature is stale")
        total_entries = con.execute(
            "SELECT COUNT(*) FROM entries WHERE period_key=?", (period,)
        ).fetchone()[0]
        audited_entries = con.execute(
            """SELECT COUNT(DISTINCT message_id) FROM editorial_audit
               WHERE period_key=? AND stage='prefilter' AND rule_version=?""",
            (period, PREFILTER_VERSION),
        ).fetchone()[0]
        if audited_entries != total_entries:
            errors.append(
                f"current prefilter did not audit every entry ({audited_entries}/{total_entries})"
            )
        prefilter_run = con.execute(
            """SELECT status,rule_version,details FROM workflow_runs
               WHERE period_key=? AND stage='prefilter'""", (period,)
        ).fetchone()
        expected_cutoff = _period_cutoff(period, None).isoformat()
        try:
            recorded_cutoff = (
                json.loads(prefilter_run["details"] or "{}").get("as_of")
                if prefilter_run else None
            )
        except (TypeError, AttributeError, json.JSONDecodeError):
            recorded_cutoff = None
        if (
            not prefilter_run or prefilter_run["status"] != "complete"
            or prefilter_run["rule_version"] != PREFILTER_VERSION
            or recorded_cutoff != expected_cutoff
        ):
            errors.append(
                f"current prefilter was not completed for publication date {expected_cutoff}"
            )
        dedupe_run = con.execute(
            """SELECT status,rule_version FROM workflow_runs
               WHERE period_key=? AND stage='semantic_dedupe'""", (period,)
        ).fetchone()
        if not dedupe_run or dedupe_run["status"] != "complete" or dedupe_run["rule_version"] != DEDUPE_VERSION:
            errors.append("current automatic duplicate arbitration was not completed")
        unresolved = con.execute(
            """SELECT COUNT(*) FROM duplicate_reviews
               WHERE period_key=? AND status IN ('pending','uncertain')""", (period,)
        ).fetchone()[0]
        if unresolved:
            errors.append(f"{unresolved} duplicate decisions are unresolved")
        review_flags = con.execute(
            """SELECT COUNT(*) FROM entries WHERE period_key=?
               AND needs_duplicate_review=1""", (period,)
        ).fetchone()[0]
        if review_flags:
            errors.append(f"{review_flags} entries still have duplicate-review flags")
        broken_canonicals = con.execute(
            """SELECT COUNT(*) FROM entries duplicate
               LEFT JOIN entries keeper ON keeper.message_id=duplicate.duplicate_of
               WHERE duplicate.period_key=? AND duplicate.excluded_reason='duplicate'
                 AND (
                   keeper.message_id IS NULL OR keeper.period_key<>duplicate.period_key
                   OR keeper.eligible<>1 OR keeper.excluded_reason IS NOT NULL
                 )""",
            (period,),
        ).fetchone()[0]
        if broken_canonicals:
            errors.append(
                f"{broken_canonicals} duplicates point to a missing or excluded canonical entry"
            )
        configured_author_misses = 0
        if settings.excluded_sender_ids:
            marks = ",".join("?" for _ in settings.excluded_sender_ids)
            configured_author_misses = con.execute(
                f"""SELECT COUNT(*) FROM messages m JOIN entries e ON e.message_id=m.id
                    WHERE e.period_key=? AND m.sender_id IN ({marks})
                      AND e.excluded_reason IS NULL""",
                (period, *settings.excluded_sender_ids),
            ).fetchone()[0]
        if configured_author_misses:
            errors.append(
                f"{configured_author_misses} configured excluded-author messages remain publishable"
            )
        missing = con.execute(
            """SELECT COUNT(*) FROM entries
               WHERE period_key=? AND excluded_reason IS NULL
                 AND manual_title IS NULL
                 AND (classification_run_id IS NULL OR classification_run_id<>?)""",
            (period, run_id),
        ).fetchone()[0]
        if missing:
            errors.append(f"{missing} eligible candidates were not classified in the current run")
        rows = con.execute(
            """SELECT m.message_id,m.sender_id,m.source_url,m.source_text,
                      e.short_title,e.manual_title,
                      e.category_code,e.category_title,e.manual_category
               FROM entries e JOIN messages m ON m.id=e.message_id
               WHERE e.period_key=? AND e.eligible=1 AND e.excluded_reason IS NULL""",
            (period,),
        ).fetchall()
    seen_urls: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    expected_prefix = f"https://t.me/{settings.source_username}/"
    for row in rows:
        external_id = row["message_id"]
        title = (row["manual_title"] or row["short_title"] or "").strip()
        category = (row["manual_category"] or row["category_title"] or "").strip()
        if not title:
            errors.append(f"message {external_id}: empty showcase title")
            continue
        if not category or not row["category_code"]:
            errors.append(f"message {external_id}: missing category")
        elif not RUSSIAN_LETTER.search(category) or UKRAINIAN_ONLY.search(category):
            errors.append(f"message {external_id}: category title is not Russian")
        if PHONE.search(title) or CONTACT.search(title):
            errors.append(f"message {external_id}: contact or URL leaked into title")
        if PRICE.search(title):
            errors.append(f"message {external_id}: price leaked into title")
        if GENERIC.fullmatch(title):
            errors.append(f"message {external_id}: generic non-informative title")
        if len(title) > TITLE_MAX_LENGTH:
            errors.append(
                f"message {external_id}: showcase title exceeds {TITLE_MAX_LENGTH} characters"
            )
        if title.count("(") != title.count(")") or title.count("[") != title.count("]"):
            errors.append(f"message {external_id}: unbalanced punctuation in title")
        if UKRAINIAN_ONLY.search(title):
            errors.append(f"message {external_id}: showcase title is not normalized to Russian")
        try:
            _validate_showcase_title(title, row["source_text"])
        except ValueError as exc:
            errors.append(f"message {external_id}: {exc}")
        try:
            _validate_category_assignment(row["source_text"], category)
        except ValueError as exc:
            errors.append(f"message {external_id}: {exc}")
        if row["sender_id"] in settings.excluded_sender_ids:
            errors.append(f"message {external_id}: excluded author reached publication")
        expected_url = f"{expected_prefix}{external_id}"
        if row["source_url"] != expected_url:
            errors.append(f"message {external_id}: invalid source URL")
        if row["source_url"] in seen_urls:
            errors.append(f"message {external_id}: duplicate source URL")
        seen_urls.add(row["source_url"])
        title_key = (category.casefold(), " ".join(title.casefold().split()))
        if title_key in seen_titles:
            errors.append(f"message {external_id}: duplicate title inside category")
        seen_titles.add(title_key)
    if not rows:
        errors.append("digest has no publishable entries")
    for number, part in enumerate(rendered_parts or (), 1):
        part_length = telegram_length(part)
        if part_length > 4096:
            errors.append(f"part {number}: Telegram limit exceeded ({part_length} UTF-16 units)")
        parser = _StrictTelegramHTML()
        try:
            parser.feed(part)
            parser.close()
        except ValueError as exc:
            errors.append(f"part {number}: invalid Telegram HTML: {exc}")
    return ValidationResult(tuple(dict.fromkeys(errors)))
