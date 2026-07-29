from __future__ import annotations

import html
from collections import defaultdict
from .db import connect


LIMIT = 3700

def render(settings, period: str) -> list[str]:
    with connect(settings.db_path) as con:
        rows = con.execute("""SELECT e.*,m.source_url FROM entries e JOIN messages m ON m.id=e.message_id
          WHERE e.period_key=? AND e.eligible=1 AND e.excluded_reason IS NULL ORDER BY e.category_title,e.short_title""", (period,)).fetchall()
    groups = defaultdict(list)
    for r in rows:
        title = r["manual_title"] or r["short_title"] or "Объявление"
        cat = (r["category_emoji"] or "📦", r["manual_category"] or r["category_title"] or "Другое")
        groups[cat].append(f'• {html.escape(title)}\u202f<a href="{html.escape(r["source_url"], quote=True)}">↗</a>')
    blocks=[]
    for (emoji, title), entries in groups.items():
        blocks.append(f"{emoji} <b>{html.escape(title)}</b>\n" + "\n".join(entries))
    month = period
    parts=[]; current=""
    for block in blocks:
        if current and len(current)+len(block)+2 > LIMIT:
            parts.append(current); current=block
        else: current = (current+"\n\n" if current else "")+block
    if current: parts.append(current)
    total=len(parts) or 1
    return [f"📚 <b>обЪявления Гуардамар</b>\nГлавное за {month} · Часть {i} из {total}\n\n{body}" + ("\n\nАктуальность уточняйте у автора.\n\n<b>Новые объявления каждый день → обЪявления Гуардамар</b>" if i==total else "") for i,body in enumerate(parts,1)]
