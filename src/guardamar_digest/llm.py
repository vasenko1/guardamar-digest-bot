from __future__ import annotations

import json
import random
import re
import time
import hashlib
import uuid
from http.client import IncompleteRead
from json import JSONDecodeError
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import Settings
from .db import connect

_last_request_at = 0.0
# Gemini free tier for the configured model allows 15 RPM; 5 seconds leaves
# room for the category-plan call and retry jitter.
MIN_REQUEST_INTERVAL = 5.0
MAX_RETRIES = 2
# Transport retries in _post do not help when a free model returns HTTP 200
# with truncated or otherwise invalid JSON. Retry the semantic request too,
# but keep the bound small so one entry cannot consume the daily quota.
MODEL_RESPONSE_ATTEMPTS = 2
CLASSIFIER_VERSION = "2026-07-31.2"
CATEGORY_CODE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
RUSSIAN_TEXT = re.compile(r"[а-яё]", re.I)
UKRAINIAN_ONLY = re.compile(r"[іїєґ]", re.I)
INCOHERENT_CATEGORY = re.compile(
    r"(?:товар\w*\s+и\s+услуг\w*|работ\w*\s+и\s+обучен\w*)", re.I,
)
PHONE_NUMBER = re.compile(
    r"(?:\+\d(?:[\s()-]*\d){7,14}|"
    r"(?<![\w.])(?:\d[\s()-]*){8,14}\d(?![\w.]))"
)
TITLE_CONTACT = re.compile(
    r"https?://|www\.|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|@\w+|"
    + PHONE_NUMBER.pattern, re.I,
)
TITLE_PRICE = re.compile(
    r"(?:\d[\d\s.,]*\s*(?:€|eur\b|евро\b|₽|\$|грн\b)|(?:€|\$)\s*\d)",
    re.I,
)
TITLE_GENERIC = re.compile(
    r"^(?:объявление|продажа товара(?:\s+за)?|прода[её]тся|"
    r"возможно торг|рекомендация услуг)\W*$", re.I,
)
SANITIZE_CONTACT = re.compile(
    r"https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|@\w+|"
    + PHONE_NUMBER.pattern, re.I,
)
TITLE_MAX_LENGTH = 95
DANGLING_END = re.compile(
    r"\b(?:в|во|на|для|с|со|из|от|до|по|к|у|и|или|а|но|под|над|при|через)$",
    re.I,
)
PROMOTIONAL_DETAIL = re.compile(
    r"\b(?:скидк\w*|акци\w*|перв\w+\s+(?:урок|занят\w*)\s+"
    r"(?:бесплат\w*|в\s+подарок)|в\s+подарок)\b",
    re.I,
)
UNNATURAL_TITLE = re.compile(r"^поиск\s+услуг\s+по\s+аренд", re.I)
BROKEN_PREPOSITIONS = re.compile(
    r"\b(?:за|от|до|из|для|на|в|с)\s+(?:за|от|до|из|для|на|в|с)\b",
    re.I,
)
NUMBER_WORDS = {
    "одна": "1", "одной": "1", "одну": "1",
    "две": "2", "двух": "2", "двумя": "2",
    "три": "3", "трех": "3", "трёх": "3", "тремя": "3",
    "четыре": "4", "четырех": "4", "четырёх": "4", "четырьмя": "4",
    "пять": "5", "пяти": "5", "пятью": "5",
}
SOURCE_SEEK = re.compile(
    r"(?:\b(?:ищу|ищем|сниму|куплю|требу(?:ется|ются))\b|"
    r"\b(?:мне|нам)\s+нуж(?:ен|на|ны)\b|"
    r"(?:^|[.!?\n])\s*нуж(?:ен|на|ны)\b|"
    r"\bактивн\w*\s+поиск\w*\b)",
    re.I,
)
TITLE_SEEK = re.compile(
    r"\b(?:ищу|ищем|ищет|сниму|куплю|нужен|нужна|нужны|требуется|поиск|ваканси\w*)\b",
    re.I,
)
FOOD = re.compile(
    r"\b(?:торт\w*|пирожн\w*|капкейк\w*|пряник\w*|выпечк\w*|"
    r"ед[аы]|десерт\w*)\b",
    re.I,
)
FLOWERS = re.compile(
    r"\b(?:цвет(?:ы|ов|ок|ка|ки|ами|ах|очн\w*)|букет\w*|"
    r"роз(?:а|ы|у|е|ой|ами|ах)?)\b",
    re.I,
)
FOOD_CATEGORY = re.compile(
    r"\b(?:ед[аы]|продукт\w*|выпечк\w*|десерт\w*|"
    r"цвет(?:ы|ов|ок|ка|ки|ами|ах|очн\w*)|букет\w*)\b",
    re.I,
)


