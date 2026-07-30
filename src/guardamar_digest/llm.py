from __future__ import annotations

import json
import re
from http.client import IncompleteRead
from json import JSONDecodeError
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import Settings
from .db import connect


def _post(url: str, headers: dict[str, str], body: dict) -> dict:
    request = Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urlopen(request, timeout=90) as response:  # nosec: API URL is configured internally
        return json.loads(response.read())


def _json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    return json.loads(fenced.group(1) if fenced else text)


def prompt(rows: list[dict]) -> str:
    return """Ты редактор ежемесячного Telegram-дайджеста городской группы Гуардамар.
Верни ТОЛЬКО JSON: {\"categories\":[{\"code\":\"...\",\"title\":\"...\",\"emoji\":\"...\"}],\"entries\":[{\"id\":1,\"include\":true,\"category\":\"...\",\"title\":\"...\",\"confidence\":\"high|low\"}]}.
Создай только широкие категории, нужные этому месяцу. Одна строка — одно объявление.
Не повторяй название категории в title. Гуардамар не указывай, но другой город указывай обязательно.
Не включай цену, контакты, URL, рекламные эпитеты. Предпочитай важные факты: формат, дата, маршрут, срок, спальни, район, бренд.
Старайся сделать title до 34 символов, но не удаляй существенный факт ради длины. Не дублируй буквальные title в одной категории.
Исключи ответы, обсуждения, сервисные сообщения и сообщения без самостоятельного объявления.
Данные:\n""" + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def classify(settings: Settings, period: str) -> str:
    with connect(settings.db_path) as con:
        records = con.execute(
            "SELECT m.id, m.source_text FROM messages m JOIN entries e ON e.message_id=m.id "
            "WHERE e.period_key=? AND e.manual_title IS NULL ORDER BY m.message_id",
            (period,),
        ).fetchall()
    rows = [{"id": r["id"], "text": re.sub(r"https?://\S+|\+?\d[\d ()-]{7,}", "", r["source_text"])[:420]} for r in records]
    if not rows:
        return "nothing to classify"
    content = prompt(rows)
    errors = []
    for provider in ("gemini", "openrouter"):
        try:
            if provider == "gemini" and settings.gemini_key:
                raw = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent?key={settings.gemini_key}", {"Content-Type":"application/json"}, {"contents":[{"parts":[{"text":content}]}], "generationConfig":{"responseMimeType":"application/json"}})
                result = _json(raw["candidates"][0]["content"]["parts"][0]["text"])
            elif provider == "openrouter" and settings.openrouter_key:
                raw = _post("https://openrouter.ai/api/v1/chat/completions", {"Content-Type":"application/json", "Authorization":f"Bearer {settings.openrouter_key}"}, {"model":settings.openrouter_model,"messages":[{"role":"user","content":content}],"response_format":{"type":"json_object"}})
                result = _json(raw["choices"][0]["message"]["content"])
            else:
                continue
            categories = {c["code"]: c for c in result.get("categories", [])}
            returned = result.get("entries", [])
            expected_ids = {row["id"] for row in rows}
            returned_ids = {entry.get("id") for entry in returned if isinstance(entry, dict)}
            if not returned or returned_ids != expected_ids:
                raise ValueError(
                    f"incomplete model response: expected {len(expected_ids)} entries, got {len(returned_ids)}"
                )
            with connect(settings.db_path) as con:
                for entry in returned:
                    if not isinstance(entry.get("title"), str) or not entry["title"].strip():
                        raise ValueError(f"missing title for entry {entry.get('id')}")
                    category = categories.get(entry.get("category"), {"title":"Другое","emoji":"📦"})
                    con.execute(
                        "UPDATE entries SET eligible=?,category_code=?,category_title=?,category_emoji=?,short_title=?,confidence=?,provider=? "
                        "WHERE message_id=? AND period_key=?",
                        (int(bool(entry.get("include"))), entry.get("category"), category.get("title"), category.get("emoji"), entry.get("title"), entry.get("confidence"), provider, entry.get("id"), period),
                    )
            return provider
        except (HTTPError, IncompleteRead, JSONDecodeError, KeyError, ValueError, URLError, TimeoutError, OSError) as exc:
            errors.append(f"{provider}: {exc}")
    raise RuntimeError("; ".join(errors) or "No LLM API key configured")
