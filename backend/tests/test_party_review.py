"""Regression tests for party / db / API review findings."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from am_party.db import Database
from am_party.models import Track
from am_party.party import ApiError, Hub, Party


async def make_party(tmp_path: Path) -> Party:
    db = Database(tmp_path / "p.db")
    await db.open()
    party = Party(db, apple=None, hub=Hub())
    await party.load()
    await party.update_settings({"request_cooldown_seconds": 0, "max_pending_per_guest": 10})
    for i in range(5):
        party._cache_track(Track(id=str(i), title=f"Song {i}", artist="A"))  # noqa: SLF001
    return party


# -- settings validation --------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"crossfade_duration": "abc"},
        {"crossfade_duration": "8"},
        {"crossfade_duration": float("inf")},
        {"crossfade_duration": 1e309},
        {"crossfade_duration": 0},
        {"crossfade_duration": 99},
        {"request_cooldown_seconds": None},
        {"request_cooldown_seconds": -1},
        {"max_pending_per_guest": 0},
        {"max_pending_per_guest": True},
        {"requests_open": "false"},
        {"require_approval": 0},
        {"party_name": 42},
        {"fallback_playlist_id": ["x"]},
        {"crossfade_mode": "loud"},
    ],
)
async def test_bad_settings_rejected(tmp_path: Path, patch: dict[str, Any]) -> None:
    p = await make_party(tmp_path)
    before = dict(p.settings())
    with pytest.raises(ApiError) as exc:
        await p.update_settings(patch)
    assert exc.value.status == 400
    assert p.settings() == before
    await p.db.close()


@pytest.mark.asyncio
async def test_good_settings(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    s = await p.update_settings(
        {"crossfade_duration": 12, "requests_open": False, "party_name": None, "max_pending_per_guest": 1}
    )
    assert s["crossfade_duration"] == 12 and s["requests_open"] is False
    assert s["party_name"] == "Party"  # null -> default, not "None"
    assert s["max_pending_per_guest"] == 1
    s = await p.update_settings({"party_name": "  "})
    assert s["party_name"] == "Party"
    await p.db.close()


# -- approvals vs withdraw --------------------------------------------------------------
@pytest.mark.asyncio
async def test_approve_after_withdraw_is_noop(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    req = await p.create_request("g1", "0")
    rows = await p.db.fetchall("SELECT * FROM requests WHERE status='pending'")
    await p.withdraw_request("g1", req["id"])
    # approve_all read the row before the withdraw landed: simulate by approving by hand
    orig = p.db.fetchall

    async def stale(sql: str, *args: Any):  # noqa: ANN202
        if "status='pending'" in sql:
            return rows
        return await orig(sql, *args)

    p.db.fetchall = stale  # type: ignore[method-assign]
    assert await p.approve_all(None) == 0
    assert p.queue == []
    row = await p.db.fetchone("SELECT status FROM requests WHERE id=?", req["id"])
    assert row and row["status"] == "withdrawn"
    await p.db.close()


@pytest.mark.asyncio
async def test_decide_withdrawn_request_conflicts(tmp_path: Path) -> None:
    from am_party.auth import User

    p = await make_party(tmp_path)
    req = await p.create_request("g1", "0")
    await p.withdraw_request("g1", req["id"])
    user = User.from_row(
        {"id": 1, "sub": "s", "email": None, "name": "x", "picture": None, "is_admin": 1,
         "permissions": "[]", "created_at": 0, "last_login": 0}
    )  # fmt: skip
    with pytest.raises(ApiError) as exc:
        await p.decide_request(req["id"], user, True)
    assert exc.value.status == 409
    assert p.queue == []
    await p.db.close()


# -- queue ------------------------------------------------------------------------------
class _Locked:
    def __init__(self, qid: int) -> None:
        self.queue_item_id = qid


class _Eng:
    def __init__(self) -> None:
        self.locked: Any = None
        self.now = None
        self.inflight: list[Any] = []

    def poke(self) -> None:
        pass

    def position(self) -> float:
        return 0.0


@pytest.mark.asyncio
async def test_play_next_positions_do_not_collapse(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    p.engine = _Eng()  # type: ignore[assignment]
    await p._insert_queue(Track(id="first", title="f", artist="a"), "admin", "end")  # noqa: SLF001
    await p._insert_queue(Track(id="last", title="l", artist="a"), "admin", "end")  # noqa: SLF001
    p.engine.locked = _Locked(p.queue[0]["id"])  # type: ignore[union-attr]
    for i in range(80):
        await p._insert_queue(Track(id=f"n{i}", title="n", artist="a"), "admin", "next")  # noqa: SLF001
    ids = [q["track"].id for q in p.queue]
    assert ids[0] == "first" and ids[-1] == "last"
    assert ids[1:-1] == [f"n{i}" for i in reversed(range(80))]  # each "next" goes right after the locked one
    positions = [q["position"] for q in p.queue]
    assert len(set(positions)) == len(positions)
    rows = await p.db.fetchall("SELECT track_id FROM queue ORDER BY position, id")
    assert [r["track_id"] for r in rows] == ids
    await p.db.close()


@pytest.mark.asyncio
async def test_remove_unknown_queue_item_404(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    with pytest.raises(ApiError) as exc:
        await p.remove_from_queue(12345)
    assert exc.value.status == 404
    await p.db.close()


@pytest.mark.asyncio
async def test_concurrent_moves_are_atomic(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    for i in range(5):
        await p._insert_queue(Track(id=str(i), title="s", artist="a"), "admin", "end")  # noqa: SLF001
    qids = [q["id"] for q in p.queue]
    await asyncio.gather(*(p.move_in_queue(qids[i % 5], (i * 3) % 5) for i in range(20)))
    rows = await p.db.fetchall("SELECT id, position FROM queue ORDER BY position, id")
    assert sorted(r["position"] for r in rows) == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert [r["id"] for r in rows] == [q["id"] for q in p.queue]
    await p.db.close()


# -- history ------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_history_is_pruned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import am_party.party as party_mod
    from am_party.player.engine import Candidate, NowPlaying

    monkeypatch.setattr(party_mod, "HISTORY_KEEP", 5)
    p = await make_party(tmp_path)
    for i in range(12):
        c = Candidate(Track(id=f"h{i}", title="h", artist="a"), None, "admin")
        await p.on_track_started(NowPlaying(candidate=c, prepared=None, started_at=0))  # type: ignore[arg-type]
    rows = await p.db.fetchall("SELECT track_id FROM history ORDER BY id")
    assert [r["track_id"] for r in rows] == [f"h{i}" for i in range(7, 12)]
    await p.close()
    await p.db.close()


# -- fallback -----------------------------------------------------------------------------
class _Apple:
    def __init__(self, tracks: list[Track]) -> None:
        self.tracks = tracks
        self.configured = True

    async def get_playlist_tracks(self, pid: str) -> list[Track]:
        return list(self.tracks)


def _tracks(n: int, prefix: str = "f") -> list[Track]:
    return [Track(id=f"{prefix}{i}", title=f"T{i}", artist="a") for i in range(n)]


@pytest.mark.asyncio
async def test_fallback_reload_keeps_order_and_pointer(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    p.apple = _Apple(_tracks(40))
    p._settings["fallback_playlist_id"] = "pl"  # noqa: SLF001
    await p.reload_fallback()
    played = []
    for _ in range(10):
        t = p._peek_fallback()  # noqa: SLF001
        assert t
        played.append(t.id)
        p._advance_fallback(t.id)  # noqa: SLF001
    order, ptr = list(p.fallback_order), p.fallback_ptr
    await p.reload_fallback()  # hourly refresh, same tracks
    assert p.fallback_order == order and p.fallback_ptr == ptr
    # playlist edited: one played and one unplayed track removed, two added
    upcoming = p._peek_fallback()  # noqa: SLF001
    assert upcoming
    gone = {played[0], next(t.id for t in p.fallback_tracks if t.id not in played and t.id != upcoming.id)}
    p.apple.tracks = [t for t in p.apple.tracks if t.id not in gone] + _tracks(2, "new")
    await p.reload_fallback()
    rest = []
    while True:
        t = p._peek_fallback()  # noqa: SLF001
        assert t
        if t.id in rest or (t.id in played and len(rest) < 30):
            break
        rest.append(t.id)
        p._advance_fallback(t.id)  # noqa: SLF001
        if not p.fallback_played:
            break  # pass complete
    # every remaining track played once before anything repeats, played ones never again
    assert not set(rest) & set(played)
    assert set(rest) == {t.id for t in p.fallback_tracks} - set(played)
    await p.db.close()


@pytest.mark.asyncio
async def test_fallback_full_pass_before_repeat(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    p.apple = _Apple(_tracks(50))
    p._settings["fallback_playlist_id"] = "pl"  # noqa: SLF001
    await p.reload_fallback()
    seen = []
    for _ in range(50):
        t = p._peek_fallback()  # noqa: SLF001
        assert t
        seen.append(t.id)
        p._advance_fallback(t.id)  # noqa: SLF001
    assert len(set(seen)) == 50
    await p.db.close()


# -- hub ------------------------------------------------------------------------------------
class _WS:
    def __init__(self, delay: float = 0.0, fail: bool = False) -> None:
        self.delay, self.fail, self.got, self.closed = delay, fail, [], False

    async def send_str(self, data: str) -> None:
        if self.fail:
            raise ConnectionResetError
        await asyncio.sleep(self.delay)
        self.got.append(data)

    async def close(self, **_: Any) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_hub_send_all_concurrent_and_drops_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    import am_party.party as party_mod

    monkeypatch.setattr(party_mod, "WS_SEND_TIMEOUT", 0.2)
    hub = Hub()
    fast, slow, dead = _WS(), _WS(delay=5), _WS(fail=True)
    for ws in (fast, slow, dead):
        hub.clients[ws] = None  # type: ignore[index]
    t = time.monotonic()
    await hub.send_all({"type": "x"})
    assert time.monotonic() - t < 1.0
    assert fast.got == [json.dumps({"type": "x"})]
    assert list(hub.clients) == [fast]
    await asyncio.sleep(0.05)
    assert slow.closed


# -- db transaction ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_transaction_rolls_back_and_isolates(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    await db.open()
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.execute("INSERT INTO settings(key, value) VALUES('a', '1')")
            raise RuntimeError
    assert await db.fetchone("SELECT * FROM settings WHERE key='a'") is None

    entered = asyncio.Event()
    release = asyncio.Event()

    async def tx() -> None:
        async with db.transaction():
            await db.execute("INSERT INTO settings(key, value) VALUES('b', '1')")
            entered.set()
            await release.wait()
            raise RuntimeError("abort")

    task = asyncio.create_task(tx())
    await entered.wait()
    # another coroutine's write waits instead of committing the half-done transaction
    other = asyncio.create_task(db.execute("INSERT INTO settings(key, value) VALUES('c', '1')"))
    await asyncio.sleep(0.05)
    assert not other.done()
    release.set()
    with pytest.raises(RuntimeError):
        await task
    await other
    keys = [r["key"] for r in await db.fetchall("SELECT key FROM settings ORDER BY key")]
    assert keys == ["c"]
    await db.close()


# -- API ------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_move_index_validation(tmp_path: Path) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from am_party import api

    p = await make_party(tmp_path)
    for i in range(2):
        await p._insert_queue(Track(id=str(i), title="s", artist="a"), "admin", "end")  # noqa: SLF001

    class Auth:
        async def require(self, request: web.Request, perm: str) -> None:
            return None

    class Enc:
        party = p
        auth = Auth()

    app = web.Application(middlewares=[api.error_middleware])
    app["am_party"] = Enc()
    app.add_routes(api.routes)
    hdr = {"Sec-Fetch-Site": "same-origin"}
    async with TestClient(TestServer(app)) as client:
        qid = p.queue[0]["id"]
        for payload, msg in (({"index": "x"}, "index must be an integer"), ({}, "index is required")):
            r = await client.post(f"/api/admin/queue/{qid}/move", json=payload, headers=hdr)
            assert r.status == 400
            assert (await r.json())["error"] == msg
        r = await client.post(f"/api/admin/queue/{qid}/move", json={"index": 1}, headers=hdr)
        assert r.status == 200
        r = await client.delete("/api/admin/queue/999999", headers=hdr)
        assert r.status == 404
        r = await client.patch("/api/admin/settings", json={"crossfade_duration": "abc"}, headers=hdr)
        assert r.status == 400
    await p.db.close()


# -- previous -------------------------------------------------------------------------------
class _FakeEngine:
    def __init__(self) -> None:
        self.now: Any = None
        self.pos = 0.0
        self.locked = None
        self.inflight: list[Any] = []
        self.paused = False
        self.seeks: list[float] = []
        self.skips: list[bool] = []

    def position(self) -> float:
        return self.pos

    def seek(self, pos: float) -> bool:
        self.seeks.append(pos)
        return True

    def skip(self, requeue: bool = False) -> None:
        self.skips.append(requeue)

    def poke(self) -> None:
        pass


@pytest.mark.asyncio
async def test_previous_goes_back_through_history(tmp_path: Path) -> None:
    from am_party.player.engine import Candidate, NowPlaying

    p = await make_party(tmp_path)
    eng = _FakeEngine()
    p.engine = eng  # type: ignore[assignment]

    async def play(tid: str, source: str = "admin") -> None:
        c = Candidate(Track(id=tid, title=tid, artist="a"), None, source)
        eng.now = NowPlaying(candidate=c, prepared=None, started_at=0)  # type: ignore[arg-type]
        await p.on_track_started(eng.now)

    await p.add_to_queue("0", "end", None)  # type: ignore[arg-type]
    for tid in ("A", "B", "C"):
        await play(tid, "fallback" if tid == "B" else "admin")

    # well into the song: restart it
    eng.pos = 10.0
    assert await p.previous() == "restart"
    assert eng.seeks == [0] and not eng.skips

    # near the start: B, then C (where we were), then the old queue
    eng.pos = 1.0
    assert await p.previous() == "previous"
    assert eng.skips == [True]
    assert [q["track"].id for q in p.queue] == ["B", "C", "0"]
    assert [q["source"] for q in p.queue][:2] == ["fallback", "admin"]

    # B starts again (consumed from the queue); pressing again goes to A, not C
    q = p.queue[0]
    c = Candidate(q["track"], q["id"], q["source"])
    eng.now = NowPlaying(candidate=c, prepared=None, started_at=0)  # type: ignore[arg-type]
    await p.on_track_started(eng.now)
    assert await p.previous() == "previous"
    assert [q["track"].id for q in p.queue] == ["A", "B", "C", "0"]
    rows = await p.db.fetchall("SELECT track_id FROM history ORDER BY id")
    assert [r["track_id"] for r in rows] == []
    # nothing earlier: restart
    await play("A")
    eng.seeks.clear()
    assert await p.previous() == "restart"
    assert eng.seeks == [0]
    await p.close()
    await p.db.close()


@pytest.mark.asyncio
async def test_previous_with_nothing_playing(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    p.engine = _FakeEngine()  # type: ignore[assignment]
    with pytest.raises(ApiError) as exc:
        await p.previous()
    assert exc.value.status == 409
    await p.close()
    await p.db.close()