def prepare_rows(records) -> list[dict]:
    return [
        {
            "id": record["id"],
            "text": SANITIZE_CONTACT.sub("", record["source_text"])[:420],
        }
        for record in records
    ]


def classification_signature(rows: list[dict]) -> str:
    return hashlib.sha256(
        (CLASSIFIER_VERSION + json.dumps(
            rows, ensure_ascii=False, sort_keys=True
        )).encode()
    ).hexdigest()


def classification_item_fingerprint(row: dict) -> str:
    return hashlib.sha256(
        (CLASSIFIER_VERSION + json.dumps(
            row, ensure_ascii=False, sort_keys=True
        )).encode()
    ).hexdigest()


def _valid_category(category: object) -> bool:
    return (
        isinstance(category, dict)
        and isinstance(category.get("code"), str)
        and CATEGORY_CODE.fullmatch(category["code"]) is not None
        and isinstance(category.get("title"), str)
        and RUSSIAN_TEXT.search(category["title"]) is not None
        and UKRAINIAN_ONLY.search(category["title"]) is None
        and INCOHERENT_CATEGORY.search(category["title"]) is None
        and isinstance(category.get("emoji", ""), str)
    )


def _validate_showcase_title(value: object, source_text: str = "") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("model response has empty title")
    title = " ".join(value.split())
    if len(title) > TITLE_MAX_LENGTH:
        raise ValueError(
            f"model response title is longer than {TITLE_MAX_LENGTH} characters"
        )
    # A model/brand-only title such as "BMW 520 TD" is language-neutral and
    # valid. Ukrainian-specific letters, however, prove that prose was not
    # normalized to Russian as required.
    if UKRAINIAN_ONLY.search(title):
        raise ValueError("model response title is not normalized to Russian")
    if TITLE_CONTACT.search(title):
        raise ValueError("model response title contains a contact or URL")
    if TITLE_PRICE.search(title):
        raise ValueError("model response title contains a price")
    if TITLE_GENERIC.fullmatch(title):
        raise ValueError("model response title is non-informative")
    if DANGLING_END.search(title):
        raise ValueError("model response title ends with a dangling word")
    if PROMOTIONAL_DETAIL.search(title):
        raise ValueError("model response title contains a promotional detail")
    if UNNATURAL_TITLE.search(title):
        raise ValueError("model response title uses an unnatural search phrase")
    if BROKEN_PREPOSITIONS.search(title):
        raise ValueError("model response contains adjacent prepositions")
    if source_text and SOURCE_SEEK.search(source_text) and not TITLE_SEEK.search(title):
        raise ValueError("model response changed a request into an offer")
    bedrooms = re.search(
        r"\b(\d+|одн(?:а|ой|у)|две|двух|двумя|три|тр[её]х|тремя|"
        r"четыре|четыр[её]х|четырьмя|пять|пяти|пятью)\s*"
        r"(?:спальн|bedrooms?\b|dormitorios?\b)",
        source_text,
        re.I,
    )
    bedroom_count = None
    if bedrooms:
        raw_count = bedrooms.group(1).casefold()
        bedroom_count = raw_count if raw_count.isdigit() else NUMBER_WORDS.get(raw_count)
    if bedroom_count and re.search(
        rf"\b{re.escape(bedroom_count)}\s*[- ]?комнатн", title, re.I
    ) and not re.search(rf"\b{re.escape(bedroom_count)}\s*спальн", title, re.I):
        raise ValueError("model response changed bedrooms into rooms")
    if title.count("(") != title.count(")") or title.count("[") != title.count("]"):
        raise ValueError("model response title has unbalanced punctuation")
    return title


