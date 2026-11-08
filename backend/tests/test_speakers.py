"""Claimable speakers: identity, sync on reconnect, control, locking, owners and shares."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from am_party import speakers as speakers_mod
from am_party.auth import User
from am_party.db import Database
from am_party.party import ApiError
from am_party.speakers import SpeakerManager

CID_A = "A" * 43
CID_B = "B" * 43


class FakeHub:
    def __init__(self) -> None:
        self.sent: list[tuple[dict[str, Any], str | None]] = []

    async def send_all(self, msg: dict[str, Any], permission: str | None = None) -> None:
        self.sent.append((msg, permission))


class FakeOutput:
    def __init__(self) -> None:
        # client_id -> [volume, muted, settable]
        self.players: dict[str, list[Any]] = {}
        self.commands: list[tuple[str, int | None, bool | None]] = []

    def connected_player_ids(self) -> set[str]:
        return set(self.players)

    def client_player_state(self, client_id: str) -> tuple[int, bool, bool] | None:
        p = self.players.get(client_id)
        return (p[0], p[1], p[2]) if p else None

    def set_client_volume(self, client_id: str, volume: int | None = None, muted: bool | None = None) -> bool:
        p = self.players.get(client_id)
        if p is None:
            return False
        self.commands.append((client_id, volume, muted))
        if volume is not None:
            p[0] = volume
        if muted is not None:
            p[1] = muted
        return True


def user(uid: int, *, admin: bool = False, perms: tuple[str, ...] = ("claim_speakers",)) -> User:
    return User(id=uid, sub=f"u{uid}", email=None, name=f"User {uid}", picture=None, is_admin=admin, granted=list(perms))


@pytest_asyncio.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[SpeakerManager, FakeOutput, FakeHub]]:
    monkeypatch.setattr(speakers_mod, "SYNC_POLL", 0.01)
    monkeypatch.setattr(speakers_mod, "SYNC_TIMEOUT", 0.3)
    monkeypatch.setattr(speakers_mod, "NOTIFY_DELAY", 0.0)
    db = Database(tmp_path / "s.db")
    await db.open()
    for uid in (1, 2, 3, 9):
        await db.execute(
            "INSERT INTO users(id, sub, name, is_admin, created_at, permissions) VALUES(?,?,?,?,0,?)",
            uid, f"u{uid}", f"User {uid}", int(uid == 9), json.dumps(["claim_speakers"]),
        )
    out, hub = FakeOutput(), FakeHub()
    mgr = SpeakerManager(db, hub, out)  # type: ignore[arg-type]
    yield mgr, out, hub
    await mgr.close()
    await db.close()


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0.02)


async def connect(mgr: SpeakerManager, out: FakeOutput, cid: str, name: str = "Kitchen", vol: int = 50, muted: bool = False) -> dict[str, Any]:
    out.players[cid] = [vol, muted, True]
    mgr.on_client_joined(cid, name)
    await settle()
    return await mgr.device_view(cid)


async def disconnect(mgr: SpeakerManager, out: FakeOutput, cid: str) -> None:
    out.players.pop(cid, None)
    mgr.on_client_left(cid)
    await settle()


@pytest.mark.asyncio
async def test_identity_and_claim(env: Any) -> None:
    mgr, out, hub = env
    with pytest.raises(ApiError) as err:
        await mgr.device_view(CID_A)
    assert err.value.status == 404

    me = await connect(mgr, out, CID_A, "Kitchen")
    assert me["name"] == "Kitchen" and not me["named"] and not me["claimed"] and me["connected"]
    token = me["claim_token"]
    assert token and CID_A not in json.dumps(await mgr.list_for(user(9, admin=True)))

    # stable id across reconnects; the claim token rotates
    await disconnect(mgr, out, CID_A)
    assert (await mgr.device_view(CID_A))["claim_token"] is None
    again = await connect(mgr, out, CID_A, "Kitchen")
    assert again["id"] == me["id"] and again["claim_token"] != token
    with pytest.raises(ApiError):
        await mgr.claim(user(1), token)

    preview = await mgr.claim_preview(user(1), again["claim_token"])
    assert preview == {"speaker": {"id": me["id"], "name": "Kitchen", "connected": True}, "already_mine": False}
    view = await mgr.claim(user(1), again["claim_token"])
    assert view["mine"] and [o["id"] for o in view["owners"]] == [1]
    # option A: anyone with the permission who scans joins as a co-owner
    await mgr.claim(user(2), again["claim_token"])
    assert (await mgr.device_view(CID_A))["claimed"]
    assert [s["id"] for s in await mgr.list_for(user(1))] == [me["id"]]
    assert await mgr.list_for(user(3)) == []
    assert len(await mgr.list_for(user(9, admin=True))) == 1
    assert ({"type": "speakers_changed"}, "claim_speakers") in hub.sent
    assert ({"type": "speaker_changed", "id": me["id"]}, None) in hub.sent


@pytest.mark.asyncio
async def test_volume_restored_on_reconnect_and_last_change_wins(env: Any) -> None:
    mgr, out, _ = env
    me = await connect(mgr, out, CID_A, vol=40)
    sid = me["id"]
    # a new speaker adopts its own values
    view = (await mgr.list_for(user(9, admin=True)))[0]
    assert (view["volume"], view["muted"]) == (40, False)

    # local change on the device wins
    mgr.on_client_volume(CID_A, 70, True)
    await settle()
    view = (await mgr.list_for(user(9, admin=True)))[0]
    assert (view["volume"], view["muted"]) == (70, True)

    # reconnect with different local values: the stored ones are pushed back
    await disconnect(mgr, out, CID_A)
    out.commands.clear()
    await connect(mgr, out, CID_A, vol=100, muted=False)
    assert out.commands == [(CID_A, 70, True)]
    assert out.players[CID_A][:2] == [70, True]
    assert (await mgr.device_view(CID_A))["id"] == sid


@pytest.mark.asyncio
async def test_sync_waits_until_commands_are_accepted(env: Any) -> None:
    mgr, out, _ = env
    await connect(mgr, out, CID_A, vol=30)
    await disconnect(mgr, out, CID_A)
    out.players[CID_A] = [100, False, False]  # same mute: only the volume is commanded  # client/state not received yet
    mgr.on_client_joined(CID_A, "Kitchen")
    await settle()
    # pre-sync reports (the device's defaults) are ignored
    mgr.on_client_volume(CID_A, 100, False)
    await settle()
    assert out.commands == []
    out.players[CID_A][2] = True
    await settle()
    assert out.commands == [(CID_A, 30, None)]
    assert (await mgr.list_for(user(9, admin=True)))[0]["volume"] == 30


@pytest.mark.asyncio
async def test_locked_speaker_resists_local_changes(env: Any) -> None:
    mgr, out, _ = env
    me = await connect(mgr, out, CID_A, vol=50)
    await mgr.claim(user(1), me["claim_token"])
    await mgr.update(user(1), me["id"], {"locked": True})
    assert (await mgr.device_view(CID_A))["locked"]
    # a locked speaker can't rename itself by announcing another name either
    await disconnect(mgr, out, CID_A)
    await connect(mgr, out, CID_A, "Renamed locally", vol=50)
    assert (await mgr.device_view(CID_A))["name"] == "Kitchen"
    out.commands.clear()
    mgr.on_client_volume(CID_A, 90, True)
    await settle()
    assert out.commands == [(CID_A, 50, False)]
    assert (await mgr.list_for(user(1)))[0]["volume"] == 50
    out.commands.clear()
    mgr.on_client_volume(CID_A, 50, True)  # only the mute differs
    await settle()
    assert out.commands == [(CID_A, None, False)]
    with pytest.raises(ApiError) as err:
        await mgr.device_rename(CID_A, "Mine now")
    assert err.value.status == 423
    await mgr.update(user(1), me["id"], {"locked": False})
    renamed = await mgr.device_rename(CID_A, "  Back   yard ")
    assert renamed["name"] == "Back yard" and renamed["named"]
    # a deliberately chosen name sticks even if the device announces another one
    await disconnect(mgr, out, CID_A)
    assert (await connect(mgr, out, CID_A, "TV 1234"))["name"] == "Back yard"


@pytest.mark.asyncio
async def test_bulk_control(env: Any) -> None:
    mgr, out, _ = env
    a = await connect(mgr, out, CID_A, "A", vol=20)
    b = await connect(mgr, out, CID_B, "B", vol=60)
    for s in (a, b):
        await mgr.claim(user(1), s["claim_token"])
    ids = [a["id"], b["id"]]

    res = await mgr.control(user(1), {"ids": ids, "volume_delta": 10})
    assert {s["name"]: s["volume"] for s in res} == {"A": 30, "B": 70}
    assert out.players[CID_A][0] == 30 and out.players[CID_B][0] == 70
    res = await mgr.control(user(1), {"ids": ids, "volume": 45})
    assert {s["volume"] for s in res} == {45}
    res = await mgr.control(user(1), {"ids": ids, "volume_delta": 100})
    assert {s["volume"] for s in res} == {100}
    out.commands.clear()
    res = await mgr.control(user(1), {"ids": ids, "muted": True})
    assert all(s["muted"] for s in res)
    assert sorted(out.commands) == [(CID_A, None, True), (CID_B, None, True)]
    res = await mgr.control(user(1), {"ids": ids, "locked": True})
    assert all(s["locked"] for s in res)

    with pytest.raises(ApiError) as err:
        await mgr.control(user(2), {"ids": ids, "muted": False})
    assert err.value.status == 403
    with pytest.raises(ApiError):
        await mgr.control(user(1), {"ids": ids})
    with pytest.raises(ApiError):
        await mgr.control(user(1), {"ids": ids, "volume": 101})
    with pytest.raises(ApiError):
        await mgr.control(user(1), {"ids": ids, "volume": True})
    # admins control everything
    res = await mgr.control(user(9, admin=True), {"ids": ids, "muted": False})
    assert not any(s["muted"] for s in res)

    # offline speakers keep the change and get it when they come back
    await disconnect(mgr, out, CID_A)
    await mgr.control(user(1), {"ids": [a["id"]], "volume": 12, "muted": True})
    out.commands.clear()
    await connect(mgr, out, CID_A, vol=99)
    assert out.commands == [(CID_A, 12, True)]
    out.commands.clear()
    await disconnect(mgr, out, CID_A)
    await connect(mgr, out, CID_A, vol=12, muted=False)
    assert out.commands == [(CID_A, None, True)]


@pytest.mark.asyncio
async def test_owners_are_equal(env: Any) -> None:
    mgr, out, _ = env
    me = await connect(mgr, out, CID_A)
    for uid in (1, 2, 3):
        await mgr.claim(user(uid), me["claim_token"])
    # any co-owner can remove any other, including whoever claimed first
    view = await mgr.remove_owner(user(3), me["id"], 1)
    assert view is not None and [o["id"] for o in view["owners"]] == [2, 3]
    assert await mgr.remove_owner(user(3), me["id"], 3) is None  # leave
    with pytest.raises(ApiError) as err:
        await mgr.remove_owner(user(3), me["id"], 2)
    assert err.value.status == 403
    # admins can remove anyone
    view = await mgr.remove_owner(user(9, admin=True), me["id"], 2)
    assert view is not None and view["owners"] == [] and not (await mgr.device_view(CID_A))["claimed"]


@pytest.mark.asyncio
async def test_share_and_handover(env: Any) -> None:
    mgr, out, _ = env
    a = await connect(mgr, out, CID_A, "A")
    b = await connect(mgr, out, CID_B, "B")
    for s in (a, b):
        await mgr.claim(user(1), s["claim_token"])
    ids = [a["id"], b["id"]]

    with pytest.raises(ApiError) as err:
        await mgr.create_share(user(2), {"ids": ids})
    assert err.value.status == 403

    share = await mgr.create_share(user(1), {"ids": ids, "mode": "share"})
    preview = await mgr.share_preview(share["token"])
    assert preview["from"] == "User 1" and [s["name"] for s in preview["speakers"]] == ["A", "B"]
    got = await mgr.accept_share(user(2), share["token"])
    assert sorted(s["name"] for s in got) == ["A", "B"]
    assert all({o["id"] for o in s["owners"]} == {1, 2} for s in got)
    with pytest.raises(ApiError):  # single use
        await mgr.accept_share(user(3), share["token"])

    handover = await mgr.create_share(user(2), {"ids": [a["id"]], "mode": "handover"})
    got = await mgr.accept_share(user(3), handover["token"])
    assert {o["id"] for o in got[0]["owners"]} == {1, 3}
    assert [s["name"] for s in await mgr.list_for(user(2))] == ["B"]

    expired = await mgr.create_share(user(1), {"ids": ids})
    mgr._shares[expired["token"]].expires_at = 0
    with pytest.raises(ApiError):
        await mgr.share_preview(expired["token"])
    with pytest.raises(ApiError):
        await mgr.create_share(user(1), {"ids": ids, "mode": "steal"})


@pytest.mark.asyncio
async def test_forget_and_prune(env: Any) -> None:
    mgr, out, _ = env
    a = await connect(mgr, out, CID_A, "A")
    b = await connect(mgr, out, CID_B, "B")
    await mgr.claim(user(1), b["claim_token"])
    admin = user(9, admin=True)
    with pytest.raises(ApiError) as err:
        await mgr.forget(admin, a["id"])
    assert err.value.status == 409
    with pytest.raises(ApiError):
        await mgr.forget(user(1), b["id"])
    await disconnect(mgr, out, CID_A)
    await disconnect(mgr, out, CID_B)
    await mgr.db.execute("UPDATE speakers SET last_seen=0")
    # only the unclaimed one goes
    assert await mgr.prune() == 1
    assert [s["name"] for s in await mgr.list_for(admin)] == ["B"]
    await mgr.forget(admin, b["id"])
    assert await mgr.list_for(admin) == []


@pytest.mark.asyncio
async def test_device_input_validation(env: Any) -> None:
    mgr, out, _ = env
    for bad in (None, 5, "short", "x" * 200, "has spaces in it ....."):
        with pytest.raises(ApiError) as err:
            await mgr.device_view(bad)
        assert err.value.status == 400
    await connect(mgr, out, CID_A)
    with pytest.raises(ApiError):
        await mgr.device_rename(CID_A, "   ")
    assert (await mgr.device_rename(CID_A, "x" * 100))["name"] == "x" * 40


@pytest.mark.asyncio
async def test_rejoin_without_disconnect_resyncs(env: Any) -> None:
    mgr, out, _ = env
    first = await connect(mgr, out, CID_A, vol=30)
    # the device reconnects before the old connection was noticed as gone: no "left" event
    out.players[CID_A] = [100, False, False]
    mgr.on_client_joined(CID_A, "Kitchen")
    await settle()
    again = await mgr.device_view(CID_A)
    assert again["claim_token"] != first["claim_token"]
    mgr.on_client_volume(CID_A, 100, False)  # the new connection's defaults: not a local change
    await settle()
    out.players[CID_A][2] = True
    await settle()
    assert out.commands == [(CID_A, 30, None)]
    assert (await mgr.list_for(user(9, admin=True)))[0]["volume"] == 30


@pytest.mark.asyncio
async def test_unsettable_defaults_are_not_adopted(env: Any) -> None:
    mgr, out, _ = env
    out.players[CID_A] = [100, False, False]  # never declares the commands
    mgr.on_client_joined(CID_A, "Kitchen")
    await asyncio.sleep(0.5)  # past SYNC_TIMEOUT
    assert (await mgr.list_for(user(9, admin=True)))[0]["volume"] is None


@pytest.mark.asyncio
async def test_share_needs_the_sharer_to_still_control_the_speakers(env: Any) -> None:
    mgr, out, _ = env
    me = await connect(mgr, out, CID_A)
    await mgr.claim(user(1), me["claim_token"])
    await mgr.claim(user(2), me["claim_token"])
    share = await mgr.create_share(user(1), {"ids": [me["id"]]})
    await mgr.remove_owner(user(2), me["id"], 1)  # user 1 is removed after making the code
    with pytest.raises(ApiError) as err:
        await mgr.share_preview(share["token"])
    assert err.value.status == 410
    with pytest.raises(ApiError) as err:
        await mgr.accept_share(user(1), share["token"])
    assert err.value.status == 410
    assert [o["id"] for o in (await mgr.list_for(user(2)))[0]["owners"]] == [2]
    # per-user cap on open codes
    for _ in range(speakers_mod.MAX_SHARES_PER_USER):
        await mgr.create_share(user(2), {"ids": [me["id"]]})
    with pytest.raises(ApiError) as err:
        await mgr.create_share(user(2), {"ids": [me["id"]]})
    assert err.value.status == 429


@pytest.mark.asyncio
async def test_unclaimed_speakers_are_capped(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr, out, _ = env
    monkeypatch.setattr(speakers_mod, "MAX_UNCLAIMED", 2)
    keep = await connect(mgr, out, CID_A, "Claimed")
    await mgr.claim(user(1), keep["claim_token"])
    await disconnect(mgr, out, CID_A)
    for i in range(4):
        cid = f"{i}" * 43
        await connect(mgr, out, cid, f"S{i}")
        await disconnect(mgr, out, cid)
    names = sorted(s["name"] for s in await mgr.list_for(user(9, admin=True)))
    assert names == ["Claimed", "S2", "S3"]
