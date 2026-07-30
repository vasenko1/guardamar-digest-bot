from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher

from .db import connect


VERSION = "2026-07-30.1"
URL_OR_CONTACT = re.compile(r"https?://\S+|(?:@\w+)|\+?\d[\d ()-]{7,}")
WORDS = re.compile(r"[\wа-яёáéíóúüñ]{3,}", re.I)
STOP_WORDS = {
    "это", "как", "для", "или", "что", "при", "без", "все", "the", "and",
    "una", "por", "con", "del", "las", "los", "есть", "будет", "можно",
}


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = URL_OR_CONTACT.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def tokens(text: str) -> set[str]:
    return {word.casefold() for word in WORDS.findall(normalized(text)) if word.casefold() not in STOP_WORDS}


def similarity(left: str, right: str) -> float:
    left_normal, right_normal = normalized(left), normalized(right)
    if left_normal == right_normal:
        return 1.0
    left_words, right_words = tokens(left), tokens(right)
    if not left_words or not right_words:
        return 0.0
    overlap = len(left_words & right_words) / len(left_words | right_words)
    sequence = SequenceMatcher(None, left_normal, right_normal).ratio()
    return max(overlap, sequence)


def _canonical(rows: list) -> object:
    # The latest full message is the link readers should open. This is deliberate:
    # edits/reposts usually contain the freshest availability information.
    return max(rows, key=lambda row: (row["published_at"], row["message_id"]))


def dedupe(db_path, period: str) -> dict[str, int]:
    with connect(db_path) as con:
        # Re-running is idempotent. Manual editorial exclusions are never touched.
        con.execute(
            """UPDATE entries SET excluded_reason=NULL, duplicate_of=NULL,
               dedupe_reason=NULL, dedupe_confidence=NULL, dedupe_version=NULL,
               needs_duplicate_review=0
               WHERE period_key=? AND dedupe_version IS NOT NULL""",
            (period,),
        )
        con.execute(
            """UPDATE entries SET eligible=1, category_code=NULL, category_title=NULL,
               category_emoji=NULL, short_title=NULL, confidence=NULL, provider=NULL
               WHERE period_key=? AND manual_title IS NULL""",
            (period,),
        )
        rows = con.execute(
            """SELECT m.id, m.message_id, m.published_at, m.sender_id, m.source_text
               FROM messages m JOIN entries e ON e.message_id=m.id
               WHERE e.period_key=? ORDER BY m.sender_id, m.published_at, m.message_id""",
            (period,),
        ).fetchall()

        by_author: dict[str, list] = defaultdict(list)
        for row in rows:
            # Missing immutable author id is unsafe for automatic matching.
            if row["sender_id"]:
                by_author[row["sender_id"]].append(row)

        auto_duplicates = 0
        review_pairs = 0
        for author_rows in by_author.values():
            exact: dict[str, list] = defaultdict(list)
            for row in author_rows:
                exact[normalized(row["source_text"])].append(row)
            consumed: set[int] = set()
            for cluster in exact.values():
                if len(cluster) < 2:
                    continue
                keeper = _canonical(cluster)
                for row in cluster:
                    if row["id"] == keeper["id"]:
                        continue
                    con.execute(
                        """UPDATE entries SET eligible=0, excluded_reason='duplicate',
                           duplicate_of=?, dedupe_reason='exact same author',
                           dedupe_confidence=1.0, dedupe_version=? WHERE message_id=?""",
                        (keeper["id"], VERSION, row["id"]),
                    )
                    consumed.add(row["id"])
                    auto_duplicates += 1
            remaining = [row for row in author_rows if row["id"] not in consumed]
            for index, left in enumerate(remaining):
                for right in remaining[index + 1:]:
                    score = similarity(left["source_text"], right["source_text"])
                    # Very similar wording is still not silently discarded. It is
                    # marked for semantic review because numbers/cities may matter.
                    if score >= 0.58:
                        con.execute(
                            """UPDATE entries SET needs_duplicate_review=1,
                               dedupe_reason='same author: similar text',
                               dedupe_confidence=?, dedupe_version=?
                               WHERE message_id IN (?,?) AND excluded_reason IS NULL""",
                            (round(score, 3), VERSION, left["id"], right["id"]),
                        )
                        review_pairs += 1
        review_entries = con.execute(
            "SELECT COUNT(*) AS count FROM entries WHERE period_key=? AND needs_duplicate_review=1",
            (period,),
        ).fetchone()["count"]
    return {"messages": len(rows), "auto_duplicates": auto_duplicates,
            "review_pairs": review_pairs, "review_entries": review_entries}
