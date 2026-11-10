"""SQLite storage. One connection, guarded by a lock. Volume is a few requests per minute at most."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT NOT NULL DEFAULT '',
    password_hash TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    hook_token_hash TEXT UNIQUE,
    hook_token_hint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    match_type TEXT NOT NULL,
    pattern TEXT NOT NULL DEFAULT '',
    strip_trigger INTEGER NOT NULL DEFAULT 1,
    stop INTEGER NOT NULL DEFAULT 1,
    action_type TEXT NOT NULL,
    action_config TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'ring',
    run_rules INTEGER NOT NULL DEFAULT 1,
    received_at REAL NOT NULL,
    recorded_at REAL,
    audio_path TEXT,
    audio_mime TEXT,
    ring_transcript TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL DEFAULT 0,
    transcript TEXT NOT NULL DEFAULT '',
    transcript_source TEXT NOT NULL DEFAULT '',
    needs_review INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS recordings_user ON recordings(user_id, id DESC);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    rule_name TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_config TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS deliveries_rec ON deliveries(recording_id);
"""


class DB:
    def __init__(self, data_dir: str | os.PathLike):
        self.data_dir = Path(data_dir)
        self.audio_dir = self.data_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
        path = self.data_dir / "indexhook.sqlite3"
        self._lock = threading.RLock()
        self._c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA foreign_keys=ON")
        self._c.executescript(SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def close(self):
        with self._lock:
            self._c.close()

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self._lock:
            return self._c.execute(sql, args).fetchall()

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        with self._lock:
            return self._c.execute(sql, args).fetchone()

    def run(self, sql: str, *args) -> int:
        """Execute a write. Returns lastrowid for INSERT, rowcount otherwise."""
        with self._lock:
            cur = self._c.execute(sql, args)
            return cur.lastrowid if sql.lstrip()[:6].upper() == "INSERT" else cur.rowcount

    # settings ---------------------------------------------------------
    def get_setting(self, scope: str, key: str) -> str | None:
        row = self.one("SELECT value FROM settings WHERE scope=? AND key=?", scope, key)
        return row["value"] if row else None

    def set_setting(self, scope: str, key: str, value: str | None):
        if value is None:
            self.run("DELETE FROM settings WHERE scope=? AND key=?", scope, key)
        else:
            self.run(
                "INSERT INTO settings(scope,key,value) VALUES(?,?,?) "
                "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value",
                scope, key, value,
            )

    def scope_settings(self, scope: str) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.q("SELECT key,value FROM settings WHERE scope=?", scope)}


def now() -> float:
    return time.time()


def loads(s: str | None, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default
