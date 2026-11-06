"""SQLite persistence."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
    permissions TEXT NOT NULL DEFAULT '[]',
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
    "require_approval": True,
    "fallback_playlist_id": None,
    "fallback_playlist_name": None,
    "fallback_shuffle": True,
    "crossfade_mode": "smart",  # smart | standard | off
    "crossfade_duration": 8,
    "max_pending_per_guest": 3,
    "request_cooldown_seconds": 20,
    "party_name": "Party",
    # TV screen copy; "" means the built-in default text.
    "tv_cta_text": "",
    "tv_paused_text": "",
    "tv_empty_text": "",
}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        # One connection is shared by every coroutine, so a commit from one would also
        # commit another's half-done multi-statement write. Writes take this lock;
        # transaction() holds it for the whole block.
        self._write_lock = asyncio.Lock()
        self._tx_owner: asyncio.Task | None = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self) -> None:
        cols = {r["name"] for r in await self.fetchall("PRAGMA table_info(users)")}
        if "permissions" not in cols:
            # v1 -> v2: individual permissions replace can_approve / can_manage_queue
            await self.c.execute("ALTER TABLE users ADD COLUMN permissions TEXT NOT NULL DEFAULT '[]'")
            for row in await self.fetchall("SELECT id, can_approve, can_manage_queue FROM users"):
                perms: list[str] = []
                if row["can_approve"]:
                    perms += ["approve_requests"]
                if row["can_manage_queue"]:
                    perms += ["queue_add", "queue_reorder", "queue_remove", "playback_skip", "playback_pause"]
                await self.c.execute(
                    "UPDATE users SET permissions=? WHERE id=?", (json.dumps(sorted(perms)), row["id"])
                )

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

    def _in_transaction(self) -> bool:
        return self._tx_owner is not None and self._tx_owner is asyncio.current_task()

    async def execute(self, sql: str, *args: Any) -> int:
        """Run one write statement (committed, unless inside transaction())."""
        if self._in_transaction():
            async with self.c.execute(sql, args) as cur:
                return cur.lastrowid or 0
        async with self._write_lock:
            async with self.c.execute(sql, args) as cur:
                last = cur.lastrowid
            await self.c.commit()
        return last or 0

    async def execute_rowcount(self, sql: str, *args: Any) -> int:
        """Like execute() but returns the number of rows changed."""
        if self._in_transaction():
            async with self.c.execute(sql, args) as cur:
                return cur.rowcount
        async with self._write_lock:
            async with self.c.execute(sql, args) as cur:
                n = cur.rowcount
            await self.c.commit()
        return n

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Database]:
        """Atomic multi-statement write: BEGIN ... COMMIT, ROLLBACK on error.

        Use execute() (or self.c.execute) inside the block; other coroutines' writes
        wait until it ends. Not re-entrant across tasks; nested use in the same task
        joins the outer transaction.
        """
        if self._in_transaction():
            yield self
            return
        async with self._write_lock:
            self._tx_owner = asyncio.current_task()
            try:
                await self.c.execute("BEGIN IMMEDIATE")
                try:
                    yield self
                except BaseException:
                    await self.c.rollback()
                    raise
                await self.c.commit()
            finally:
                self._tx_owner = None

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
        async with self.transaction():
            for key, value in values.items():
                if key not in DEFAULT_SETTINGS:
                    continue
                await self.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    key,
                    json.dumps(value),
                )
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
