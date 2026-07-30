from __future__ import annotations

import re
import hashlib
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
import json
from urllib.error import HTTPError, URLError

from .db import connect
from .llm import _json, _post


VERSION = "2026-07-30.1"
_blocked_providers: set[str] = set()


class ProviderTemporarilyBlocked(ValueError):
    pass
URL_OR_CONTACT = re.compile(r"https?://\S+|(?:@\w+)|\+?\d[\d ()-]{7,}")
WORDS = re.compile(r"[\wа-яёáéíóúüñ]{3,}", re.I)
STOP_WORDS = {
    "это", "как", "для", "или", "что", "при", "без", "все", "the", "and",
    "una", "por", "con", "del", "las", "los", "есть", "будет", "можно",
}

CONFIDENCE_MAP = {
    "high": "high", "high confidence": "high", "высокая": "high", "высокий": "high", "высоко": "high",
    "medium": "medium", "medium confidence": "medium", "средняя": "medium", "средний": "medium", "средне": "medium",
    "low": "low", "low confidence": "low", "низкая": "low", "низкий": "low", "низко": "low",
}


def normalize_confidence(value: object) -> str:
    """Free models sometimes localize enum values despite the JSON instruction."""
    if not isinstance(value, str):
        return "low"
    return CONFIDENCE_MAP.get(value.strip().casefold(), "low")


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = URL_OR_CONTACT.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalized(text).encode("utf-8")).hexdigest()


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
        # Model conclusions are reproducible and may be refreshed; explicit
        # editor decisions are durable editorial data and must survive re-runs.
        con.execute("DELETE FROM duplicate_reviews WHERE period_key=? AND provider IS NOT 'manual'", (period,))
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
                            """INSERT OR IGNORE INTO duplicate_reviews
                               (period_key,left_message_id,right_message_id,lexical_score)
                               VALUES (?,?,?,?)""",
                            (period, left["id"], right["id"], round(score, 3)),
                        )
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
        _rebuild_semantic_duplicates(con, period)
        _refresh_review_flags(con, period)
    return {"messages": len(rows), "auto_duplicates": auto_duplicates,
            "review_pairs": review_pairs, "review_entries": review_entries}


def _review_pairs(db_path, period: str) -> list[tuple[object, object]]:
    """Load only persistent candidate pairs generated by the deterministic stage."""
    with connect(db_path) as con:
        rows = con.execute(
            """SELECT l.id AS left_id, l.message_id AS left_external_id, l.published_at AS left_published_at,
                      l.source_text AS left_text, r.id AS right_id, r.message_id AS right_external_id,
                      r.published_at AS right_published_at, r.source_text AS right_text
               FROM duplicate_reviews d
               JOIN messages l ON l.id=d.left_message_id JOIN messages r ON r.id=d.right_message_id
               WHERE d.period_key=? AND d.status='pending' ORDER BY l.id, r.id""",
            (period,),
        ).fetchall()
    return [
        ({"id": row["left_id"], "message_id": row["left_external_id"], "published_at": row["left_published_at"], "source_text": row["left_text"]},
         {"id": row["right_id"], "message_id": row["right_external_id"], "published_at": row["right_published_at"], "source_text": row["right_text"]})
        for row in rows
    ]


def _review_prompt(pairs: list[tuple[object, object]]) -> str:
    data = [
        {"left": left["id"], "right": right["id"],
         "a": URL_OR_CONTACT.sub(" ", left["source_text"])[:700],
         "b": URL_OR_CONTACT.sub(" ", right["source_text"])[:700]}
        for left, right in pairs
    ]
    return """Ты проверяешь только возможные дубли объявлений Telegram.
Каждая пара принадлежит одному автору. `same_offer=true` только если это одно
и то же предложение (повтор, обновление или перефразированный репост), а не
два похожих, но разных товара/услуги/объекта. Разные город, даты, маршрут,
бренд, модель, комнаты или предмет обычно означают `false`.
Верни ТОЛЬКО JSON: {\"decisions\":[{\"left\":1,\"right\":2,
\"same_offer\":true,\"confidence\":\"high|medium|low\"}]}.
Не добавляй и не пропускай пары. Текст уже очищен от контактов и ссылок.
Пары:\n""" + json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _ask_provider(settings, content: str, provider: str, max_tokens: int) -> dict:
    if provider in _blocked_providers:
        raise ProviderTemporarilyBlocked(f"{provider} temporarily rate-limited")
    if provider == "gemini" and settings.gemini_key:
        raw = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent?key={settings.gemini_key}",
            {"Content-Type": "application/json"},
            {"contents": [{"parts": [{"text": content}]}],
             "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": max_tokens}},
        )
        return _json(raw["candidates"][0]["content"]["parts"][0]["text"])
    if provider == "openrouter" and settings.openrouter_key:
        raw = _post(
            "https://openrouter.ai/api/v1/chat/completions",
            {"Content-Type": "application/json", "Authorization": f"Bearer {settings.openrouter_key}"},
            {"model": settings.openrouter_model, "messages": [{"role": "user", "content": content}],
             "response_format": {"type": "json_object"}, "max_tokens": max_tokens},
        )
        return _json(raw["choices"][0]["message"]["content"])
    raise ValueError(f"{provider} API key is not configured")


