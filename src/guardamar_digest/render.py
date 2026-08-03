from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from .db import connect
from .editorial import (
    VERSION as EDITORIAL_VERSION,
    INTENT_SUBSECTIONS,
    LOCAL_CITY_IN_TITLE,
    infer_intent,
)
from .llm import OTHER_LOCATION_ALIASES, _required_location


LIMIT = 3600
REAL_ESTATE_SUBSECTIONS = {
    "Сдам в аренду", "Сниму в аренду", "Продам", "Куплю",
}
SUBSECTION_ORDER = {
    "Продажа": 0,
    "Сдам в аренду": 1,
    "Сниму в аренду": 2,
    "Куплю": 3,
    "Отдам": 4,
    "Поездки и трансфер": 5,
    "Автоуслуги": 6,
    "Требуется": 0,
    "Ищу работу": 1,
    "Предлагаю услуги": 0,
    "Ищу специалиста": 1,
}
MONTHS_RU = (
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)

def telegram_length(text: str) -> int:
    # Telegram counts supplementary Unicode characters (most emoji) as two
    # UTF-16 code units. Counting this way is safer than Python's len().
    return len(text.encode("utf-16-le")) // 2


def _has_outside_city(title: str, source_text: str) -> bool:
    """Rank another city after Guardamar and location-neutral listings."""
    if _required_location(source_text):
        return True
    normalized_title = title.casefold()
    return any(
        alias in normalized_title
        for _, aliases in OTHER_LOCATION_ALIASES
        for alias in aliases
    )


def _display_title(title: str) -> str:
    """Remove the digest's own city from a line while preserving other cities."""
    compact = LOCAL_CITY_IN_TITLE.sub("", title)
    compact = re.sub(r"\s+", " ", compact)
    compact = re.sub(r"\s+([,;:])", r"\1", compact)
    return compact.strip(" ,;:–—-") or title


def _intent_subsection(category_title: str, source_text: str) -> str | None:
    """Return a stable editorial subsection based on the original ad intent."""
    return INTENT_SUBSECTIONS.get(infer_intent(category_title, source_text))


def _append_nested_blocks(
    blocks: list[str], heading: str, subsection_entries: list[tuple[str, list[str]]]
) -> None:
    current = heading
    for subsection, entries in subsection_entries:
        subheading = f"<b>{html.escape(subsection)}</b>\n"
        first_entry = True
        for entry in entries:
            prefix = ""
            if current != heading:
                prefix += "\n"
            if first_entry:
                prefix += subheading
            candidate = current + prefix + entry
            if current != heading and telegram_length(candidate) > LIMIT:
                blocks.append(current.rstrip())
                current = heading + subheading + entry
            else:
                current = candidate
            first_entry = False
    if current != heading:
        blocks.append(current.rstrip())

