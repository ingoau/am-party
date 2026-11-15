"""Audit log: what gets recorded, by whom, and how it is listed."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from am_party.api import _apply_user_changes
from am_party.audit import AuditLog
from am_party.auth import User
from am_party.db import Database
from am_party.models import Track
from am_party.party import Hub, Party


def staff(uid: int = 1, name: str = "Sam", admin: bool = False) -> User:
    return User(id=uid, sub=f"s{uid}", email=f"{name.lower()}@x", name=name, picture=None,
                is_admin=admin, granted=["approve_requests", "queue_add", "queue_remove", "queue_reorder"])


async def make_party(tmp_path: Path) -> Party:
    db = Database(tmp_path / "p.db")
    await db.open()
    for uid, name in ((1, "Sam"), (2, "Alex")):
        await db.execute(
            "INSERT INTO users(id, sub, email, name, created_at) VALUES(?,?,?,?,?)",
            uid, f"s{uid}", f"{name.lower()}@x", name, time.time(),
        )
    party = Party(db, apple=None, hub=Hub())
    await party.load()
    await party.update_settings({"request_rate_limits": []})
    for i in range(5):
        party._cache_track(Track(id=str(i), title=f"Song {i}", artist="A"))  # noqa: SLF001
    await party.db.execute("DELETE FROM audit_log")  # setup noise
    return party


async def entries(p: Party, **kw: object) -> list[dict]:
    return (await p.audit.list(**kw))["entries"]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_request_lifecycle_is_recorded(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    r1 = await p.create_request("guest-a", "0", ip="10.0.0.5")
    r2 = await p.create_request("guest-b", "1")
    await p.decide_request(r1["id"], staff(), True, "next")
    await p.decide_request(r2["id"], staff(2, "Alex"), False)
    log = await entries(p, include_ip=True)
    assert [e["action"] for e in log] == [
        "request.reject", "request.approve", "request.create", "request.create",
    ]
    reject, approve, _, create = log
    assert reject["actor"] == {"kind": "user", "id": 2, "name": "Alex", "guest": None}
    assert approve["actor"]["name"] == "Sam"
    assert approve["details"]["position"] == "next"
    assert approve["target"] == {"type": "request", "id": str(r1["id"]), "label": "A - Song 0"}
    assert create["actor"]["kind"] == "guest" and create["actor"]["guest"] == p.guest_label("guest-a")
    assert create["ip"] == "10.0.0.5"
    assert create["actor"]["name"] == p.guest_label("guest-a")
    # the rejection names the guest in its details too
    assert [e["action"] for e in await entries(p, q=p.guest_label("guest-b"))] == [
        "request.reject", "request.create",
    ]
    # IPs are left out unless asked for (non-admin viewers)
    assert "ip" not in (await entries(p))[0]


@pytest.mark.asyncio
async def test_queue_changes_are_recorded(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    sam = staff()
    await p.add_to_queue("2", "end", sam)
    await p.add_to_queue("3", "next", sam)
    await p.move_in_queue(p.queue[1]["id"], 0, sam)  # song 2 to the front
    await p.remove_from_queue(p.queue[1]["id"], staff(2, "Alex"))  # song 3
    log = await entries(p)
    assert [e["action"] for e in log] == ["queue.remove", "queue.move", "queue.add", "queue.add"]
    assert log[0]["target"]["label"] == "A - Song 3" and log[0]["actor"]["name"] == "Alex"
    assert log[1]["details"]["from"] == 1 and log[1]["details"]["to"] == 0
    assert log[3]["details"]["position"] == "end" and log[2]["details"]["position"] == "next"


@pytest.mark.asyncio
async def test_approve_all_and_require_approval(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    await p.create_request("g1", "0")
    await p.create_request("g2", "1")
    await p.set_require_approval(False, staff())
    log = await entries(p)
    assert [e["action"] for e in log[:2]] == ["settings.require_approval", "request.approve_all"]
    assert log[0]["details"] == {"enabled": False, "approved_waiting": 2}
    assert log[1]["details"]["count"] == 2
    assert [r["track"] for r in log[1]["details"]["requests"]] == ["A - Song 0", "A - Song 1"]
    # already off and nothing waiting: nothing to record
    await p.set_require_approval(False, staff())
    assert len(await entries(p)) == len(log)


@pytest.mark.asyncio
async def test_settings_diff_only_lists_changes(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    await p.update_settings({"party_name": "Bash", "crossfade_mode": "smart"}, staff())
    (entry,) = await entries(p)
    assert entry["action"] == "settings.update"
    assert entry["details"]["changes"] == {"party_name": {"from": "Party", "to": "Bash"}}
    await p.update_settings({"party_name": "Bash"}, staff())
    assert len(await entries(p)) == 1


@pytest.mark.asyncio
async def test_user_changes_report_what_changed(tmp_path: Path) -> None:
    db = Database(tmp_path / "u.db")
    await db.open()
    await db.execute(
        "INSERT INTO users(id, sub, name, is_admin, permissions, created_at) VALUES "
        "(1, 'a', 'Admin', 1, '[]', 0), (2, 'b', 'Bo', 0, '[\"queue_add\"]', 0)"
    )

    class FakeAmParty:
        def __init__(self) -> None:
            self.db = db
            self.hub = Hub()

        async def refresh_ws_users(self) -> None:
            pass

    changed = await _apply_user_changes(
        FakeAmParty(), [1, 2], None, None, ["playback_skip"], ["queue_add"]  # type: ignore[arg-type]
    )
    assert changed == [
        {"id": 1, "name": "Admin", "granted": ["playback_skip"]},
        {"id": 2, "name": "Bo", "granted": ["playback_skip"], "revoked": ["queue_add"]},
    ]
    assert await _apply_user_changes(
        FakeAmParty(), [2], None, None, ["playback_skip"], []  # type: ignore[arg-type]
    ) == []
    await db.close()


@pytest.mark.asyncio
async def test_filters_paging_and_prune(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    await db.open()
    audit = AuditLog(db)
    for i in range(5):
        await audit.record("queue.add", actor_kind="system", target_label=f"Song {i}")
    await audit.record("player.skip", actor_kind="system", target_label="Yanito_%")
    page = await audit.list(limit=4)
    assert len(page["entries"]) == 4 and page["next_before"] is not None
    rest = await audit.list(limit=4, before=page["next_before"])
    assert len(rest["entries"]) == 2 and rest["next_before"] is None
    assert [e["action"] for e in (await audit.list(action="player."))["entries"]] == ["player.skip"]
    assert (await audit.list(action="player"))["entries"] == []  # exact unless it ends in "."
    # LIKE wildcards in the search are literal
    assert [e["target"]["label"] for e in (await audit.list(q="o_%"))["entries"]] == ["Yanito_%"]
    assert len((await audit.list(q="song"))["entries"]) == 5
    await db.execute("UPDATE audit_log SET at = at - 400 * 86400 WHERE target_label='Song 0'")
    assert await audit.prune() == 1
    await db.close()


@pytest.mark.asyncio
async def test_actor_name_survives_user_deletion(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    await p.add_to_queue("0", "end", staff(2, "Alex"))
    await p.db.execute("DELETE FROM users WHERE id=2")
    (entry,) = await entries(p)
    assert entry["actor"]["id"] is None and entry["actor"]["name"] == "Alex"


def test_large_details_keep_their_scalars() -> None:
    import json

    from am_party.audit import MAX_DETAILS, MAX_LIST, _details_json

    reqs = [{"id": i, "track": f"Artist {i} - " + "x" * 80} for i in range(150)]
    out = json.loads(_details_json({"count": 150, "position": "next", "requests": reqs}))
    assert out["count"] == 150 and out["position"] == "next"
    assert len(out["requests"]) == MAX_LIST and out["requests_more"] == 150 - MAX_LIST
    # long strings are cut, and something still huge falls back to its scalars
    out = json.loads(_details_json({"count": 1, "blob": {"a": "y" * 50_000, "b": "z" * 50_000}}))
    assert out == {"count": 1, "truncated": True} or len(json.dumps(out)) <= MAX_DETAILS


@pytest.mark.asyncio
async def test_emails_are_for_admins_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "e.db")
    await db.open()
    audit = AuditLog(db)
    await audit.record("auth.login_denied", actor_kind="guest", actor_name="eve@evil.example",
                       details={"reason": "x"})
    await audit.record("user.delete", actor_kind="system", details={"email": "bo@x", "was_admin": False})
    staff_view = (await audit.list())["entries"]
    assert staff_view[1]["actor"]["name"] == "…@evil.example"
    assert staff_view[0]["details"] == {"was_admin": False}
    admin_view = (await audit.list(include_ip=True))["entries"]
    assert admin_view[1]["actor"]["name"] == "eve@evil.example"
    assert admin_view[0]["details"]["email"] == "bo@x"
    await db.close()
