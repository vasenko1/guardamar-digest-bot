from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from .db import connect
from .llm import (
    OTHER_LOCATION_ALIASES,
    REAL_ESTATE_SECTIONS,
    _compact_realestate_title,
    _realestate_mode,
)


VERSION = "2026-08-03.4"
VALID_INTENTS = {
    "sale_offer", "purchase_seek", "giveaway", "rent_offer", "rent_seek",
    "service_offer", "service_seek", "job_offer", "job_seek",
    "trip_offer", "other",
}
VALID_LOCATION_SCOPES = {"local", "outside", "mixed", "unspecified"}
INTENT_SUBSECTIONS = {
    "sale_offer": "Продажа",
    "purchase_seek": "Куплю",
    "giveaway": "Отдам",
    "rent_offer": "Сдам в аренду",
    "rent_seek": "Сниму в аренду",
    "service_offer": "Предлагаю услуги",
    "service_seek": "Ищу специалиста",
    "job_offer": "Требуется",
    "job_seek": "Ищу работу",
    "trip_offer": "Поездки и трансфер",
}
LOCAL_CITY = re.compile(
    r"(?:guardamar\s+del\s+segura|guardamar|"
    r"г(?:у|в)ардамар(?:[-\s]+дель[-\s]+сегура)?\w*)",
    re.I,
)
LOCAL_CITY_IN_TITLE = re.compile(
    r"(?:\s*,?\s*(?:в\s+|г\.?\s*)?)"
    r"(?:guardamar\s+del\s+segura|guardamar|"
    r"г(?:у|в)ардамар(?:[-\s]+дель[-\s]+сегура)?\w*)",
    re.I,
)
SELL = re.compile(r"\b(?:продам|прода[её]тся|продаж[аи]|продаю)\b", re.I)
PRICE = re.compile(r"\d[\d\s.,]*\s*(?:€|eur\b|евро\b|грн\b|₽|\$)", re.I)
GIVE = re.compile(r"\b(?:отдам|віддам|даром)\b", re.I)
FREE = re.compile(r"\b(?:бесплатно|безкоштовно|в\s+хорошие\s+руки)\b", re.I)
SEEK = re.compile(
    r"\b(?:ищу|ищем|куплю|шукаю|кто\s+занимается|кто\s+прода[её]т|"
    r"хтось\s+прода[єе]|где\s+(?:можно\s+)?приобрести|подскажите)\b",
    re.I,
)
RENT = re.compile(
    r"\b(?:аренд\w*|оренд\w*|сда(?:м|ю|ём|ем|ете|ют|ется|ются)|"
    r"зда(?:м|ю|ємо|єте|ють|ється|ються)|снять|сниму)\b",
    re.I,
)
JOB_SEEK = re.compile(
    r"\bищ(?:у|ем|ет|ут)\s+работ\w*|\bшука\w*\s+робот\w*|"
    r"\bготов\w*\s+приступить\b",
    re.I,
)
JOB_OFFER = re.compile(
    r"\b(?:ваканси\w*|требу(?:ется|ются)|в\s+поисках\s+сотрудник\w*|"
    r"ищем\s+(?:ответственн\w+\s+)?(?:специалист|сотрудник|работник|мастер)\w*|"
    r"потріб\w*\s+(?:працівник|робітник|майстер)|"
    r"ищу\s+нян\w*|требу(?:ется|ются)\s+нян\w*|работа\s*[!:.\n])",
    re.I,
)
TRIP = re.compile(
    r"\b(?:трансфер\w*|попутчик\w*|поездк\w*|еду|їхат\w*|аэропорт\w*|вокзал\w*)\b",
    re.I,
)
SERVICE_DOMINANT = re.compile(
    r"\b(?:ремонт\w*|клининг\w*|химчистк\w*|кухн\w*\s+под\s+заказ|"
    r"окн\w*\s+(?:и|или)\s+двер\w*|кондиционирован\w*|кондиционер\w*|"
    r"холодильн\w*\s+оборудован\w*)\b",
    re.I,
)
SMM_OFFER = re.compile(
    r"\bищу\s+(?:всего\s+)?\d*\s*проект\w*\b.*\b(?:ведение|контент|"
    r"reels|stories|продвижен\w*|беру\s+на\s+себя)\b",
    re.I | re.S,
)


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).replace("—", "-").split())