def render(settings, period: str) -> list[str]:
    with connect(settings.db_path) as con:
        run=con.execute(
            """SELECT run_id,status,categories_json FROM classification_runs
               WHERE period_key=?""",
            (period,),
        ).fetchone()
        if not run or run["status"]!="complete":
            raise RuntimeError("Classification is not complete; preview and publish are blocked")
        missing=con.execute(
            """SELECT COUNT(*) FROM entries
               WHERE period_key=? AND eligible=1 AND excluded_reason IS NULL
                 AND manual_title IS NULL
                 AND (classification_run_id IS NULL OR classification_run_id<>?)""",
            (period,run["run_id"]),
        ).fetchone()[0]
        if missing: raise RuntimeError("Classification is incomplete; preview and publish are blocked")
        editorial_run = con.execute(
            """SELECT status,rule_version FROM workflow_runs
               WHERE period_key=? AND stage='editorial_normalization'""",
            (period,),
        ).fetchone()
        if (
            not editorial_run or editorial_run["status"] != "complete"
            or editorial_run["rule_version"] != EDITORIAL_VERSION
        ):
            raise RuntimeError(
                "Editorial normalization is not complete; preview and publish are blocked"
            )
        try:
            category_plan = json.loads(run["categories_json"])
        except (TypeError, json.JSONDecodeError):
            category_plan = []
        category_order = {
            item.get("code"): index
            for index, item in enumerate(category_plan)
            if isinstance(item, dict) and item.get("code")
        }
        rows = con.execute("""SELECT e.*,m.source_url,m.source_text,m.chat_id,
          m.message_id AS external_message_id FROM entries e JOIN messages m ON m.id=e.message_id
          WHERE e.period_key=? AND e.eligible=1 AND e.excluded_reason IS NULL""", (period,)).fetchall()
        rows = sorted(
            rows,
            key=lambda row: (
                category_order.get(row["category_code"], len(category_order)),
                row["category_title"] or "",
                1 if (row["location_scope"] in {"outside", "mixed"} or (
                    not row["location_scope"] and _has_outside_city(
                        row["manual_title"] or row["short_title"] or "",
                        row["source_text"],
                    )
                )) else 0,
                row["manual_title"] or row["short_title"] or "",
                row["chat_id"],
                row["external_message_id"],
            ),
        )
    groups = defaultdict(list)
    for r in rows:
        title = _display_title(
            r["manual_title"] or r["short_title"] or "Объявление"
        )
        cat = (r["category_emoji"] or "📦", r["manual_category"] or r["category_title"] or "Другое")
        line = f'• {html.escape(title)}\u202f<a href="{html.escape(r["source_url"], quote=True)}">↗</a>'
        groups[cat].append((line, r["source_text"], r["intent_code"]))
    blocks=[]
    realestate_groups = [
        (title, [line for line, _, _ in entries]) for (_, title), entries in groups.items()
        if title in REAL_ESTATE_SUBSECTIONS
    ]
    realestate_emitted = False
    for (emoji, title), entries in groups.items():
        if title in REAL_ESTATE_SUBSECTIONS:
            if realestate_emitted:
                continue
            realestate_emitted = True
            _append_nested_blocks(
                blocks, "🏠 <b>Недвижимость</b>\n", realestate_groups
            )
            continue
        heading=f"{html.escape(emoji)} <b>{html.escape(title)}</b>\n"
        subsection_groups = defaultdict(list)
        for entry, source_text, intent_code in entries:
            subsection = INTENT_SUBSECTIONS.get(intent_code) or _intent_subsection(title, source_text)
            if subsection:
                subsection_groups[subsection].append(entry)
        # Compact titles intentionally omit verbs such as "ищу" and "продам".
        # Therefore even a single transactional intent needs a visible label;
        # otherwise the direction of the listing is lost.
        if subsection_groups and sum(map(len, subsection_groups.values())) == len(entries):
            ordered = sorted(
                subsection_groups.items(),
                key=lambda item: (SUBSECTION_ORDER.get(item[0], 99), item[0]),
            )
            _append_nested_blocks(blocks, heading, ordered)
            continue
        chunk=[]
        for entry, _, _ in entries:
            candidate="\n".join(chunk+[entry])
            if chunk and telegram_length(heading + candidate)>LIMIT:
                blocks.append(heading+"\n".join(chunk)); chunk=[entry]
            else: chunk.append(entry)
        if chunk: blocks.append(heading+"\n".join(chunk))
    year, month_number = map(int, period.split("-"))
    month = f"{MONTHS_RU[month_number]} {year}"
    parts=[]; current=""
    for block in blocks:
        if current and telegram_length(current + "\n\n" + block) > LIMIT:
            parts.append(current); current=block
        else: current = (current+"\n\n" if current else "")+block
    if current: parts.append(current)
    total=len(parts) or 1
    group_url = f"https://t.me/{html.escape(settings.source_username, quote=True)}"
    footer = (
        "\n\nАктуальность уточняйте у автора.\n\n"
        f'<b>Новые объявления каждый день → '
        f'<a href="{group_url}">обЪявления Гуардамар</a></b>'
    )
    return [
        f"📚 <b>обЪявления Гуардамар</b>\n"
        f"Главное за {month} · Часть {i} из {total}\n\n{body}"
        + (footer if i == total else "")
        for i, body in enumerate(parts, 1)
    ]
