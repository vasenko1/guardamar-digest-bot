from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  chat_id TEXT NOT NULL,
  message_id INTEGER NOT NULL,
  published_at TEXT NOT NULL,
  sender_name TEXT,
  source_text TEXT NOT NULL,
  source_url TEXT NOT NULL,
  media_type TEXT,
  media_group_id TEXT,
  imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS entries (
  message_id INTEGER PRIMARY KEY REFERENCES messages(id),
  period_key TEXT NOT NULL,
  eligible INTEGER NOT NULL DEFAULT 1,
  category_code TEXT,
  category_title TEXT,
  category_emoji TEXT,
  short_title TEXT,
  confidence TEXT,
  provider TEXT,
  manual_title TEXT,
  manual_category TEXT,
  excluded_reason TEXT
);
CREATE INDEX IF NOT EXISTS entries_period ON entries(period_key);
CREATE TABLE IF NOT EXISTS publications (
  period_key TEXT NOT NULL,
  destination TEXT NOT NULL,
  part_no INTEGER NOT NULL,
  telegram_message_id INTEGER,
  rendered_html TEXT NOT NULL,
  sent_at TEXT,
  PRIMARY KEY(period_key, destination, part_no)
);
"""


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()
