from __future__ import annotations

import json
import random
import re
import time
from http.client import IncompleteRead
from json import JSONDecodeError
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import Settings
from .db import connect

_last_request_at = 0.0
MIN_REQUEST_INTERVAL = 2.0
MAX_RETRIES = 2


def _post(url: str, headers: dict[str, str], body: dict) -> dict:
    """Paced REST call with bounded retries for free-provider transient errors."""
    global _last_request_at
    request = Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    for attempt in range(MAX_RETRIES + 1):
        pause = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if pause > 0:
            time.sleep(pause)
        try:
            _last_request_at = time.monotonic()
            with urlopen(request, timeout=90) as response:  # nosec: API URL is configured internally
                return json.loads(response.read())
        except HTTPError as exc:
            retryable = exc.code in {408, 429} or 500 <= exc.code < 600
            if not retryable or attempt == MAX_RETRIES:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = min(float(retry_after), 60.0) if retry_after else min(2 ** (attempt + 1), 30.0)
            except ValueError:
                delay = min(2 ** (attempt + 1), 30.0)
        except (IncompleteRead, URLError, TimeoutError, OSError):
            if attempt == MAX_RETRIES:
                raise
            delay = min(2 ** (attempt + 1), 30.0)
        time.sleep(delay + random.uniform(0, 0.8))
    raise RuntimeError("unreachable")


def _json(text: object) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("model response has no text content")
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text.strip()
    value, _ = json.JSONDecoder().raw_decode(candidate)
    if not isinstance(value, dict):
        raise ValueError("model response is not a JSON object")
    return value


