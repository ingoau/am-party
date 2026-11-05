"""SQLite persistence."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub TEXT NOT NULL UNIQUE,
    email TEXT,
    name TEXT,
    picture TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    can_approve INTEGER NOT NULL DEFAULT 0,
    can_manage_queue INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_login REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL,
    track TEXT NOT NULL,
    guest_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    decided_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);

CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position REAL NOT NULL,
    track_id TEXT NOT NULL,
    track TEXT NOT NULL,
    source TEXT NOT NULL,
    request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL,
    track TEXT NOT NULL,
    source TEXT NOT NULL,
    played_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis (
    track_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "requests_open": True,
    "fallback_playlist_id": None,
    "fallback_playlist_name": None,
    "fallback_shuffle": True,
    "crossfade_mode": "smart",  # smart | standard | off
    "crossfade_duration": 8,
    "max_pending_per_guest": 3,
    "request_cooldown_seconds": 20,
    "party_name": "Encore",
}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    @property
    def c(self) -> aiosqlite.Connection:
        assert self.conn is not None
        return self.conn

    async def fetchone(self, sql: str, *args: Any) -> dict[str, Any] | None:
        async with self.c.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        async with self.c.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def execute(self, sql: str, *args: Any) -> int:
        async with self.c.execute(sql, args) as cur:
            last = cur.lastrowid
        await self.c.commit()
        return last or 0

    # settings -----------------------------------------------------------
    async def get_settings(self) -> dict[str, Any]:
        rows = await self.fetchall("SELECT key, value FROM settings")
        out = dict(DEFAULT_SETTINGS)
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                continue
        return out

    async def set_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                continue
            await self.c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
        await self.c.commit()
        return await self.get_settings()


    # credentials ------------------------------------------------------------
    async def get_credential(self, key: str) -> tuple[str, float] | None:
        row = await self.fetchone("SELECT value, updated_at FROM credentials WHERE key=?", key)
        return (row["value"], row["updated_at"]) if row else None

    async def set_credential(self, key: str, value: str | None) -> None:
        if value:
            await self.execute(
                "INSERT INTO credentials(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                key,
                value,
                now(),
            )
        else:
            await self.execute("DELETE FROM credentials WHERE key=?", key)


def now() -> float:
    return time.time()