def _sanitize_showcase_title(value: object, source_text: str = "") -> str:
    """Remove forbidden data that can be deleted without changing the offer."""
    if not isinstance(value, str):
        raise ValueError("model response has empty title")
    title = SANITIZE_CONTACT.sub(" ", value)
    # Keep cleanup and validation aligned. The broad validation expression is
    # intentionally applied second so any contact it can reject is also
    # removable; the result is validated again below.
    title = TITLE_CONTACT.sub(" ", title)
    title = TITLE_PRICE.sub(" ", title)
    title = re.sub(r"\s*[·•|]+\s*", " ", title)
    title = re.sub(r"(?:\s*[-\N{EN DASH}\N{EM DASH},:;/]+\s*)*[)\]]+\s*$", "", title)
    title = re.sub(r"\s+", " ", title).strip(" ,;:|/\N{EN DASH}\N{EM DASH}-")
    if len(title) > TITLE_MAX_LENGTH:
        shortened = title[:TITLE_MAX_LENGTH].rsplit(" ", 1)[0]
        title = shortened.rstrip(" ,;:|/\N{EN DASH}\N{EM DASH}-")
        # A cut inside a parenthetical detail must not leave invalid output.
        if title.count("(") > title.count(")"):
            title = title.rsplit("(", 1)[0].rstrip(" ,;:-")
        if title.count("[") > title.count("]"):
            title = title.rsplit("[", 1)[0].rstrip(" ,;:-")
    while DANGLING_END.search(title):
        title = title.rsplit(" ", 1)[0].rstrip(" ,;:|/\N{EN DASH}\N{EM DASH}-")
    return _validate_showcase_title(title, source_text)


def _validate_category_assignment(source_text: str, category_title: str) -> None:
    if (FOOD.search(source_text) or FLOWERS.search(source_text)) and not FOOD_CATEGORY.search(category_title):
        raise ValueError("food or flowers assigned outside their digest section")


def _ensure_editorial_categories(categories: list[dict], rows: list[dict]) -> tuple[list[dict], bool]:
    """Add a dynamic broad section only when this month's sample needs it."""
    has_food = any(FOOD.search(row["text"]) for row in rows)
    has_flowers = any(FLOWERS.search(row["text"]) for row in rows)
    if not has_food and not has_flowers:
        return categories, False
    if any(FOOD_CATEGORY.search(category["title"]) for category in categories):
        return categories, False
    used = {category["code"] for category in categories}
    code = "food_flowers"
    suffix = 2
    while code in used:
        code = f"food_flowers_{suffix}"
        suffix += 1
    if has_food and has_flowers:
        title, emoji = "Еда и цветы", "🍰🌸"
    elif has_food:
        title, emoji = "Еда и доставка", "🍰"
    else:
        title, emoji = "Цветы и букеты", "🌸"
    return [*categories, {"code": code, "title": title, "emoji": emoji}], True


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
    # Free models occasionally wrap the requested object in a one-element
    # array despite JSON mode. This shape is unambiguous and safe to unwrap.
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        value = value[0]
    if not isinstance(value, dict):
        raise ValueError("model response is not a JSON object")
    return value


