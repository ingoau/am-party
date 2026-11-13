"""Automatic request review (lyrics + Jev), with the network replaced by fakes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from am_party.autoapprove import (
    AutoReviewer,
    Lyrics,
    ReviewError,
    Verdict,
    _best_search_hit,
    build_state,
    parse_verdict,
)
from am_party.db import Database
from am_party.models import Track
from am_party.party import ApiError, Hub, Party


class FakeReviewer:
    def __init__(self, verdicts: dict[str, Verdict | Exception]) -> None:
        self.verdicts = verdicts
        self.api_key: str | None = "key"
        self.calls: list[tuple[str, str, str]] = []
        self.gate: asyncio.Event | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def review(self, track: Track, prompt: str, model: str) -> Verdict:
        self.calls.append((track.id, prompt, model))
        if self.gate:
            await self.gate.wait()
        v = self.verdicts[track.id]
        if isinstance(v, Exception):
            raise v
        return v


async def make_party(tmp_path: Path, reviewer: Any, **settings: Any) -> Party:
    db = Database(tmp_path / "p.db")
    await db.open()
    party = Party(db, apple=None, hub=Hub())
    await party.load()
    party.reviewer = reviewer
    await party.update_settings(
        {"request_cooldown_seconds": 0, "max_pending_per_guest": 10, "auto_approval": True, **settings}
    )
    for i in range(5):
        party._cache_track(Track(id=str(i), title=f"Song {i}", artist="A"))  # noqa: SLF001
    return party


async def settle(p: Party) -> None:
    for _ in range(20):
        if not p._reviewing:  # noqa: SLF001
            return
        await asyncio.sleep(0.01)
    raise AssertionError("reviews didn't finish")


async def by_id(p: Party) -> dict[str, dict[str, Any]]:
    return {r["track"]["id"]: r for r in await p.list_requests("all")}


@pytest.mark.asyncio
async def test_confident_verdicts_decide(tmp_path: Path) -> None:
    rv = FakeReviewer(
        {
            "0": Verdict("approve", 0.97, lyrics_found=True),
            "1": Verdict("reject", 0.95, lyrics_found=True),
            "2": Verdict("approve", 0.6, lyrics_found=False),
        }
    )
    p = await make_party(tmp_path, rv)
    for i in range(3):
        assert (await p.create_request(f"g{i}", str(i)))["status"] == "pending"
    await settle(p)
    reqs = await by_id(p)
    assert reqs["0"]["status"] == "approved" and reqs["0"]["auto"]["status"] == "approved"
    assert reqs["1"]["status"] == "rejected" and reqs["1"]["auto"]["probability"] == 0.95
    # below the 90% default: left for a person, with Jev's lean recorded
    assert reqs["2"]["status"] == "pending"
    assert reqs["2"]["auto"] == {
        "status": "flagged",
        "decision": "approve",
        "probability": 0.6,
        "note": "No lyrics found",
    }
    assert [q["track"].id for q in p.queue] == ["0"]


@pytest.mark.asyncio
async def test_threshold_prompt_and_model_are_configurable(tmp_path: Path) -> None:
    rv = FakeReviewer({"0": Verdict("approve", 0.8, lyrics_found=True)})
    p = await make_party(
        tmp_path,
        rv,
        auto_approval_threshold=75,
        auto_approval_prompt="  Only 80s music  ",
        auto_approval_model="jev-1.13.0",
    )
    await p.create_request("g", "0")
    await settle(p)
    assert (await by_id(p))["0"]["status"] == "approved"
    assert rv.calls == [("0", "Only 80s music", "jev-1.13.0")]
    for bad in ({"auto_approval_threshold": 40}, {"auto_approval_threshold": "90"}, {"auto_approval_model": "a b"}):
        with pytest.raises(ApiError):
            await p.update_settings(bad)


@pytest.mark.asyncio
async def test_errors_leave_request_for_manual_review(tmp_path: Path) -> None:
    rv = FakeReviewer({"0": ReviewError("Jev returned HTTP 401: bad key"), "1": RuntimeError("boom")})
    p = await make_party(tmp_path, rv)
    await p.create_request("g", "0")
    await p.create_request("g", "1")
    await settle(p)
    reqs = await by_id(p)
    assert reqs["0"]["status"] == "pending" and reqs["0"]["auto"]["status"] == "error"
    assert "401" in reqs["0"]["auto"]["note"]
    assert reqs["1"]["status"] == "pending" and reqs["1"]["auto"]["status"] == "error"
    # recheck after fixing things
    rv.verdicts["0"] = Verdict("approve", 0.99, lyrics_found=True)
    await p.recheck_request(reqs["0"]["id"])
    await settle(p)
    assert (await by_id(p))["0"]["status"] == "approved"


@pytest.mark.asyncio
async def test_human_decision_wins_race(tmp_path: Path) -> None:
    rv = FakeReviewer({"0": Verdict("approve", 0.99, lyrics_found=True)})
    rv.gate = asyncio.Event()
    p = await make_party(tmp_path, rv)
    req = await p.create_request("g", "0")
    await asyncio.sleep(0.01)
    assert (await by_id(p))["0"]["auto"]["status"] == "checking"
    # a person rejects it while Jev is still thinking
    await p.db.execute("UPDATE requests SET status='rejected' WHERE id=?", req["id"])
    rv.gate.set()
    await settle(p)
    assert (await by_id(p))["0"]["status"] == "rejected"
    assert p.queue == []


@pytest.mark.asyncio
async def test_off_without_key_or_when_disabled(tmp_path: Path) -> None:
    rv = FakeReviewer({"0": Verdict("approve", 0.99, lyrics_found=True)})
    rv.api_key = None
    p = await make_party(tmp_path, rv)
    assert p.state()["auto_approval"] is False
    await p.create_request("g", "0")
    await settle(p)
    assert rv.calls == [] and "auto" not in (await by_id(p))["0"]
    # adding a key and calling review_pending picks up what's waiting
    rv.api_key = "key"
    assert await p.review_pending() == 1
    await settle(p)
    assert (await by_id(p))["0"]["status"] == "approved"


@pytest.mark.asyncio
async def test_enabling_reviews_waiting_requests(tmp_path: Path) -> None:
    rv = FakeReviewer({"0": Verdict("reject", 0.99, lyrics_found=True)})
    p = await make_party(tmp_path, rv, auto_approval=False)
    await p.create_request("g", "0")
    await p.update_settings({"auto_approval": True})
    for _ in range(20):
        await asyncio.sleep(0.01)
        if rv.calls and not p._reviewing:  # noqa: SLF001
            break
    assert (await by_id(p))["0"]["status"] == "rejected"


def test_parse_verdict() -> None:
    v = parse_verdict(
        {
            "model": "jev-1.13.0",
            "answers": {
                "decision": {
                    "type": "choice",
                    "choice": "reject",
                    "confidence": 0.8,
                    "probabilities": {"approve": 0.1, "reject": 0.9},
                }
            },
        },
        lyrics_found=True,
    )
    assert (v.decision, v.probability, v.model) == ("reject", 0.9, "jev-1.13.0")
    with pytest.raises(ReviewError):
        parse_verdict({"answers": {}}, lyrics_found=True)
    with pytest.raises(ReviewError):
        parse_verdict({"answers": {"decision": {"choice": "maybe", "probabilities": {}}}}, lyrics_found=True)


def test_state_and_search_hit() -> None:
    t = Track(id="1", title="Song", artist="Band", album="LP", duration=200, explicit=True)
    state = build_state(t, Lyrics("la la la"))
    assert "Song: Song" in state and "explicit" in state and state.endswith("la la la")
    assert "not available" in build_state(t, Lyrics(None))
    assert "instrumental" in build_state(t, Lyrics(None, instrumental=True))
    hits = [
        {"duration": 120, "plainLyrics": "short version"},
        {"duration": 203, "plainLyrics": "album version"},
        {"duration": 200, "plainLyrics": None},
    ]
    assert _best_search_hit(hits, t)["plainLyrics"] == "album version"  # type: ignore[index]
    assert _best_search_hit([{"duration": 100, "plainLyrics": "x"}], t) is None


def test_reviewer_without_key() -> None:
    rv = AutoReviewer(http=None, api_keys={"openrouter": "or-key"})  # type: ignore[arg-type]
    assert not rv.configured  # default provider is TypeSafe, which has no key
    with pytest.raises(ReviewError, match="TypeSafe"):
        asyncio.run(rv.review(Track(id="1", title="a", artist="b"), "", ""))
    rv.provider = "openrouter"
    assert rv.configured


class _Resp:
    def __init__(self, status: int, data: Any) -> None:
        self.status = status
        self._data = data

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def json(self, content_type: Any = None) -> Any:
        return self._data

    async def text(self) -> str:
        return str(self._data)

    def raise_for_status(self) -> None:
        pass


class FakeHttp:
    """Records requests; LRCLIB finds nothing, Jev approves."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def get(self, url: str, **kw: Any) -> _Resp:
        return _Resp(404, None)

    def post(self, url: str, json: dict[str, Any], headers: dict[str, str], **kw: Any) -> _Resp:
        self.posts.append((url, json, headers))
        answer = {"choice": "approve", "probabilities": {"approve": 0.93, "reject": 0.07}}
        return _Resp(200, {"model": "typesafe/jev-1.14", "answers": {"decision": answer}})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("typesafe", "https://api.typesafe.ai/v1/systemone"),
        ("openrouter", "https://openrouter.ai/api/v1/systemone"),
    ],
)
async def test_provider_routing(provider: str, url: str) -> None:
    http = FakeHttp()
    rv = AutoReviewer(http, {"typesafe": "ts-key", "openrouter": "or-key"})  # type: ignore[arg-type]
    rv.provider = provider
    v = await rv.review(Track(id="1", title="Song", artist="Band"), "be nice", "jev-latest")
    assert (v.decision, v.probability, v.lyrics_found) == ("approve", 0.93, False)
    (sent_url, payload, headers), = http.posts
    assert sent_url == url
    assert headers["Authorization"] == f"Bearer {'or-key' if provider == 'openrouter' else 'ts-key'}"
    assert payload["model"] == "jev-latest"
    assert payload["questions"]["decision"]["instructions"] == "be nice"


@pytest.mark.asyncio
async def test_provider_setting(tmp_path: Path) -> None:
    rv = FakeReviewer({})
    p = await make_party(tmp_path, rv)
    p.set_reviewer(rv)  # type: ignore[arg-type]
    assert rv.provider == "typesafe"  # type: ignore[attr-defined]
    await p.update_settings({"auto_approval_provider": "openrouter", "auto_approval_model": "~typesafe/jev-latest"})
    assert rv.provider == "openrouter"  # type: ignore[attr-defined]
    with pytest.raises(ApiError):
        await p.update_settings({"auto_approval_provider": "openai"})