def _topic_prompt(rows: list[object]) -> str:
    data = [{"id": row["id"], "text": URL_OR_CONTACT.sub(" ", row["source_text"])[:420]} for row in rows]
    return """Ты определяешь, какие объявления ОДНОГО автора за месяц рекламируют
одно и то же предложение. Верни ТОЛЬКО JSON:
{\"items\":[{\"id\":1,\"intent\":\"offer|search|event|other\",
\"offer_key\":\"короткий_ключ\",\"confidence\":\"high|medium|low\"}]}.

Одинаковый `offer_key` ставь только одному продолжающемуся рекламному потоку:
повторы услуги, одного поиска, мероприятия или меню одного продавца. Разные
квартиры, машины, маршруты, даты/события, услуги, товары и запросы получают
разные `offer_key`, даже если широкая тема одинакова. «Предлагаю» и «ищу» —
разные intent. Если сомневаешься, сделай ключ уникальным. Не добавляй и не
    пропускай id. Тексты очищены от контактов и ссылок. Данные:\n""" + json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def discover_topics(settings, period: str) -> tuple[int, int]:
    """Find same-offer candidates even when their wording has little overlap."""
    with connect(settings.db_path) as con:
        rows = con.execute(
            """SELECT m.id, m.sender_id, m.published_at, m.message_id, m.source_text
               FROM messages m JOIN entries e ON e.message_id=m.id
               WHERE e.period_key=? AND e.excluded_reason IS NULL AND m.sender_id IS NOT NULL
                 AND m.sender_id<>'' ORDER BY m.sender_id, m.published_at, m.message_id""",
            (period,),
        ).fetchall()
        cached = {topic["message_id"]: topic for topic in con.execute(
            "SELECT message_id,intent,offer_key,confidence,provider,text_fingerprint FROM dedupe_topics WHERE period_key=?", (period,)
        )}
    by_author: dict[str, list] = defaultdict(list)
    for row in rows:
        by_author[row["sender_id"]].append(row)
    annotations: list[tuple[object, str, str, str, str]] = []
    providers: list[str] = []
    # Keeping an author's messages together lets the model distinguish two
    # listings from repeated advertising. Most authors have only a few posts.
    for author_rows in by_author.values():
        if len(author_rows) == 1:
            continue
        if all(cached.get(row["id"]) and cached[row["id"]]["text_fingerprint"] == fingerprint(row["source_text"]) for row in author_rows):
            annotations.extend((row, cached[row["id"]]["intent"], cached[row["id"]]["offer_key"], cached[row["id"]]["confidence"], cached[row["id"]]["provider"]) for row in author_rows)
            continue
        if len(author_rows) > 10:
            # Large-volume publishers are riskier. Overlapping windows preserve
            # local context; their cross-window matches stay for editor review.
            windows = [author_rows[index:index + 10] for index in range(0, len(author_rows), 10)]
        else:
            windows = [author_rows]
        for window in windows:
            expected = {row["id"] for row in window}
            errors: list[str] = []
            for provider in ("gemini", "openrouter"):
                try:
                    result = _ask_provider(settings, _topic_prompt(window), provider, 1024)
                    items = result.get("items", [])
                    returned = {item.get("id") for item in items if isinstance(item, dict)}
                    if returned != expected:
                        raise ValueError(f"incomplete topic response: expected {len(expected)} items, got {len(returned)}")
                    for item in items:
                        if item.get("intent") not in {"offer", "search", "event", "other"}:
                            raise ValueError("topic response has invalid intent")
                        if not isinstance(item.get("offer_key"), str) or not item["offer_key"].strip():
                            raise ValueError("topic response has empty offer_key")
                        item["confidence"] = normalize_confidence(item.get("confidence"))
                    by_id = {row["id"]: row for row in window}
                    annotations.extend((by_id[item["id"]], item["intent"], item["offer_key"].strip().casefold(), item["confidence"], provider) for item in items)
                    providers.append(provider)
                    break
                except HTTPError as exc:
                    if exc.code == 429:
                        _blocked_providers.add(provider)
                    errors.append(f"{provider}: HTTP {exc.code}")
                except (URLError, TimeoutError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"{provider}: {exc}")
            else:
                raise RuntimeError("; ".join(errors))
    # Overlapping windows for a very prolific author may annotate a boundary
    # message twice. Keep the last complete annotation, never make a self-pair.
    annotation_by_id = {item[0]["id"]: item for item in annotations}
    annotations = list(annotation_by_id.values())
    with connect(settings.db_path) as con:
        for row, intent, offer_key, confidence, provider in annotations:
            con.execute(
                """INSERT OR REPLACE INTO dedupe_topics
                   (period_key,message_id,intent,offer_key,confidence,provider,text_fingerprint) VALUES (?,?,?,?,?,?,?)""",
                (period, row["id"], intent, offer_key, confidence, provider, fingerprint(row["source_text"])),
            )
        grouped: dict[tuple[str, str, str], list[object]] = defaultdict(list)
        for row, intent, offer_key, confidence, provider in annotations:
            if confidence != "low":
                grouped[(row["sender_id"], intent, offer_key)].append(row)
        created = 0
        for group_rows in grouped.values():
            group_rows.sort(key=lambda row: (row["published_at"], row["message_id"]))
            # Adjacent links form a transitive campaign and avoid quadratic cost.
            for left, right in zip(group_rows, group_rows[1:]):
                result = con.execute(
                    """INSERT OR IGNORE INTO duplicate_reviews
                       (period_key,left_message_id,right_message_id,lexical_score) VALUES (?,?,?,0)""",
                    (period, min(left["id"], right["id"]), max(left["id"], right["id"])),
                )
                created += result.rowcount
    return len(annotations), created


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, value: int) -> int:
        self.parent.setdefault(value, value)
        if self.parent[value] != value:
            self.parent[value] = self.find(self.parent[value])
        return self.parent[value]

    def join(self, left: int, right: int) -> None:
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[right] = left


