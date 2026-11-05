"""Request approval flow: manual, approve-all, and approval disabled."""

from __future__ import annotations

from pathlib import Path

import pytest

from encore.db import Database
from encore.models import Track
from encore.party import Hub, Party


async def make_party(tmp_path: Path) -> Party:
    db = Database(tmp_path / "p.db")
    await db.open()
    party = Party(db, apple=None, hub=Hub())
    await party.load()
    await party.update_settings({"request_cooldown_seconds": 0, "max_pending_per_guest": 10})
    for i in range(5):
        party._cache_track(Track(id=str(i), title=f"Song {i}", artist="A"))  # noqa: SLF001
    return party


@pytest.mark.asyncio
async def test_approve_all_keeps_order(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    for i in range(3):
        await p.create_request(f"guest{i}", str(i))
    assert [q["track"].id for q in p.queue] == []
    assert await p.approve_all(None) == 3
    assert [q["track"].id for q in p.queue] == ["0", "1", "2"]
    assert await p.approve_all(None) == 0


@pytest.mark.asyncio
async def test_approve_all_play_next_keeps_order(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    await p.create_request("g", "4")
    await p.approve_all(None)
    for i in range(3):
        await p.create_request(f"guest{i}", str(i))
    await p.approve_all(None, "next")
    assert [q["track"].id for q in p.queue] == ["0", "1", "2", "4"]


@pytest.mark.asyncio
async def test_disabling_approval(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    await p.create_request("g1", "0")
    approved = await p.set_require_approval(False, None)
    assert approved == 1 and [q["track"].id for q in p.queue] == ["0"]
    req = await p.create_request("g2", "1")
    assert req["status"] == "approved"
    assert [q["track"].id for q in p.queue] == ["0", "1"]
    assert p.state()["require_approval"] is False
    await p.set_require_approval(True, None)
    req = await p.create_request("g3", "2")
    assert req["status"] == "pending"


@pytest.mark.asyncio
async def test_withdraw_and_positions(tmp_path: Path) -> None:
    from encore.party import ApiError

    p = await make_party(tmp_path)
    await p.create_request("g1", "0")
    await p.create_request("g2", "1")
    await p.create_request("g1", "2")
    mine = await p.my_requests("g1")
    assert all(r["can_withdraw"] for r in mine)
    await p.withdraw_request("g1", mine[0]["id"])  # newest first -> song 2
    with pytest.raises(ApiError):
        await p.withdraw_request("g2", mine[1]["id"])  # someone else's
    await p.approve_all(None)
    mine = await p.my_requests("g1")
    by_status = {r["status"]: r for r in mine}
    assert by_status["withdrawn"]["can_withdraw"] is False
    approved = by_status["approved"]
    assert approved["queue_position"] == 1 and approved["eta_seconds"] == 0
    assert [u["request_id"] for u in p.state()["upcoming"]] == [approved["id"], approved["id"] + 1]
    await p.withdraw_request("g1", approved["id"])
    assert [q["track"].id for q in p.queue] == ["1"]
