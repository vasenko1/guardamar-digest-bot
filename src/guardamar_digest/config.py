from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    root: Path
    db_path: Path
    source_username: str
    source_chat_id: str
    bot_token: str
    admin_chat_id: str
    gemini_key: str
    gemini_model: str
    openrouter_key: str
    openrouter_model: str
    excluded_sender_ids: frozenset[str]


def settings() -> Settings:
    load_dotenv(ROOT / ".env")
    return Settings(
        root=ROOT,
        db_path=ROOT / "state" / "digest.sqlite3",
        source_username=os.getenv("TELEGRAM_SOURCE_USERNAME", "").lstrip("@"),
        source_chat_id=os.getenv("TELEGRAM_SOURCE_CHAT_ID", ""),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        admin_chat_id=os.getenv("TELEGRAM_ADMIN_CHAT_ID", ""),
        gemini_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"),
        openrouter_key=os.getenv("OPENROUTER_API_KEY", ""),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "openrouter/free"),
        excluded_sender_ids=frozenset(x.strip() for x in os.getenv("EXCLUDED_SENDER_IDS", "").split(",") if x.strip()),
    )