def semantic_dedupe(settings, period: str) -> dict[str, int | str]:
    """Use tiny, all-or-nothing LLM batches to resolve semantic duplicate pairs."""
    _blocked_providers.clear()
    topic_items, thematic_pairs = discover_topics(settings, period)
    pairs = _review_pairs(settings.db_path, period)
    if not pairs:
        return {"topic_items": topic_items, "thematic_pairs": thematic_pairs, "candidate_pairs": 0,
                "semantic_duplicates": 0, "unresolved_pairs": 0, "provider": "none"}
    decisions: list[dict] = []
    provider_used = ""
    # Eight pairs fit comfortably in the free-model response budget and make a
    # truncated answer harmless: nothing is written until every batch validates.
    for offset in range(0, len(pairs), 8):
        batch = pairs[offset:offset + 8]
        expected = {(left["id"], right["id"]) for left, right in batch}
        errors: list[str] = []
        for provider in ("gemini", "openrouter"):
            try:
                result = _ask_provider(settings, _review_prompt(batch), provider, 768)
                returned = result.get("decisions", [])
                keys = {(item.get("left"), item.get("right")) for item in returned if isinstance(item, dict)}
                if keys != expected:
                    raise ValueError(f"incomplete semantic response: expected {len(expected)} pairs, got {len(keys)}")
                for item in returned:
                    item["confidence"] = normalize_confidence(item.get("confidence"))
                decisions.extend(returned)
                provider_used = provider if not provider_used else provider_used
                break
            except Exception as exc:  # individual provider errors are reported only if all fallbacks fail
                errors.append(f"{provider}: {exc}")
        else:
            raise RuntimeError("; ".join(errors))

    by_id = {row["id"]: row for pair in pairs for row in pair}
    groups = _UnionFind()
    unresolved_ids: set[int] = set()
    unresolved_pairs = 0
    decisions_by_pair = {(item["left"], item["right"]): item for item in decisions}
    for decision in decisions:
        left, right = decision["left"], decision["right"]
        if decision.get("same_offer") and decision["confidence"] == "high":
            groups.join(left, right)
        else:
            if decision.get("confidence") != "high":
                unresolved_pairs += 1
                unresolved_ids.update((left, right))

    clusters: dict[int, list[int]] = defaultdict(list)
    for value in groups.parent:
        clusters[groups.find(value)].append(value)
    semantic_duplicates = 0
    with connect(settings.db_path) as con:
        for (left, right), decision in decisions_by_pair.items():
            status = "same" if decision.get("same_offer") and decision["confidence"] == "high" else (
                "different" if decision["confidence"] == "high" else "pending"
            )
            con.execute(
                """UPDATE duplicate_reviews SET status=?, confidence=?, provider=?
                   WHERE period_key=? AND left_message_id=? AND right_message_id=?""",
                (status, decision["confidence"], provider_used, period, left, right),
            )
        # A high-confidence "different" decision clears the preliminary review
        # flag; low/medium decisions remain visible for a human editor.
        candidate_ids = {row["id"] for pair in pairs for row in pair}
        for message_id in candidate_ids:
            con.execute(
                "UPDATE entries SET needs_duplicate_review=? WHERE message_id=? AND excluded_reason IS NULL",
                (int(message_id in unresolved_ids), message_id),
            )
        for member_ids in clusters.values():
            if len(member_ids) < 2:
                continue
            keeper = _canonical([by_id[value] for value in member_ids])
            for message_id in member_ids:
                if message_id == keeper["id"]:
                    continue
                con.execute(
                    """UPDATE entries SET eligible=0, excluded_reason='duplicate', duplicate_of=?,
                       dedupe_reason='semantic same author', dedupe_confidence=0.95,
                       dedupe_version=?, needs_duplicate_review=0 WHERE message_id=?""",
                    (keeper["id"], VERSION, message_id),
                )
                semantic_duplicates += 1
    return {"topic_items": topic_items, "thematic_pairs": thematic_pairs, "candidate_pairs": len(pairs), "semantic_duplicates": semantic_duplicates,
            "unresolved_pairs": unresolved_pairs, "provider": provider_used}


