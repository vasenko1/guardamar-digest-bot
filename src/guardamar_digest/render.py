from __future__ import annotations

import html
from collections import defaultdict
from .db import connect


LIMIT = 3600

def telegram_length(text: str) -> int:
    # Telegram counts supplementary Unicode characters (most emoji) as two
    # UTF-16 code units. Counting this way is safer than Python's len().
    return len(text.encode("utf-16-le")) // 2

def render(settings, period: str) -> list[str]:
    with connect(settings.db_path) as con:
        run=con.execute("SELECT run_id,status FROM classification_runs WHERE period_key=?",(period,)).fetchone()
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
        rows = con.execute("""SELECT e.*,m.source_url FROM entries e JOIN messages m ON m.id=e.message_id
          WHERE e.period_key=? AND e.eligible=1 AND e.excluded_reason IS NULL ORDER BY e.category_title,e.short_title""", (period,)).fetchall()
    groups = defaultdict(list)
    for r in rows:
        title = r["manual_title"] or r["short_title"] or "Объявление"
        cat = (r["category_emoji"] or "📦", r["manual_category"] or r["category_title"] or "Другое")
        groups[cat].append(f'• {html.escape(title)}\u202f<a href="{html.escape(r["source_url"], quote=True)}">↗</a>')
    blocks=[]
    for (emoji, title), entries in groups.items():
        heading=f"{emoji} <b>{html.escape(title)}</b>\n"
        chunk=[]
        for entry in entries:
            candidate="\n".join(chunk+[entry])
            if chunk and telegram_length(heading + candidate)>LIMIT:
                blocks.append(heading+"\n".join(chunk)); chunk=[entry]
            else: chunk.append(entry)
        if chunk: blocks.append(heading+"\n".join(chunk))
    month = period
    parts=[]; current=""
    for block in blocks:
        if current and telegram_length(current + "\n\n" + block) > LIMIT:
            parts.append(current); current=block
        else: current = (current+"\n\n" if current else "")+block
    if current: parts.append(current)
    total=len(parts) or 1
    return [f"📚 <b>обЪявления Гуардамар</b>\nГлавное за {month} · Часть {i} из {total}\n\n{body}" + ("\n\nАктуальность уточняйте у автора.\n\n<b>Новые объявления каждый день → обЪявления Гуардамар</b>" if i==total else "") for i,body in enumerate(parts,1)]