def prompt(rows: list[dict], categories: list[dict] | None = None) -> str:
    if categories:
        return """Верни ТОЛЬКО JSON: {\"entries\":[{\"id\":1,\"include\":true,\"category\":\"разрешённый_code\",\"title\":\"...\",\"confidence\":\"high|low\"}]}.
Для КАЖДОГО переданного id верни ровно один объект. Используй только разрешённые категории. Не пиши цену, контакты, URL; другой город укажи. Если это не самостоятельное объявление, include=false, но title всё равно заполни кратко.
Витринная строка ВСЕГДА на русском, даже если исходник на украинском, испанском или английском. Имена, бренды, города и даты сохраняй.
Title — одна информативная строка, обычно 40–75 и максимум 95 символов.
Не повторяй в title название категории, не используй капслок и рекламные эпитеты.
Сохраняй направление объявления: «ищу/сниму/куплю/требуется» нельзя превращать в предложение.
Не добавляй скидки, акции, подарки и бесплатный пробный урок. Не обрывай строку на предлоге или союзе.
После удаления цены перечитай фразу: не оставляй сочетания вроде «за в городе».
Пиши естественно: вместо «поиск услуг по аренде автомобиля» — «ищу автомобиль в аренду».
Не заменяй число спален числом комнат.
Еду, выпечку, десерты и цветы помещай только в соответствующий раздел еды/цветов.
Включай только конкретное предложение или запрос товара, услуги, жилья, работы, транспорта, обучения либо мероприятия. Погода, новости, отзывы, обсуждения и общие вопросы без конкретного запроса — include=false.
Разрешённые категории: """ + json.dumps(categories, ensure_ascii=False) + "\nДанные:\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return """Ты редактор ежемесячного Telegram-дайджеста городской группы Гуардамар.
Верни ТОЛЬКО JSON: {\"categories\":[{\"code\":\"...\",\"title\":\"...\",\"emoji\":\"...\"}],\"entries\":[{\"id\":1,\"include\":true,\"category\":\"...\",\"title\":\"...\",\"confidence\":\"high|low\"}]}.
Создай только широкие категории, нужные этому месяцу. Одна строка — одно объявление.
Не повторяй название категории в title. Гуардамар не указывай, но другой город указывай обязательно.
Не включай цену, контакты, URL, рекламные эпитеты. Предпочитай важные факты: формат, дата, маршрут, срок, спальни, район, бренд.
Витринная строка всегда на русском, независимо от языка исходного сообщения.
Старайся сделать title до 34 символов, но не удаляй существенный факт ради длины. Не дублируй буквальные title в одной категории.
Исключи ответы, обсуждения, сервисные сообщения и сообщения без самостоятельного объявления.
""" + ("Используй ТОЛЬКО этот план категорий: " + json.dumps(categories, ensure_ascii=False) + "\n" if categories else "") + "Данные:\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def plan_prompt(rows: list[dict]) -> str:
    return """Верни ТОЛЬКО JSON:
{\"categories\":[{\"code\":\"ascii_code\",\"title\":\"Русское название\",\"emoji\":\"...\"}]}.
Создай широкие категории только для объявлений текущего месяца.
Название каждой категории обязательно пиши по-русски независимо от языка
исходных объявлений. Категория должна объединять одну понятную потребность
читателя. Не смешивай товары с услугами, работу с обучением, транспорт с
недвижимостью. Не создавай микрокатегории; обычно достаточно 5–9 разделов.
Данные:\n""" + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def classify(settings: Settings, period: str) -> str:
    # Refuse to spend scarce free-model quota on unfiltered duplicates or stale
    # imports. Local imports avoid a module cycle: dedupe itself uses _post.
    from .dedupe import VERSION as dedupe_version
    from .prefilter import VERSION as prefilter_version, _period_cutoff

    with connect(settings.db_path) as con:
        stages = {
            row["stage"]: row
            for row in con.execute(
                """SELECT stage,status,rule_version,details FROM workflow_runs
                   WHERE period_key=? AND stage IN ('prefilter','semantic_dedupe')""",
                (period,),
            )
        }
        if (
            not stages.get("prefilter")
            or stages["prefilter"]["status"] != "complete"
            or stages["prefilter"]["rule_version"] != prefilter_version
            or not stages.get("semantic_dedupe")
            or stages["semantic_dedupe"]["status"] != "complete"
            or stages["semantic_dedupe"]["rule_version"] != dedupe_version
        ):
            raise RuntimeError(
                "Prefilter and automatic duplicate arbitration must complete before classification"
            )
        try:
            prefilter_as_of = json.loads(
                stages["prefilter"]["details"] or "{}"
            ).get("as_of")
        except (TypeError, json.JSONDecodeError):
            prefilter_as_of = None
        expected_as_of = _period_cutoff(period, None).isoformat()
        if prefilter_as_of != expected_as_of:
            raise RuntimeError(
                f"Prefilter is stale: expected publication date {expected_as_of}"
            )
        unresolved = con.execute(
            """SELECT COUNT(*) FROM duplicate_reviews
               WHERE period_key=? AND status IN ('pending','uncertain')""",
            (period,),
        ).fetchone()[0]
        if unresolved:
            raise RuntimeError(
                f"Duplicate arbitration is incomplete: {unresolved} unresolved pairs"
            )
        records = con.execute(
            "SELECT m.id, m.message_id, m.source_text FROM messages m JOIN entries e ON e.message_id=m.id "
            "WHERE e.period_key=? AND e.manual_title IS NULL AND e.excluded_reason IS NULL "
            "AND e.needs_duplicate_review=0 ORDER BY m.message_id",
            (period,),
        ).fetchall()
    external_ids = {record["id"]: record["message_id"] for record in records}
    source_texts = {record["id"]: record["source_text"] for record in records}
    rows = prepare_rows(records)
    item_fingerprints = {
        row["id"]: classification_item_fingerprint(row) for row in rows
    }
    if not rows:
        return "nothing to classify"
    signature=classification_signature(rows)
    with connect(settings.db_path) as con:
        run=con.execute("SELECT * FROM classification_runs WHERE period_key=?",(period,)).fetchone()
    fixed_categories = None
    if run and run["input_signature"] == signature:
      try:
        cached_categories = json.loads(run["categories_json"])
        if (
          isinstance(cached_categories,list) and cached_categories
          and all(_valid_category(c) for c in cached_categories)
          and len({c["code"] for c in cached_categories})==len(cached_categories)
        ):
          fixed_categories=cached_categories
      except (TypeError,json.JSONDecodeError):
        fixed_categories=None
    cached_plan_valid = fixed_categories is not None
    # The category plan is cheap: all texts are shortened and no per-entry
    # output is requested. It prevents the first batch from defining the month.
    plan_rows = [{"id": row["id"], "text": row["text"][:150]} for row in rows]
    for provider in (() if fixed_categories else ("gemini", "openrouter")):
      try:
        content=plan_prompt(plan_rows)
        if provider=="gemini" and settings.gemini_key:
          raw=_post(f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent?key={settings.gemini_key}",{"Content-Type":"application/json"},{"contents":[{"parts":[{"text":content}]}],"generationConfig":{"responseMimeType":"application/json","maxOutputTokens":1024}}); result=_json(raw["candidates"][0]["content"]["parts"][0]["text"])
        elif provider=="openrouter" and settings.openrouter_key:
          raw=_post("https://openrouter.ai/api/v1/chat/completions",{"Content-Type":"application/json","Authorization":f"Bearer {settings.openrouter_key}"},{"model":settings.openrouter_model,"messages":[{"role":"user","content":content}],"response_format":{"type":"json_object"},"max_tokens":1024}); result=_json(raw["choices"][0]["message"]["content"])
        else: continue
        fixed_categories=[
          c for c in result.get("categories",[])
          if _valid_category(c)
        ]
        if fixed_categories and len({c["code"] for c in fixed_categories})==len(fixed_categories): break
        fixed_categories=None
      except Exception: continue
    if not fixed_categories: raise RuntimeError("Could not create category plan with free LLM providers")
    fixed_categories, categories_added = _ensure_editorial_categories(
      fixed_categories,
      [{"text": source_texts[row["id"]]} for row in rows],
    )
    if categories_added:
      cached_plan_valid = False
    if not run or run["input_signature"]!=signature or not cached_plan_valid:
      run_id=uuid.uuid4().hex
      previous_run_id = run["run_id"] if run else None
      fixed_map = {category["code"]: category for category in fixed_categories}
      with connect(settings.db_path) as con:
        con.execute("INSERT OR REPLACE INTO classification_runs(period_key,run_id,input_signature,categories_json,status,lock_until) VALUES (?,?,?,?, 'running', datetime('now','+20 minutes'))",(period,run_id,signature,json.dumps(fixed_categories,ensure_ascii=False)))
        for row in rows:
          existing = con.execute(
            """SELECT eligible,category_code,short_title,classification_run_id,
                      classification_fingerprint,classification_version
               FROM entries WHERE period_key=? AND message_id=?""",
            (period,row["id"]),
          ).fetchone()
          reusable = bool(
            existing and existing["category_code"] in fixed_map
            and existing["short_title"]
            and (
              (
                existing["classification_version"] == CLASSIFIER_VERSION
                and existing["classification_fingerprint"] == item_fingerprints[row["id"]]
              )
              or (
                existing["classification_version"] is None
                and previous_run_id
                and existing["classification_run_id"] == previous_run_id
              )
            )
          )
          if reusable:
            try:
              source_text = source_texts[row["id"]]
              _validate_showcase_title(existing["short_title"], source_text)
              if existing["eligible"]:
                _validate_category_assignment(
                  source_text, fixed_map[existing["category_code"]]["title"]
                )
            except ValueError:
              reusable = False
          if reusable:
            category = fixed_map[existing["category_code"]]
            con.execute(
              """UPDATE entries SET category_title=?,category_emoji=?,
                 classification_run_id=?,classification_fingerprint=?,
                 classification_version=? WHERE period_key=? AND message_id=?""",
              (category.get("title"),category.get("emoji"),run_id,
               item_fingerprints[row["id"]],CLASSIFIER_VERSION,period,row["id"]),
            )
          else:
            con.execute(
              """UPDATE entries SET category_code=NULL,category_title=NULL,
                 category_emoji=NULL,short_title=NULL,confidence=NULL,provider=NULL,
                 classification_run_id=NULL,classification_fingerprint=NULL,
                 classification_version=NULL WHERE period_key=? AND message_id=?
                 AND manual_title IS NULL""",
              (period,row["id"]),
            )
    else:
      run_id=run["run_id"]
      with connect(settings.db_path) as con:
        con.execute(
          """UPDATE classification_runs SET status='running',
             lock_until=datetime('now','+20 minutes'),updated_at=CURRENT_TIMESTAMP
             WHERE period_key=? AND run_id=?""",
          (period,run_id),
        )
    # A completed run from an older editorial validator may contain a title
    # that is formally valid but changes intent or ends mid-phrase. Remove only
    # those checkpoints; all other LLM work remains reusable.
    fixed_map = {category["code"]: category for category in fixed_categories}
    row_map = {row["id"]: row for row in rows}
    with connect(settings.db_path) as con:
      checkpoints = con.execute(
        """SELECT message_id,eligible,category_code,short_title FROM entries
           WHERE period_key=? AND classification_run_id=? AND manual_title IS NULL""",
        (period, run_id),
      ).fetchall()
      for checkpoint in checkpoints:
        row = row_map.get(checkpoint["message_id"])
        category = fixed_map.get(checkpoint["category_code"])
        try:
          if row is None or category is None:
            raise ValueError("checkpoint is outside the current input plan")
          source_text = source_texts[checkpoint["message_id"]]
          _validate_showcase_title(checkpoint["short_title"], source_text)
          if checkpoint["eligible"]:
            _validate_category_assignment(source_text, category["title"])
        except ValueError:
          con.execute(
            """UPDATE entries SET category_code=NULL,category_title=NULL,
               category_emoji=NULL,short_title=NULL,confidence=NULL,provider=NULL,
               classification_run_id=NULL,classification_fingerprint=NULL,
               classification_version=NULL
               WHERE period_key=? AND message_id=? AND manual_title IS NULL""",
            (period, checkpoint["message_id"]),
          )
    provider_used = []
    all_entries = []
    with connect(settings.db_path) as con:
      done={r["message_id"] for r in con.execute("SELECT message_id FROM entries WHERE period_key=? AND classification_run_id=?",(period,run_id))}
    for offset in range(0, len(rows), 1):
      batch = rows[offset:offset + 1]
      if batch[0]["id"] in done: continue
      errors = []
      providers = ("gemini", "openrouter") * MODEL_RESPONSE_ATTEMPTS
      for provider in providers:
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
            returned_ids = {
                entry.get("id") for entry in returned
                if isinstance(entry, dict)
                and isinstance(entry.get("id"), int)
                and not isinstance(entry.get("id"), bool)
            }
            if (
                not isinstance(returned, list)
                or len(returned) != len(expected_ids)
                or returned_ids != expected_ids
            ):
                raise ValueError(
                    f"incomplete model response: expected {len(expected_ids)} entries, got {len(returned_ids)}"
                )
            fixed_map={c["code"]:c for c in fixed_categories}
            if any(entry.get("category") not in fixed_map for entry in returned): raise ValueError("model used a category outside the fixed plan")
            with connect(settings.db_path) as con:
              entry=returned[0]
              source_text = source_texts[entry["id"]]
              title = _sanitize_showcase_title(entry.get("title"), source_text)
              if not isinstance(entry.get("include"),bool): raise ValueError("model response has non-boolean include")
              category=fixed_map[entry["category"]]
              if entry["include"]:
                _validate_category_assignment(source_text, category["title"])
              con.execute("UPDATE entries SET eligible=?,category_code=?,category_title=?,category_emoji=?,short_title=?,confidence=?,provider=?,classification_run_id=?,classification_fingerprint=?,classification_version=? WHERE message_id=? AND period_key=?",(int(entry["include"]),entry["category"],category.get("title"),category.get("emoji"),title,entry.get("confidence"),provider,run_id,item_fingerprints[entry["id"]],CLASSIFIER_VERSION,entry["id"],period))
            provider_used.append(provider); break
        except HTTPError as exc:
            errors.append(f"{provider}: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:800]}")
        except (IncompleteRead, JSONDecodeError, KeyError, ValueError, URLError, TimeoutError, OSError) as exc:
            errors.append(f"{provider}: {exc}")
      else:
        external_id = external_ids.get(batch[0]["id"], batch[0]["id"])
        raise RuntimeError(
          f"Telegram message {external_id}: "
          + ("; ".join(errors) or "No LLM API key configured")
        )
    with connect(settings.db_path) as con:
      remaining = con.execute(
        """SELECT COUNT(*) FROM entries e JOIN messages m ON m.id=e.message_id
           WHERE e.period_key=? AND e.manual_title IS NULL
             AND e.excluded_reason IS NULL AND e.needs_duplicate_review=0
             AND (e.classification_run_id IS NULL OR e.classification_run_id<>?)""",
        (period, run_id),
      ).fetchone()[0]
      if remaining:
        raise RuntimeError(
          f"classification checkpoint is incomplete: {remaining} entries remain"
        )
      con.execute("UPDATE classification_runs SET status='complete',lock_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE period_key=? AND run_id=?",(period,run_id))
    return "+".join(sorted(set(provider_used)))
