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


VERSION = "2026-07-30.2"
_blocked_providers: set[str] = set()


class ProviderTemporarilyBlocked(ValueError):
    pass
URL_OR_CONTACT = re.compile(r"https?://\S+|(?:@\w+)|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|\+?\d[\d ()-]{7,}")
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
    # Prefer the latest *full* repost. A trailing "возможно торг" or truncated
    # export must never replace a complete advertisement merely because it is
    # newer.
    lengths = [len(normalized(row["source_text"])) if "source_text" in row.keys() else 0 for row in rows]
    longest = max(lengths, default=0)
    full = [
        row for row, length in zip(rows, lengths)
        if not longest or length >= max(20, int(longest * 0.7))
    ]
    return max(full or rows, key=lambda row: (row["published_at"], row["message_id"]))


def dedupe(db_path, period: str) -> dict[str, int]:
    with connect(db_path) as con:
        con.execute(
            """INSERT OR REPLACE INTO workflow_runs
               (period_key,stage,rule_version,status,details,completed_at)
               VALUES (?,'semantic_dedupe',?,'pending',NULL,CURRENT_TIMESTAMP)""",
            (period, VERSION),
        )
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
               WHERE period_key=? AND manual_title IS NULL AND excluded_reason IS NULL""",
            (period,),
        )
        rows = con.execute(
            """SELECT m.id, m.message_id, m.published_at, m.sender_id, m.source_text
               FROM messages m JOIN entries e ON e.message_id=m.id
               WHERE e.period_key=? AND e.excluded_reason IS NULL
               ORDER BY m.sender_id, m.published_at, m.message_id""",
            (period,),
        ).fetchall()
        current = {row["id"]: fingerprint(row["source_text"]) for row in rows}
        for review in con.execute("SELECT left_message_id,right_message_id,left_fingerprint,right_fingerprint FROM duplicate_reviews WHERE period_key=? AND provider IS NOT 'manual'", (period,)):
            if current.get(review["left_message_id"]) != review["left_fingerprint"] or current.get(review["right_message_id"]) != review["right_fingerprint"]:
                con.execute("DELETE FROM duplicate_reviews WHERE period_key=? AND left_message_id=? AND right_message_id=?", (period, review["left_message_id"], review["right_message_id"]))
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
                               (period_key,left_message_id,right_message_id,lexical_score,left_fingerprint,right_fingerprint)
                               VALUES (?,?,?,?,?,?)""",
                            (period, left["id"], right["id"], round(score, 3), fingerprint(left["source_text"]), fingerprint(right["source_text"])),
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
               WHERE d.period_key=? AND d.status IN ('pending','uncertain')
               ORDER BY l.id, r.id""",
            (period,),
        ).fetchall()
    return [
        ({"id": row["left_id"], "message_id": row["left_external_id"], "published_at": row["left_published_at"], "source_text": row["left_text"]},
         {"id": row["right_id"], "message_id": row["right_external_id"], "published_at": row["right_published_at"], "source_text": row["right_text"]})
        for row in rows
    ]


OFFER_WORDS = re.compile(
    r"\b(?:продам|прода[её]тся|сдам|предлага|услуг|аренд[ау]|"
    r"курс|заняти|урок|доставк|ремонт|установк)\w*\b", re.I
)
SEARCH_WORDS = re.compile(
    r"\b(?:ищу|ищем|сниму|куплю|нужен|нужна|нужны|требуется|"
    r"порекомендуйте|посоветуйте)\w*\b", re.I
)
DATE_TOKEN = re.compile(
    r"(?<!\d)(?:[0-3]?\d[./-][01]?\d(?:[./-]20\d{2})?|"
    r"[0-3]?\d\s+(?:января|февраля|марта|апреля|мая|июня|июля|"
    r"августа|сентября|октября|ноября|декабря|січня|лютого|березня|"
    r"квітня|травня|червня|липня|серпня|вересня|жовтня|листопада|грудня))",
    re.I,
)
ROUTE_TOKEN = re.compile(
    r"\b[\wа-яёіїєґáéíóúüñ-]{3,}\s*(?:→|↔|—|-)\s*"
    r"[\wа-яёіїєґáéíóúüñ-]{3,}\b", re.I
)


def _intent(text: str) -> str:
    if SEARCH_WORDS.search(text):
        return "search"
    if OFFER_WORDS.search(text):
        return "offer"
    return "other"


def _deterministic_arbitration(left: object, right: object) -> tuple[str, str, str]:
    """Always produce a conservative-but-complete same/different decision."""
    a, b = left["source_text"], right["source_text"]
    intent_a, intent_b = _intent(a), _intent(b)
    if {intent_a, intent_b} == {"offer", "search"}:
        return "different", "intent_conflict", "high"
    dates_a = {match.casefold() for match in DATE_TOKEN.findall(a)}
    dates_b = {match.casefold() for match in DATE_TOKEN.findall(b)}
    if dates_a and dates_b and dates_a.isdisjoint(dates_b):
        return "different", "explicit_date_conflict", "high"
    routes_a = {match.casefold() for match in ROUTE_TOKEN.findall(a)}
    routes_b = {match.casefold() for match in ROUTE_TOKEN.findall(b)}
    if routes_a and routes_b and routes_a.isdisjoint(routes_b):
        return "different", "explicit_route_conflict", "high"
    score = similarity(a, b)
    common = tokens(a) & tokens(b)
    smaller = min(len(tokens(a)), len(tokens(b))) or 1
    containment = len(common) / smaller
    if score >= 0.72 or containment >= 0.62:
        return "same", "same_author_similar_offer", "medium"
    # The user prefers a clean digest over retaining every borderline repost,
    # but unrelated texts must not be joined merely because the author is same.
    if score >= 0.58 and len(common) >= 3:
        return "same", "same_author_same_topic", "low"
    return "different", "insufficient_offer_overlap", "medium"


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
    for attempt in range(2):
        try:
            if provider == "gemini" and settings.gemini_key:
                raw = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent?key={settings.gemini_key}", {"Content-Type": "application/json"}, {"contents": [{"parts": [{"text": content}]}], "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": max_tokens}})
                return _json(raw["candidates"][0]["content"]["parts"][0].get("text"))
            if provider == "openrouter" and settings.openrouter_key:
                raw = _post("https://openrouter.ai/api/v1/chat/completions", {"Content-Type": "application/json", "Authorization": f"Bearer {settings.openrouter_key}"}, {"model": settings.openrouter_model, "messages": [{"role": "user", "content": content}], "response_format": {"type": "json_object"}, "max_tokens": max_tokens})
                return _json(raw["choices"][0]["message"].get("content"))
            raise ValueError(f"{provider} API key is not configured")
        except (KeyError, TypeError, ValueError):
            if attempt:
                raise
    raise RuntimeError("unreachable")


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
                except (URLError, TimeoutError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
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
                first, second = sorted((left, right), key=lambda row: row["id"])
                result = con.execute(
                    """INSERT OR IGNORE INTO duplicate_reviews
                       (period_key,left_message_id,right_message_id,lexical_score,left_fingerprint,right_fingerprint) VALUES (?,?,?,?,?,?)""",
                    (period, first["id"], second["id"], 0, fingerprint(first["source_text"]), fingerprint(second["source_text"])),
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
    """Resolve every candidate; free-LLM failure falls back to deterministic rules."""
    _blocked_providers.clear()
    topic_error = ""
    try:
        topic_items, thematic_pairs = discover_topics(settings, period)
    except RuntimeError as exc:
        # A weak free fallback may fail the strict topic JSON contract. Existing
        # lexical candidates are still safe and useful; do not discard a whole
        # month merely because this optional discovery layer is unavailable.
        topic_items, thematic_pairs = 0, 0
        topic_error = str(exc)
    pairs = _review_pairs(settings.db_path, period)
    if not pairs:
        with connect(settings.db_path) as con:
            con.execute(
                """INSERT OR REPLACE INTO workflow_runs
                   (period_key,stage,rule_version,status,details,completed_at)
                   VALUES (?,'semantic_dedupe',?,'complete',?,CURRENT_TIMESTAMP)""",
                (period, VERSION, json.dumps({"candidate_pairs": 0}, ensure_ascii=False)),
            )
        return {"topic_items": topic_items, "thematic_pairs": thematic_pairs, "candidate_pairs": 0,
                "same_pairs": 0, "semantic_duplicates": 0, "fallback_pairs": 0,
                "unresolved_pairs": 0, "provider": "none", "topic_warning": topic_error}
    provider_names: set[str] = set()
    fallback_count = 0
    same_count = 0
    for left, right in pairs:
        expected = {(left["id"], right["id"])}
        errors: list[str] = []
        final_status = final_confidence = final_provider = final_reason = ""
        for provider in ("gemini", "openrouter"):
            try:
                result = _ask_provider(settings, _review_prompt([(left, right)]), provider, 768)
                returned = result.get("decisions", [])
                keys = {(item.get("left"), item.get("right")) for item in returned if isinstance(item, dict)}
                if keys != expected:
                    raise ValueError("incomplete semantic response")
                item = returned[0]
                confidence = normalize_confidence(item.get("confidence"))
                # A low-confidence free-model answer is not allowed to become
                # unresolved; it is passed to the deterministic arbiter.
                if confidence == "high":
                    final_status = "same" if item.get("same_offer") else "different"
                    final_confidence, final_provider = confidence, provider
                    final_reason = "llm_same_offer" if final_status == "same" else "llm_distinct_offer"
                    provider_names.add(provider)
                    break
                errors.append(f"{provider}: low-confidence answer")
            except HTTPError as exc:
                if exc.code == 429:
                    _blocked_providers.add(provider)
                errors.append(f"{provider}: HTTP {exc.code}")
            except (URLError, TimeoutError, OSError, KeyError,
                    TypeError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{provider}: {exc}")
        if not final_status:
            final_status, final_reason, final_confidence = _deterministic_arbitration(left, right)
            final_provider = "rule"
            provider_names.add("rule")
            fallback_count += 1
        with connect(settings.db_path) as con:
            con.execute(
                """UPDATE duplicate_reviews SET status=?,confidence=?,provider=?,
                   reason_code=?,reason_detail=?,rule_version=?,decided_at=CURRENT_TIMESTAMP
                   WHERE period_key=? AND left_message_id=? AND right_message_id=?""",
                (final_status, final_confidence, final_provider, final_reason,
                 "; ".join(errors)[:800] or None, VERSION, period,
                 left["id"], right["id"]),
            )
        same_count += int(final_status == "same")
    with connect(settings.db_path) as con:
        # Pair decisions were checkpointed one by one. Rebuilding once keeps the
        # weak-phone path linear enough without sacrificing crash recovery.
        _rebuild_semantic_duplicates(con, period)
        _refresh_review_flags(con, period)
        semantic_duplicates = con.execute(
            """SELECT COUNT(*) FROM entries WHERE period_key=?
               AND dedupe_reason LIKE 'semantic same author%'""", (period,)
        ).fetchone()[0]
        con.execute(
            """INSERT OR REPLACE INTO workflow_runs
               (period_key,stage,rule_version,status,details,completed_at)
               VALUES (?,'semantic_dedupe',?,'complete',?,CURRENT_TIMESTAMP)""",
            (period, VERSION, json.dumps({
                "candidate_pairs": len(pairs), "same_pairs": same_count,
                "fallback_pairs": fallback_count,
            }, ensure_ascii=False),),
        )
    return {"topic_items": topic_items, "thematic_pairs": thematic_pairs,
            "candidate_pairs": len(pairs), "same_pairs": same_count,
            "semantic_duplicates": semantic_duplicates, "fallback_pairs": fallback_count,
            "unresolved_pairs": 0, "provider": "+".join(sorted(provider_names)) or "none",
            "topic_warning": topic_error}


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
               WHERE d.period_key=? AND d.status IN ('pending','uncertain')
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
              WHERE d.period_key=entries.period_key AND d.status IN ('pending','uncertain')
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
    pair_ids = {
        value for pair in pairs
        for value in (pair["left_message_id"], pair["right_message_id"])
    }
    texts = {}
    if pair_ids:
        texts = {
            row["id"]: row
            for row in con.execute(
                """SELECT id,message_id,published_at,source_text FROM messages
                   WHERE id IN (%s)""" % ",".join("?" for _ in pair_ids),
                tuple(pair_ids),
            )
        }
    for pair in pairs:
        # Guard against transitive A~B~C over-merging. Every member of the two
        # prospective clusters must be compatible with every member of the
        # other cluster according to explicit stop features.
        left_root, right_root = union.find(pair["left_message_id"]), union.find(pair["right_message_id"])
        if left_root == right_root:
            involved.update((pair["left_message_id"], pair["right_message_id"]))
            continue
        left_members = [value for value in union.parent if union.find(value) == left_root]
        right_members = [value for value in union.parent if union.find(value) == right_root]
        conflict = any(
            _deterministic_arbitration(texts[left], texts[right])[1]
            in {"intent_conflict", "explicit_date_conflict", "explicit_route_conflict"}
            for left in left_members for right in right_members
        )
        if conflict:
            con.execute(
                """UPDATE duplicate_reviews SET status='different',
                   reason_code='cluster_stop_feature',
                   reason_detail='transitive merge rejected by explicit stop feature',
                   rule_version=?,decided_at=CURRENT_TIMESTAMP
                   WHERE period_key=? AND left_message_id=? AND right_message_id=?""",
                (VERSION, period, pair["left_message_id"], pair["right_message_id"]),
            )
            continue
        union.join(pair["left_message_id"], pair["right_message_id"])
        involved.update((pair["left_message_id"], pair["right_message_id"]))
    if not involved:
        return
    rows = [texts[value] for value in involved]
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


def exclude_senders(db_path, period: str, sender_ids: set[str]) -> int:
    if not sender_ids: return 0
    with connect(db_path) as con:
        marks=",".join("?" for _ in sender_ids)
        result=con.execute(f"UPDATE entries SET eligible=0, excluded_reason='excluded author', needs_duplicate_review=0 WHERE period_key=? AND message_id IN (SELECT id FROM messages WHERE sender_id IN ({marks}))", (period,*sender_ids))
    return result.rowcount
