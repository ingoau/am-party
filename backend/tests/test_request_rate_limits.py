"""Per-guest request limits: N songs per period, several stacked periods, rolling windows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from am_party.db import Database
from am_party.models import Track
from am_party.party import ApiError, Hub, Party, _human_seconds


async def make_party(tmp_path: Path, limits: list[dict[str, int]] | None = None) -> Party:
    """A party with 12 cached tracks; limits=None keeps the default settings."""
    db = Database(tmp_path / "p.db")
    await db.open()
    party = Party(db, apple=None, hub=Hub())
    await party.load()
    if limits is not None:
        await party.update_settings({"request_rate_limits": limits})
    for i in range(12):
        party._cache_track(Track(id=str(i), title=f"Song {i}", artist="A"))  # noqa: SLF001
    return party


async def backdate(party: Party, seconds: float) -> None:
    """Pretend every request so far was made `seconds` earlier."""
    await party.db.execute("UPDATE requests SET created_at = created_at - ?", seconds)


@pytest.mark.asyncio
async def test_default_is_ten_requests_per_ten_minutes(tmp_path: Path) -> None:
    p = await make_party(tmp_path)
    assert p.settings()["request_rate_limits"] == [{"requests": 10, "seconds": 600}]
    for i in range(10):
        await p.create_request("g1", str(i))
    with pytest.raises(ApiError) as err:
        await p.create_request("g1", "10")
    assert err.value.status == 429
    assert "10 songs every 10 minutes" in err.value.message


@pytest.mark.asyncio
async def test_budget_returns_when_requests_age_out(tmp_path: Path) -> None:
    p = await make_party(tmp_path, [{"requests": 2, "seconds": 600}])
    await p.create_request("g1", "0")
    await p.create_request("g1", "1")
    with pytest.raises(ApiError):
        await p.create_request("g1", "2")
    await backdate(p, 601)
    await p.create_request("g1", "2")


@pytest.mark.asyncio
async def test_every_period_applies(tmp_path: Path) -> None:
    p = await make_party(tmp_path, [{"requests": 2, "seconds": 60}, {"requests": 3, "seconds": 3600}])
    await p.create_request("g1", "0")
    await p.create_request("g1", "1")
    with pytest.raises(ApiError) as err:  # the minute is used up first
        await p.create_request("g1", "2")
    assert "every 1 minute" in err.value.message

    await backdate(p, 61)  # both old requests leave the minute window
    await p.create_request("g1", "2")
    with pytest.raises(ApiError) as err:  # 3 in the hour: the hourly window now bites
        await p.create_request("g1", "3")
    assert "every 1 hour" in err.value.message

    await backdate(p, 3601)
    await p.create_request("g1", "3")


@pytest.mark.asyncio
async def test_windows_are_per_guest(tmp_path: Path) -> None:
    p = await make_party(tmp_path, [{"requests": 1, "seconds": 600}])
    await p.create_request("g1", "0")
    with pytest.raises(ApiError):
        await p.create_request("g1", "1")
    await p.create_request("g2", "1")


@pytest.mark.asyncio
async def test_no_periods_means_no_guest_limit(tmp_path: Path) -> None:
    p = await make_party(tmp_path, [])
    for i in range(12):
        await p.create_request("g1", str(i))


@pytest.mark.asyncio
async def test_admin_can_add_and_remove_periods(tmp_path: Path) -> None:
    p = await make_party(tmp_path, [])
    s = await p.update_settings({"request_rate_limits": [{"requests": 5, "seconds": 300}]})
    assert s["request_rate_limits"] == [{"requests": 5, "seconds": 300}]
    s = await p.update_settings({"request_rate_limits": []})
    assert s["request_rate_limits"] == []


@pytest.mark.parametrize(
    "patch",
    [
        {"request_rate_limits": {"requests": 5, "seconds": 300}},
        {"request_rate_limits": [{"requests": 5.5, "seconds": 300}]},
        {"request_rate_limits": [{"requests": 5, "seconds": 604_801}]},
        {"request_rate_limits": [{"requests": 1001, "seconds": 60}]},
        {"request_rate_limits": [{"seconds": 60}]},
    ],
)
@pytest.mark.asyncio
async def test_invalid_periods_rejected(tmp_path: Path, patch: dict[str, Any]) -> None:
    p = await make_party(tmp_path, [])
    with pytest.raises(ApiError) as err:
        await p.update_settings(patch)
    assert err.value.status == 400


def test_human_seconds() -> None:
    assert _human_seconds(1) == "1 second"
    assert _human_seconds(45) == "45 seconds"
    assert _human_seconds(61) == "1 minute 1 second"
    assert _human_seconds(600) == "10 minutes"
    assert _human_seconds(3660) == "1 hour 1 minute"
    assert _human_seconds(86400) == "1 day"
    assert _human_seconds(7 * 86400) == "7 days"
    assert _human_seconds(90000) == "1 day 1 hour"