def editorial_fingerprint(source: str, category: str, title: str, manual: str) -> str:
    payload = json.dumps(
        [VERSION, source, category, title, manual], ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def infer_location(source: str, title: str) -> tuple[str, str | None]:
    combined = _normalized(f"{source}\n{title}").casefold()
    local = bool(LOCAL_CITY.search(combined))
    outside_names = [
        display for display, aliases in OTHER_LOCATION_ALIASES
        if any(alias in combined for alias in aliases)
    ]
    if outside_names:
        scope = "mixed" if local or len(outside_names) > 1 else "outside"
        return scope, ", ".join(outside_names)
    if local:
        return "local", None
    return "unspecified", None


def infer_intent(category_title: str, source: str) -> str:
    category = category_title.casefold()
    if category_title in {value[1] for value in REAL_ESTATE_SECTIONS.values()}:
        return {
            "Сдам в аренду": "rent_offer", "Сниму в аренду": "rent_seek",
            "Продам": "sale_offer", "Куплю": "purchase_seek",
        }[category_title]
    if "работ" in category or "ваканси" in category:
        if JOB_SEEK.search(source):
            return "job_seek"
        return "job_offer" if JOB_OFFER.search(source) else "other"
    if "транспорт" in category or "авто" in category:
        if RENT.search(source):
            return "rent_seek" if SEEK.search(source) else "rent_offer"
        if SELL.search(source) or re.search(r"\b(?:19|20)\d{2}\b", source):
            return "sale_offer"
        if TRIP.search(source):
            return "trip_offer"
        return "service_offer"
    if any(word in category for word in ("товар", "вещ", "одежд")):
        if RENT.search(source):
            return "rent_seek" if SEEK.search(source) else "rent_offer"
        if SELL.search(source) or PRICE.search(source):
            return "sale_offer"
        if GIVE.search(source) and FREE.search(source):
            return "giveaway"
        if SEEK.search(source):
            return "purchase_seek"
        return "sale_offer"
    if any(word in category for word in ("услуг", "красот", "здоров")):
        if SMM_OFFER.search(source):
            return "service_offer"
        return "service_seek" if SEEK.search(source) else "service_offer"
    return "other"


def service_title(source: str, existing: str) -> str:
    if re.search(r"\bкондиционер|холодильн", source, re.I):
        return "Кондиционеры и холодильное оборудование"
    if re.search(r"\bокн\w*\b", source, re.I) and re.search(r"\bдвер\w*\b", source, re.I):
        return "Окна и двери"
    if re.search(r"\bремонт", source, re.I) and re.search(r"\bклининг|уборк", source, re.I):
        return "Ремонт, кухни и клининг"
    return existing


def normalize_title(intent: str, category: str, source: str, existing: str) -> str:
    title = existing
    lowered = source.casefold()
    original_scope, original_location = infer_location(source, existing)
    original_title_has_location = bool(
        original_location and any(
            alias in existing.casefold()
            for display, aliases in OTHER_LOCATION_ALIASES
            if display == original_location
            for alias in aliases
        )
    )
    # Stable subject templates avoid mechanically truncated phrases and retain
    # only a distinguishing fact that is explicit in the source.
    templates = (
        (r"\bcosatto\b", "Автокресло Cosatto"),
        (r"\bchicco\s+baby\s+hug\b", "Колыбель Chicco Baby Hug"),
        (r"\bвелосипед\b.*\b28\b", "Велосипед, 28 дюймов"),
        (r"\bкроватк\w*[-\s]?трансформер", "Кроватка-трансформер"),
        (r"\bавтокресл\w*.*\b360", "Автокресло с поворотом 360°"),
        (r"\bавтокресл\w*.*(?:18|20)\s*кг", "Автокресло, 0–20 кг"),
        (r"дв(?:ух|о)ярусн\w*\s+кроват|двояросне\s+ліжко", "Двухъярусная кровать"),
        (r"\bдуховк\w*\b", "Духовка"),
        (r"\bодежд\w*\b.*\b68[-–]80\b", "Одежда для девочки, 68–80 см"),
        (r"вещ\w*\s+для\s+мам\w*.*новорожден", "Вещи для мамы и новорождённого"),
        (r"диван.*2\s+матрас", "Диван и два матраса, 80–90 × 200 см"),
    )
    if any(word in category.casefold() for word in ("товар", "вещ", "одежд")):
        for pattern, replacement in templates:
            if re.search(pattern, lowered, re.I | re.S):
                title = replacement
                break
    if "обуч" in category.casefold() or "курс" in category.casefold():
        if "bebest" in lowered and "онлайн-курс" in lowered:
            title = "Испанский язык в школе BeBest"
        elif re.search(r"лекц\w*\s+курс|лекційний\s+курс", lowered):
            title = "Испанский язык, 2 раза в неделю"
        elif re.search(r"английск\w*|англійськ\w*", lowered) and re.search(r"от\s+5|від\s+5", lowered):
            title = "Английский язык для детей от 5 лет"
        elif re.search(r"шахмат\w*|шахи", lowered):
            title = "Занятия по шахматам"
        elif re.search(r"рисован\w*|малюван\w*", lowered):
            title = "Рисование для детей"
        elif re.search(r"іспанськ|испанск", lowered) and re.search(r"дітей|детей", lowered):
            title = "Испанский язык онлайн для детей и взрослых"
        elif re.search(r"іспанськ|испанск", lowered) and re.search(r"онлайн", lowered):
            title = "Испанский язык онлайн"
    if "работ" in category.casefold() or "ваканси" in category.casefold():
        if re.search(r"рихтовщик|кузовщик|сварщик", lowered):
            title = "Рихтовщик, кузовщик, сварщик, подготовщик"
        elif re.search(r"помощник\w*\s+по\s+кухн", lowered):
            title = "Помощник на кухню"
        elif re.search(r"сотрудник\w*\s+на\s+кухн", lowered):
            title = "Сотрудник на кухню"
        elif re.search(r"\bнян\w*\b", lowered) and intent == "job_offer":
            title = "Няня для детей 1 и 5 лет"
        elif intent == "job_seek":
            title = "Полная или частичная занятость"
    is_transport = "транспорт" in category.casefold() or "авто" in category.casefold()
    if (
        not is_transport
        and any(word in category.casefold() for word in ("услуг", "красот", "здоров"))
    ):
        title = service_title(source, title)
        if re.search(r"\bтрансфер\w*", lowered):
            title = "Трансфер"
        service_templates = (
            (r"очищен\w*.*(?:бактери|микроб)|антибактериальн", "Антибактериальная обработка салона"),
            (r"пошив.*ремонт\s+одежд", "Пошив и ремонт одежды"),
            (r"разработк\w*.*(?:сайт|приложен)", "Разработка сайтов и приложений"),
            (r"перетяжк\w*\s+рул", "Перетяжка руля"),
            (r"ремонтн\w*.*строительн|строительн\w*.*ремонтн", "Ремонт и строительные работы"),
            (r"(?:рилс|reels).*монтаж|сниму.*видеоролик", "Съёмка и монтаж Reels"),
            (r"косметолог", "Косметолог"),
            (r"маникюр", "Мастер маникюра" if intent == "service_seek" else "Маникюр"),
            (r"ламинирован\w*\s+ресниц", "Ламинирование ресниц"),
            (r"шугаринг", "Шугаринг"),
            (r"остеопат|мануальн\w*\s+терап", "Остеопатия и мануальная терапия"),
        )
        for pattern, replacement in service_templates:
            if re.search(pattern, lowered, re.I | re.S):
                title = replacement
                break
    if re.search(r"ед[аы]|цвет|букет|торт|десерт", category, re.I):
        if re.search(r"букет|цвет", lowered):
            title = "Доставка букетов"
        elif re.search(r"торт|пирожн|десерт", lowered):
            title = "Торты и десерты"
    if intent == "service_offer":
        if SMM_OFFER.search(source):
            title = "Ведение Instagram"
        title = re.sub(r"^(?:предоставляю\s+)?услуг[аи]\s+(?:по\s+)?", "", title, flags=re.I)
    elif intent == "service_seek":
        title = re.sub(r"^(?:ищу|требуется)\s+", "", title, flags=re.I)
    elif intent == "purchase_seek":
        title = re.sub(r"^(?:ищу|куплю|покупка)\s+", "", title, flags=re.I)
    elif intent == "giveaway":
        title = re.sub(r"^(?:отдам|віддам)(?:\s+бесплатно)?\s+", "", title, flags=re.I)
    elif intent == "job_offer":
        title = re.sub(r"^ваканси[ия]\s+", "", title, flags=re.I)
    elif intent == "job_seek":
        title = re.sub(r"^ищу\s+работу\s*", "", title, flags=re.I)
    elif intent == "rent_seek" and ("транспорт" in category.casefold() or "авто" in category.casefold()):
        title = "Автомобиль"
    elif intent == "rent_offer" and ("транспорт" in category.casefold() or "авто" in category.casefold()):
        if not re.search(r"\b(?:19|20)\d{2}\b", source):
            title = "Автомобили"
    title = LOCAL_CITY_IN_TITLE.sub("", title)
    for display, aliases in OTHER_LOCATION_ALIASES:
        for alias in sorted(aliases, key=len, reverse=True):
            title = re.sub(
                rf"\b{re.escape(alias)}\w*\b", display, title, flags=re.I
            )
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"\s+([,;:])", r"\1", title)
    title = title.strip(" ,;:–—-")
    append_outside_location = bool(
        original_location and (
            original_title_has_location
            or (
                original_scope == "outside"
                and not (
                    "транспорт" in category.casefold()
                    and intent in {"rent_offer", "service_offer"}
                )
            )
        )
    )
    if (
        append_outside_location
        and not any(alias in title.casefold() for display, aliases in OTHER_LOCATION_ALIASES
                    if display == original_location for alias in aliases)
    ):
        title = f"{title}, {original_location}"
    return title or existing


