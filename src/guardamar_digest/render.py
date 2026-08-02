from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from .db import connect
from .llm import _required_location


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
SELL = re.compile(r"\b(?:продам|прода[её]тся|продаж[аи]|продаю)\b", re.I)
GIVE_AWAY = re.compile(r"\b(?:отдам|віддам|даром)\b", re.I)
STRONG_SEEK = re.compile(
    r"\b(?:ищу|ищем|куплю|шукаю|кто\s+занимается|кто\s+прода[её]т|"
    r"хтось\s+прода[єе]|подскажите)\b",
    re.I,
)
LEADING_NEED = re.compile(
    r"^\s*(?:добрый\s+(?:день|вечер)[!,.]?\s*)?"
    r"(?:требу(?:ется|ются)|нуж(?:ен|на|ны)|потрібн\w*)\b",
    re.I,
)
RENT = re.compile(
    r"\b(?:аренд\w*|оренд\w*|сдам|здам|сда[её]м|зда[єе]мо|"
    r"сда[её]тся|зда[єе]ться|снять|сниму)\b",
    re.I,
)
JOB_SEEK = re.compile(
    r"\bищ(?:у|ем|ет|ут)\s+работ\w*|\bшука\w*\s+робот\w*|"
    r"\bготов\w*\s+приступить\b",
    re.I,
)
JOB_OFFER = re.compile(
    r"\b(?:ваканси\w*|требу(?:ется|ются)|потріб\w*\s+(?:працівник|"
    r"робітник|майстер)|ищем\s+(?:сотрудник|работник|мастер)|набираем)\b",
    re.I,
)
TRIP_TRANSFER = re.compile(r"\b(?:трансфер\w*|попутчик\w*|поездк\w*|еду|їхати|аэропорт\w*|вокзал\w*)\b", re.I)
VEHICLE = re.compile(
    r"\b(?:автомоб[иі]л\w*|машин\w*|авто\b|audi|bmw|chevrolet|citro[eë]n|"
    r"fiat|ford|honda|hyundai|kia|mazda|mercedes|mitsubishi|nissan|opel|"
    r"peugeot|renault|seat|skoda|tesla|toyota|volkswagen|volvo)\b",
    re.I,
)
MODEL_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
MONTHS_RU = (
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)

def telegram_length(text: str) -> int:
    # Telegram counts supplementary Unicode characters (most emoji) as two
    # UTF-16 code units. Counting this way is safer than Python's len().
    return len(text.encode("utf-16-le")) // 2


def _intent_subsection(category_title: str, source_text: str) -> str | None:
    """Return a stable editorial subsection based on the original ad intent."""
    category = category_title.casefold()
    seeks = bool(STRONG_SEEK.search(source_text) or LEADING_NEED.search(source_text))
    if "работ" in category or "ваканси" in category:
        if JOB_SEEK.search(source_text):
            return "Ищу работу"
        if JOB_OFFER.search(source_text):
            return "Требуется"
        return None
    if "транспорт" in category or "авто" in category:
        if RENT.search(source_text) and VEHICLE.search(source_text):
            return "Сниму в аренду" if seeks else "Сдам в аренду"
        if VEHICLE.search(source_text) and (
            SELL.search(source_text) or MODEL_YEAR.search(source_text)
        ):
            return "Продажа"
        if TRIP_TRANSFER.search(source_text):
            return "Поездки и трансфер"
        return "Автоуслуги"
    if any(word in category for word in ("товар", "вещ", "одежд")):
        if GIVE_AWAY.search(source_text):
            return "Отдам"
        if RENT.search(source_text):
            return "Сниму в аренду" if seeks else "Сдам в аренду"
        if seeks:
            return "Куплю"
        return "Продажа"
    if any(word in category for word in ("услуг", "красот", "здоров")):
        return "Ищу специалиста" if seeks else "Предлагаю услуги"
    return None


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
        try:
            category_plan = json.loads(run["categories_json"])
        except (TypeError, json.JSONDecodeError):
            category_plan = []
        category_order = {
            item.get("code"): index
            for index, item in enumerate(category_plan)
            if isinstance(item, dict) and item.get("code")
        }
        rows = con.execute("""SELECT e.*,m.source_url,m.source_text FROM entries e JOIN messages m ON m.id=e.message_id
          WHERE e.period_key=? AND e.eligible=1 AND e.excluded_reason IS NULL""", (period,)).fetchall()
        rows = sorted(
            rows,
            key=lambda row: (
                category_order.get(row["category_code"], len(category_order)),
                row["category_title"] or "",
                1 if _required_location(row["source_text"]) else 0,
                row["short_title"] or "",
            ),
        )
    groups = defaultdict(list)
    for r in rows:
        title = r["manual_title"] or r["short_title"] or "Объявление"
        cat = (r["category_emoji"] or "📦", r["manual_category"] or r["category_title"] or "Другое")
        line = f'• {html.escape(title)}\u202f<a href="{html.escape(r["source_url"], quote=True)}">↗</a>'
        groups[cat].append((line, r["source_text"]))
    blocks=[]
    realestate_groups = [
        (title, [line for line, _ in entries]) for (_, title), entries in groups.items()
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
        for entry, source_text in entries:
            subsection = _intent_subsection(title, source_text)
            if subsection:
                subsection_groups[subsection].append(entry)
        # A lone intent label adds noise. Show nested groups only when the
        # category contains genuinely different editorial intents.
        if len(subsection_groups) > 1 and sum(map(len, subsection_groups.values())) == len(entries):
            ordered = sorted(
                subsection_groups.items(),
                key=lambda item: (SUBSECTION_ORDER.get(item[0], 99), item[0]),
            )
            _append_nested_blocks(blocks, heading, ordered)
            continue
        chunk=[]
        for entry, _ in entries:
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