def review_report(db_path, period: str) -> str:
    """Human-readable, local-only list of pairs that no model was sure about."""
    with connect(db_path) as con:
        rows = con.execute(
            """SELECT l.message_id AS left_id, l.source_url AS left_url, l.source_text AS left_text,
                      r.message_id AS right_id, r.source_url AS right_url, r.source_text AS right_text,
                      d.lexical_score, d.confidence
               FROM duplicate_reviews d JOIN messages l ON l.id=d.left_message_id
               JOIN messages r ON r.id=d.right_message_id
               JOIN entries le ON le.message_id=l.id JOIN entries re ON re.message_id=r.id
               WHERE d.period_key=? AND d.status='pending'
                 AND le.excluded_reason IS NULL AND re.excluded_reason IS NULL
               ORDER BY l.message_id, r.message_id""",
            (period,),
        ).fetchall()
    if not rows:
        return "Нет неуверенных пар дублей."
    output = [f"Неуверенные пары дублей: {len(rows)}"]
    for number, row in enumerate(rows, 1):
        left = " ".join(row["left_text"].split())[:220]
        right = " ".join(row["right_text"].split())[:220]
        output.extend((
            f"\n{number}. сообщения {row['left_id']} ↔ {row['right_id']} (сходство {row['lexical_score']}; LLM: {row['confidence'] or 'нет'})",
            f"A: {left}\n   {row['left_url']}",
            f"B: {right}\n   {row['right_url']}",
        ))
    return "\n".join(output)


