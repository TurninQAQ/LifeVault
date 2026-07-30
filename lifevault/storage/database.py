from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS life_records (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    amount REAL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    event_date TEXT,
    deadline TEXT,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    details_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    source_text_hash TEXT,
    source_text_preview TEXT,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_life_records_type ON life_records(user_id, type);
CREATE INDEX IF NOT EXISTS idx_life_records_status ON life_records(user_id, status);
CREATE INDEX IF NOT EXISTS idx_life_records_deadline ON life_records(user_id, deadline);

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES life_records(id),
    user_id TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    reminder_type TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_id TEXT REFERENCES reminders(id),
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(user_id, idempotency_key),
    UNIQUE(record_id, reminder_type, scheduled_at)
);

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(user_id, status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_reminders_record ON reminders(record_id);

CREATE TABLE IF NOT EXISTS reminder_batches (
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    reminder_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id TEXT PRIMARY KEY,
    default_time TEXT NOT NULL,
    quiet_hours_start TEXT,
    quiet_hours_end TEXT,
    default_advance_days INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_checkpoints (
    thread_id TEXT PRIMARY KEY,
    checkpoint_data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT,
    result TEXT NOT NULL,
    params_summary TEXT,
    created_at TEXT NOT NULL
);
"""


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(database_path: Path) -> None:
    with connect(database_path) as conn:
        conn.executescript(SCHEMA)
