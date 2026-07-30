from __future__ import annotations

import html
import json
from collections import defaultdict
from .db import connect


LIMIT = 3600
MONTHS_RU = (
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)

def telegram_length(text: str) -> int:
    # Telegram counts supplementary Unicode characters (most emoji) as two
    # UTF-16 code units. Counting this way is safer than Python's len().
    return len(text.encode("utf-16-le")) // 2

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
        rows = con.execute("""SELECT e.*,m.source_url FROM entries e JOIN messages m ON m.id=e.message_id
          WHERE e.period_key=? AND e.eligible=1 AND e.excluded_reason IS NULL""", (period,)).fetchall()
        rows = sorted(
            rows,
            key=lambda row: (
                category_order.get(row["category_code"], len(category_order)),
                row["category_title"] or "",
                row["short_title"] or "",
            ),
        )
    groups = defaultdict(list)
    for r in rows:
        title = r["manual_title"] or r["short_title"] or "Объявление"
        cat = (r["category_emoji"] or "📦", r["manual_category"] or r["category_title"] or "Другое")
        groups[cat].append(f'• {html.escape(title)}\u202f<a href="{html.escape(r["source_url"], quote=True)}">↗</a>')
    blocks=[]
    for (emoji, title), entries in groups.items():
        heading=f"{html.escape(emoji)} <b>{html.escape(title)}</b>\n"
        chunk=[]
        for entry in entries:
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