def _refresh_review_flags(con, period: str) -> None:
    con.execute(
        """UPDATE entries SET needs_duplicate_review=CASE WHEN EXISTS (
              SELECT 1 FROM duplicate_reviews d
              WHERE d.period_key=entries.period_key AND d.status='pending'
                AND (d.left_message_id=entries.message_id OR d.right_message_id=entries.message_id)
            ) THEN 1 ELSE 0 END
            WHERE period_key=? AND excluded_reason IS NULL""",
        (period,),
    )


def decide_pairs(db_path, period: str, pairs: list[tuple[int, int]], same: bool) -> int:
    """Apply an editor's explicit decision using visible Telegram message IDs."""
    applied = 0
    with connect(db_path) as con:
        for left_external, right_external in pairs:
            row = con.execute(
                """SELECT d.left_message_id, d.right_message_id, l.published_at AS left_at,
                          l.message_id AS left_external, r.published_at AS right_at,
                          r.message_id AS right_external
                   FROM duplicate_reviews d JOIN messages l ON l.id=d.left_message_id
                   JOIN messages r ON r.id=d.right_message_id
                   WHERE d.period_key=? AND ((l.message_id=? AND r.message_id=?)
                     OR (l.message_id=? AND r.message_id=?))""",
                (period, left_external, right_external, right_external, left_external),
            ).fetchone()
            if row is None:
                raise ValueError(f"pair {left_external}:{right_external} is not in the review queue")
            con.execute(
                """UPDATE duplicate_reviews SET status=?, confidence='editor', provider='manual'
                   WHERE period_key=? AND left_message_id=? AND right_message_id=?""",
                ("same" if same else "different", period, row["left_message_id"], row["right_message_id"]),
            )
            applied += 1
        _rebuild_semantic_duplicates(con, period)
        _refresh_review_flags(con, period)
    return applied


def _rebuild_semantic_duplicates(con, period: str) -> None:
    """Make transitive duplicate clusters point to their single latest message."""
    con.execute(
        """UPDATE entries SET eligible=1, excluded_reason=NULL, duplicate_of=NULL,
           dedupe_reason=NULL, dedupe_confidence=NULL, dedupe_version=NULL
           WHERE period_key=? AND dedupe_reason LIKE 'semantic same author%'""",
        (period,),
    )
    pairs = con.execute(
        """SELECT d.left_message_id, d.right_message_id FROM duplicate_reviews d
           WHERE d.period_key=? AND d.status='same'""",
        (period,),
    ).fetchall()
    union = _UnionFind()
    involved: set[int] = set()
    for pair in pairs:
        union.join(pair["left_message_id"], pair["right_message_id"])
        involved.update((pair["left_message_id"], pair["right_message_id"]))
    if not involved:
        return
    rows = con.execute(
        """SELECT id, message_id, published_at FROM messages WHERE id IN (%s)"""
        % ",".join("?" for _ in involved),
        tuple(involved),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    clusters: dict[int, list[int]] = defaultdict(list)
    for message_id in involved:
        clusters[union.find(message_id)].append(message_id)
    for member_ids in clusters.values():
        keeper = _canonical([by_id[message_id] for message_id in member_ids])
        for message_id in member_ids:
            if message_id == keeper["id"]:
                continue
            con.execute(
                """UPDATE entries SET eligible=0, excluded_reason='duplicate', duplicate_of=?,
                   dedupe_reason='semantic same author', dedupe_confidence=1.0,
                   dedupe_version=?, needs_duplicate_review=0
                   WHERE message_id=? AND (excluded_reason IS NULL OR dedupe_reason LIKE 'semantic same author%')""",
                (keeper["id"], VERSION, message_id),
            )


def exclude_messages(db_path, period: str, message_ids: list[int], reason: str) -> int:
    """Exclude non-ads or editorially unsuitable messages without deleting raw data."""
    with connect(db_path) as con:
        changed = 0
        for external_id in message_ids:
            result = con.execute(
                """UPDATE entries SET eligible=0, excluded_reason=?, duplicate_of=NULL,
                   dedupe_reason='editor exclusion', dedupe_confidence=NULL,
                   dedupe_version=NULL, needs_duplicate_review=0
                   WHERE period_key=? AND message_id=(SELECT id FROM messages WHERE message_id=?)""",
                (reason, period, external_id),
            )
            if result.rowcount != 1:
                raise ValueError(f"message {external_id} was not imported for {period}")
            changed += 1
        _refresh_review_flags(con, period)
    return changed