def prompt(rows: list[dict], categories: list[dict] | None = None) -> str:
    return """Ты редактор ежемесячного Telegram-дайджеста городской группы Гуардамар.
Верни ТОЛЬКО JSON: {\"categories\":[{\"code\":\"...\",\"title\":\"...\",\"emoji\":\"...\"}],\"entries\":[{\"id\":1,\"include\":true,\"category\":\"...\",\"title\":\"...\",\"confidence\":\"high|low\"}]}.
Создай только широкие категории, нужные этому месяцу. Одна строка — одно объявление.
Не повторяй название категории в title. Гуардамар не указывай, но другой город указывай обязательно.
Не включай цену, контакты, URL, рекламные эпитеты. Предпочитай важные факты: формат, дата, маршрут, срок, спальни, район, бренд.
Старайся сделать title до 34 символов, но не удаляй существенный факт ради длины. Не дублируй буквальные title в одной категории.
Исключи ответы, обсуждения, сервисные сообщения и сообщения без самостоятельного объявления.
""" + ("Используй ТОЛЬКО этот план категорий: " + json.dumps(categories, ensure_ascii=False) + "\n" if categories else "") + "Данные:\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def plan_prompt(rows: list[dict]) -> str:
    return "Верни ТОЛЬКО JSON: {\"categories\":[{\"code\":\"...\",\"title\":\"...\",\"emoji\":\"...\"}]}. Создай широкие категории только для объявлений текущего месяца. Данные:\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def classify(settings: Settings, period: str) -> str:
    with connect(settings.db_path) as con:
        records = con.execute(
            "SELECT m.id, m.source_text FROM messages m JOIN entries e ON e.message_id=m.id "
            "WHERE e.period_key=? AND e.manual_title IS NULL AND e.excluded_reason IS NULL "
            "AND e.needs_duplicate_review=0 ORDER BY m.message_id",
            (period,),
        ).fetchall()
    rows = [{"id": r["id"], "text": re.sub(r"https?://\S+|\+?\d[\d ()-]{7,}", "", r["source_text"])[:420]} for r in records]
    if not rows:
        return "nothing to classify"
    fixed_categories = None
    # The category plan is cheap: all texts are shortened and no per-entry
    # output is requested. It prevents the first batch from defining the month.
    plan_rows = [{"id": row["id"], "text": row["text"][:150]} for row in rows]
    for provider in ("gemini", "openrouter"):
      try:
        content=plan_prompt(plan_rows)
        if provider=="gemini" and settings.gemini_key:
          raw=_post(f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent?key={settings.gemini_key}",{"Content-Type":"application/json"},{"contents":[{"parts":[{"text":content}]}],"generationConfig":{"responseMimeType":"application/json","maxOutputTokens":1024}}); result=_json(raw["candidates"][0]["content"]["parts"][0]["text"])
        elif provider=="openrouter" and settings.openrouter_key:
          raw=_post("https://openrouter.ai/api/v1/chat/completions",{"Content-Type":"application/json","Authorization":f"Bearer {settings.openrouter_key}"},{"model":settings.openrouter_model,"messages":[{"role":"user","content":content}],"response_format":{"type":"json_object"},"max_tokens":1024}); result=_json(raw["choices"][0]["message"]["content"])
        else: continue
        fixed_categories=[c for c in result.get("categories",[]) if isinstance(c,dict) and c.get("code") and c.get("title")]
        if fixed_categories: break
      except Exception: continue
    if not fixed_categories: raise RuntimeError("Could not create category plan with free LLM providers")
    provider_used = []
    all_entries = []
    for offset in range(0, len(rows), 1):
      batch = rows[offset:offset + 1]
      errors = []
      for provider in ("gemini", "openrouter"):
        try:
            content = prompt(batch, fixed_categories)
            if provider == "gemini" and settings.gemini_key:
                raw = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent?key={settings.gemini_key}", {"Content-Type":"application/json"}, {"contents":[{"parts":[{"text":content}]}], "generationConfig":{"responseMimeType":"application/json", "maxOutputTokens":2048}})
                result = _json(raw["candidates"][0]["content"]["parts"][0]["text"])
            elif provider == "openrouter" and settings.openrouter_key:
                raw = _post("https://openrouter.ai/api/v1/chat/completions", {"Content-Type":"application/json", "Authorization":f"Bearer {settings.openrouter_key}"}, {"model":settings.openrouter_model,"messages":[{"role":"user","content":content}],"response_format":{"type":"json_object"},"max_tokens":2048})
                result = _json(raw["choices"][0]["message"]["content"])
            else:
                continue
            categories = {c["code"]: c for c in result.get("categories", [])}
            returned = result.get("entries", [])
            expected_ids = {row["id"] for row in batch}
            returned_ids = {entry.get("id") for entry in returned if isinstance(entry, dict)}
            if not returned or returned_ids != expected_ids:
                raise ValueError(
                    f"incomplete model response: expected {len(expected_ids)} entries, got {len(returned_ids)}"
                )
            fixed_map={c["code"]:c for c in fixed_categories}
            if any(entry.get("category") not in fixed_map for entry in returned): raise ValueError("model used a category outside the fixed plan")
            all_entries.extend((entry, fixed_map, provider) for entry in returned)
            provider_used.append(provider); break
        except HTTPError as exc:
            errors.append(f"{provider}: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:800]}")
        except (IncompleteRead, JSONDecodeError, KeyError, ValueError, URLError, TimeoutError, OSError) as exc:
            errors.append(f"{provider}: {exc}")
      else: raise RuntimeError("; ".join(errors) or "No LLM API key configured")
    with connect(settings.db_path) as con:
      for entry, categories, provider in all_entries:
        if not isinstance(entry.get("title"), str) or not entry["title"].strip(): raise ValueError(f"missing title for entry {entry.get('id')}")
        category=categories.get(entry.get("category"), {"title":"Другое","emoji":"📦"})
        con.execute("UPDATE entries SET eligible=?,category_code=?,category_title=?,category_emoji=?,short_title=?,confidence=?,provider=? WHERE message_id=? AND period_key=?", (int(bool(entry.get("include"))),entry.get("category"),category.get("title"),category.get("emoji"),entry.get("title"),entry.get("confidence"),provider,entry.get("id"),period))
    return "+".join(sorted(set(provider_used)))
