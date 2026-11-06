"""Permission model, migration and bulk updates."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from am_party.api import _apply_user_changes
from am_party.auth import PERMISSIONS, User
from am_party.db import Database
from am_party.party import ApiError, Hub


class FakeAmParty:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.hub = Hub()

    async def refresh_ws_users(self) -> None:
        pass


OLD_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sub TEXT NOT NULL UNIQUE, email TEXT, name TEXT,
    picture TEXT, is_admin INTEGER NOT NULL DEFAULT 0, can_approve INTEGER NOT NULL DEFAULT 0,
    can_manage_queue INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, last_login REAL);
INSERT INTO users(sub, is_admin, can_approve, can_manage_queue, created_at) VALUES
    ('admin', 1, 1, 1, 0), ('approver', 0, 1, 0, 0), ('dj', 0, 0, 1, 0), ('nobody', 0, 0, 0, 0);
"""


async def users(db: Database) -> dict[str, User]:
    return {r["sub"]: User.from_row(r) for r in await db.fetchall("SELECT * FROM users")}


@pytest.mark.asyncio
async def test_migration_from_coarse_permissions(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.close()
    db = Database(path)
    await db.open()
    u = await users(db)
    assert u["admin"].is_admin and set(u["admin"].permissions) == set(PERMISSIONS)
    assert u["approver"].granted == ["approve_requests"]
    assert set(u["dj"].granted) == {
        "queue_add", "queue_reorder", "queue_remove", "playback_skip", "playback_pause",
    }
    assert u["nobody"].granted == [] and not u["nobody"].has_any()
    await db.close()
    # second open is a no-op
    db = Database(path)
    await db.open()
    assert (await users(db))["approver"].granted == ["approve_requests"]
    await db.close()


def test_user_has() -> None:
    row = {"id": 1, "sub": "x", "is_admin": 0, "permissions": json.dumps(["playback_skip", "bogus"])}
    u = User.from_row(row)
    assert u.granted == ["playback_skip"]
    assert u.has("playback_skip") and not u.has("queue_add") and not u.has("admin")
    admin = User.from_row({**row, "is_admin": 1})
    assert admin.has("queue_add") and admin.has("admin")


@pytest.mark.asyncio
async def test_bulk_add_remove_and_last_admin(tmp_path: Path) -> None:
    db = Database(tmp_path / "new.db")
    await db.open()
    for sub, admin in (("a", 1), ("b", 0), ("c", 0)):
        await db.execute(
            "INSERT INTO users(sub, is_admin, created_at) VALUES(?,?,0)", sub, admin
        )
    e = FakeAmParty(db)
    await _apply_user_changes(e, [2, 3], None, None, ["queue_add", "playback_skip", "nope"], [])
    u = await users(db)
    assert u["b"].granted == ["playback_skip", "queue_add"] == u["c"].granted
    await _apply_user_changes(e, [2, 3], None, None, [], ["playback_skip"])
    assert (await users(db))["c"].granted == ["queue_add"]
    await _apply_user_changes(e, [3], None, ["approve_requests"], [], [])
    assert (await users(db))["c"].granted == ["approve_requests"]
    with pytest.raises(ApiError):
        await _apply_user_changes(e, [1], False, None, [], [])
    await _apply_user_changes(e, [2], True, None, [], [])
    await _apply_user_changes(e, [1], False, None, [], [])  # another admin exists now
    u = await users(db)
    assert not u["a"].is_admin and u["b"].is_admin
    with pytest.raises(ApiError):
        await _apply_user_changes(e, [99], None, None, ["queue_add"], [])
    await db.close()