def normalize_period(settings, period: str) -> dict[str, int]:
    counts = {"examined": 0, "changed": 0, "category_repairs": 0}
    with connect(settings.db_path) as con:
        run = con.execute(
            "SELECT status,categories_json FROM classification_runs WHERE period_key=?",
            (period,),
        ).fetchone()
        if not run or run["status"] != "complete":
            raise RuntimeError("Classification must complete before editorial normalization")
        categories = json.loads(run["categories_json"])
        service_category = next(
            (item for item in categories if re.search(r"услуг", item.get("title", ""), re.I)),
            None,
        )
        if service_category is None:
            possible_services = con.execute(
                """SELECT m.source_text FROM entries e JOIN messages m ON m.id=e.message_id
                   WHERE e.period_key=? AND e.eligible=1 AND e.excluded_reason IS NULL
                     AND e.manual_category IS NULL
                     AND e.category_title IN (?,?,?,?)
                """,
                (period, *(value[1] for value in REAL_ESTATE_SECTIONS.values())),
            ).fetchall()
            if any(SERVICE_DOMINANT.search(row["source_text"]) for row in possible_services):
                used_codes = {item.get("code") for item in categories}
                code = "services"
                suffix = 2
                while code in used_codes:
                    code = f"services_{suffix}"
                    suffix += 1
                service_category = {
                    "code": code,
                    "title": "Бытовые и профессиональные услуги",
                    "emoji": "🛠️",
                }
                categories.append(service_category)
                con.execute(
                    "UPDATE classification_runs SET categories_json=? WHERE period_key=?",
                    (json.dumps(categories, ensure_ascii=False), period),
                )
        con.execute(
            """INSERT OR REPLACE INTO workflow_runs
               (period_key,stage,rule_version,status,details,completed_at)
               VALUES (?,'editorial_normalization',?,'running',NULL,CURRENT_TIMESTAMP)""",
            (period, VERSION),
        )
        rows = con.execute(
            """SELECT e.*,m.source_text FROM entries e JOIN messages m ON m.id=e.message_id
               WHERE e.period_key=? AND e.eligible=1 AND e.excluded_reason IS NULL
               ORDER BY m.chat_id,m.message_id""",
            (period,),
        ).fetchall()
        for row in rows:
            counts["examined"] += 1
            category_code = row["category_code"]
            category_title = row["manual_category"] or row["category_title"] or ""
            category_emoji = row["category_emoji"]
            title = row["manual_title"] or row["short_title"] or ""
            reason = "metadata refresh"
            realestate_mode = _realestate_mode(row["source_text"])
            if (
                row["manual_category"] is None
                and realestate_mode
                and category_title != REAL_ESTATE_SECTIONS[realestate_mode][1]
            ):
                category_code, category_title = REAL_ESTATE_SECTIONS[realestate_mode]
                category_emoji = ""
                if row["manual_title"] is None:
                    title = _compact_realestate_title(row["source_text"])
                reason = "real estate moved to correct intent subsection"
                counts["category_repairs"] += 1
            if (
                row["manual_category"] is None
                and category_title in {value[1] for value in REAL_ESTATE_SECTIONS.values()}
                and SERVICE_DOMINANT.search(row["source_text"])
                and service_category
            ):
                category_code = service_category["code"]
                category_title = service_category["title"]
                category_emoji = service_category.get("emoji", "🛠")
                if row["manual_title"] is None:
                    title = service_title(row["source_text"], title)
                reason = "service repaired from false real-estate match"
                counts["category_repairs"] += 1
            intent = infer_intent(category_title, row["source_text"])
            if row["manual_title"] is None:
                title = normalize_title(intent, category_title, row["source_text"], title)
            scope, location = infer_location(row["source_text"], title)
            fingerprint = editorial_fingerprint(
                row["source_text"], category_title, title, row["manual_title"] or ""
            )
            changed = any((
                row["category_code"] != category_code,
                row["category_title"] != category_title,
                row["category_emoji"] != category_emoji,
                row["short_title"] != title and row["manual_title"] is None,
                row["intent_code"] != intent,
                row["location_scope"] != scope,
                row["location_name"] != location,
                row["editorial_fingerprint"] != fingerprint,
                row["editorial_version"] != VERSION,
            ))
            if changed:
                counts["changed"] += 1
            con.execute(
                """UPDATE entries SET category_code=?,category_title=?,category_emoji=?,
                   short_title=CASE WHEN manual_title IS NULL THEN ? ELSE short_title END,
                   intent_code=?,location_scope=?,location_name=?,editorial_fingerprint=?,
                   editorial_version=?,editorial_reason=? WHERE message_id=?""",
                (category_code, category_title, category_emoji, title, intent, scope,
                 location, fingerprint, VERSION, reason, row["message_id"]),
            )
            con.execute(
                """INSERT OR REPLACE INTO editorial_audit
                   (period_key,message_id,stage,decision,reason_code,reason_detail,
                    confidence,provider,rule_version)
                   VALUES (?,?,'editorial_normalization','normalize','metadata',?,
                           'high','rule',?)""",
                (period, row["message_id"], reason, VERSION),
            )
        con.execute(
            """UPDATE workflow_runs SET status='complete',details=?,completed_at=CURRENT_TIMESTAMP
               WHERE period_key=? AND stage='editorial_normalization'""",
            (json.dumps(counts, ensure_ascii=False), period),
        )
    return counts
