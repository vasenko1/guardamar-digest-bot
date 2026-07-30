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
  sender_id TEXT,
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
  classification_run_id TEXT,
  manual_title TEXT,
  manual_category TEXT,
  excluded_reason TEXT,
  duplicate_of INTEGER REFERENCES messages(id),
  dedupe_reason TEXT,
  dedupe_confidence REAL,
  dedupe_version TEXT,
  needs_duplicate_review INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS publications (
  period_key TEXT NOT NULL,
  destination TEXT NOT NULL,
  part_no INTEGER NOT NULL,
  telegram_message_id INTEGER,
  rendered_html TEXT NOT NULL,
  sent_at TEXT,
  PRIMARY KEY(period_key, destination, part_no)
);
CREATE TABLE IF NOT EXISTS classification_runs (
  period_key TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  input_signature TEXT NOT NULL,
  categories_json TEXT NOT NULL,
  status TEXT NOT NULL,
  lock_until TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS workflow_runs (
  period_key TEXT NOT NULL,
  stage TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  status TEXT NOT NULL,
  details TEXT,
  completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(period_key, stage)
);
CREATE TABLE IF NOT EXISTS duplicate_reviews (
  period_key TEXT NOT NULL,
  left_message_id INTEGER NOT NULL REFERENCES messages(id),
  right_message_id INTEGER NOT NULL REFERENCES messages(id),
  lexical_score REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  confidence TEXT,
  provider TEXT,
  reason_code TEXT,
  reason_detail TEXT,
  rule_version TEXT,
  decided_at TEXT,
  left_fingerprint TEXT,
  right_fingerprint TEXT,
  PRIMARY KEY(period_key, left_message_id, right_message_id)
);
CREATE TABLE IF NOT EXISTS editorial_audit (
  id INTEGER PRIMARY KEY,
  period_key TEXT NOT NULL,
  message_id INTEGER NOT NULL REFERENCES messages(id),
  stage TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  reason_detail TEXT,
  confidence TEXT NOT NULL,
  provider TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(period_key, message_id, stage, rule_version)
);
CREATE TABLE IF NOT EXISTS dedupe_topics (
  period_key TEXT NOT NULL,
  message_id INTEGER NOT NULL REFERENCES messages(id),
  intent TEXT NOT NULL,
  offer_key TEXT NOT NULL,
  confidence TEXT NOT NULL,
  provider TEXT NOT NULL,
  text_fingerprint TEXT,
  rule_version TEXT,
  PRIMARY KEY(period_key, message_id)
);
"""


def _add_column_if_missing(con: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def migrate(con: sqlite3.Connection) -> None:
    # SQLite CREATE TABLE does not update an existing table. These migrations make
    # phone databases created by earlier MVP versions compatible without data loss.
    _add_column_if_missing(con, "messages", "sender_id TEXT")
    _add_column_if_missing(con, "entries", "duplicate_of INTEGER")
    _add_column_if_missing(con, "entries", "dedupe_reason TEXT")
    _add_column_if_missing(con, "entries", "dedupe_confidence REAL")
    _add_column_if_missing(con, "entries", "dedupe_version TEXT")
    _add_column_if_missing(con, "entries", "needs_duplicate_review INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(con, "entries", "classification_run_id TEXT")
    _add_column_if_missing(con, "dedupe_topics", "text_fingerprint TEXT")
    _add_column_if_missing(con, "dedupe_topics", "rule_version TEXT")
    _add_column_if_missing(con, "duplicate_reviews", "left_fingerprint TEXT")
    _add_column_if_missing(con, "duplicate_reviews", "right_fingerprint TEXT")
    _add_column_if_missing(con, "duplicate_reviews", "reason_code TEXT")
    _add_column_if_missing(con, "duplicate_reviews", "reason_detail TEXT")
    _add_column_if_missing(con, "duplicate_reviews", "rule_version TEXT")
    _add_column_if_missing(con, "duplicate_reviews", "decided_at TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS entries_period ON entries(period_key)")
    con.execute("CREATE INDEX IF NOT EXISTS messages_sender_period ON messages(sender_id, published_at)")
    con.execute("CREATE INDEX IF NOT EXISTS entries_duplicate_review ON entries(period_key, needs_duplicate_review)")
    con.execute("CREATE INDEX IF NOT EXISTS duplicate_reviews_status ON duplicate_reviews(period_key, status)")
    con.execute("CREATE INDEX IF NOT EXISTS dedupe_topics_offer ON dedupe_topics(period_key, offer_key)")
    con.execute("CREATE INDEX IF NOT EXISTS editorial_audit_period ON editorial_audit(period_key, stage, decision)")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        migrate(con)
        yield con
        con.commit()
    finally:
        con.close()
