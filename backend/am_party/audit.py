"""Audit log: who did what, and when.

Every state-changing action is recorded here: staff actions (approvals, queue changes,
skips, settings, users, speakers, credentials), guest requests, sign-ins and the
automatic (Jev) review. Rows keep a snapshot of the actor's name, so they still read
correctly after the user is renamed or deleted. Secrets (tokens, API keys) are never
recorded, only whether they were set or cleared.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .auth import User
    from .db import Database
    from .models import Track
    from .party import Hub

LOGGER = logging.getLogger(__name__)

KEEP_DAYS = 365  # entries older than this are pruned by housekeeping
MAX_PAGE = 200
MAX_DETAILS = 8000  # characters of JSON; longer details are truncated

# Who did it. "user" is a signed-in staff member, "guest" a party guest (by cookie),
# "system" the app itself (e.g. Jev's automatic review), "device" a speaker.
ACTOR_KINDS = ("user", "guest", "system", "device")


def track_ref(track: Track) -> dict[str, Any]:
    return {"id": track.id, "title": track.title, "artist": track.artist}


def track_label(track: Track) -> str:
    return f"{track.artist} - {track.title}" if track.artist else track.title


def guest_label(guest_id: str) -> str:
    return "Guest " + hashlib.sha256(guest_id.encode()).hexdigest()[:4].upper()


def user_label(user: User | None) -> str | None:
    if user is None:
        return None
    return user.name or user.email or f"User {user.id}"


class AuditLog:
    def __init__(self, db: Database, hub: Hub | None = None) -> None:
        self.db = db
        self.hub = hub

    async def record(
        self,
        action: str,
        *,
        user: User | None = None,
        actor_kind: str | None = None,
        actor_name: str | None = None,
        guest_id: str | None = None,
        ip: str | None = None,
        target_type: str | None = None,
        target_id: Any = None,
        target_label: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Add an entry. Never raises: a failed audit write must not undo the action."""
        kind = actor_kind or ("user" if user else "guest" if guest_id else "system")
        name = actor_name if actor_name is not None else user_label(user)
        if name is None and guest_id:
            name = guest_label(guest_id)  # so the search box finds "Guest 1A2B"
        try:
            blob = json.dumps(details, default=str, ensure_ascii=False) if details else None
            if blob and len(blob) > MAX_DETAILS:
                blob = json.dumps({"truncated": True, "preview": blob[:MAX_DETAILS]}, ensure_ascii=False)
            await self.db.execute(
                "INSERT INTO audit_log(at, actor_kind, actor_id, actor_name, guest_id, ip, action, "
                "target_type, target_id, target_label, details) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                time.time(),
                kind,
                user.id if user else None,
                name,
                guest_id,
                ip,
                action,
                target_type,
                None if target_id is None else str(target_id),
                target_label,
                blob,
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("Couldn't write audit entry %s", action)
            return
        who = name or (guest_label(guest_id) if guest_id else kind)
        LOGGER.info(
            "Audit: %s %s%s", who, action, f" {target_label}" if target_label else ""
        )
        if self.hub:
            try:
                await self.hub.send_all({"type": "audit_changed"}, "view_audit_log")
            except Exception:  # noqa: BLE001
                LOGGER.debug("audit_changed broadcast failed", exc_info=True)

    async def list(
        self,
        *,
        before: int | None = None,
        limit: int = 100,
        action: str | None = None,
        actor_id: int | None = None,
        actor_kind: str | None = None,
        q: str | None = None,
        include_ip: bool = False,
    ) -> dict[str, Any]:
        """Newest first. `before` is an entry id (keyset paging); `action` matches
        exactly or as a prefix ending in "." (e.g. "queue.")."""
        limit = max(1, min(int(limit), MAX_PAGE))
        where: list[str] = []
        args: list[Any] = []
        if before is not None:
            where.append("id < ?")
            args.append(before)
        if action:
            if action.endswith("."):
                where.append("action LIKE ? ESCAPE '\\'")
                args.append(_like_escape(action) + "%")
            else:
                where.append("action = ?")
                args.append(action)
        if actor_id is not None:
            where.append("actor_id = ?")
            args.append(actor_id)
        if actor_kind:
            where.append("actor_kind = ?")
            args.append(actor_kind)
        if q:
            pat = "%" + _like_escape(q.strip()[:200]) + "%"
            where.append(
                "(target_label LIKE ? ESCAPE '\\' OR actor_name LIKE ? ESCAPE '\\' "
                "OR action LIKE ? ESCAPE '\\' OR details LIKE ? ESCAPE '\\')"
            )
            args += [pat, pat, pat, pat]
        sql = "SELECT * FROM audit_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = await self.db.fetchall(sql, *args, limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        entries = [_entry_out(r, include_ip) for r in rows]
        return {"entries": entries, "next_before": rows[-1]["id"] if more and rows else None}

    async def prune(self, keep_days: int = KEEP_DAYS) -> int:
        return await self.db.execute_rowcount(
            "DELETE FROM audit_log WHERE at < ?", time.time() - keep_days * 86400
        )


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _entry_out(r: dict[str, Any], include_ip: bool) -> dict[str, Any]:
    try:
        details = json.loads(r["details"]) if r["details"] else None
    except ValueError:
        details = None
    out = {
        "id": r["id"],
        "at": r["at"],
        "action": r["action"],
        "actor": {
            "kind": r["actor_kind"],
            "id": r["actor_id"],
            "name": r["actor_name"],
            # the same short label the requests list shows (the full id is their cookie)
            "guest": guest_label(r["guest_id"]) if r["guest_id"] else None,
        },
        "target": (
            {"type": r["target_type"], "id": r["target_id"], "label": r["target_label"]}
            if r["target_type"] or r["target_label"]
            else None
        ),
        "details": details,
    }
    if include_ip:
        out["ip"] = r["ip"]
    return out


def settings_diff(before: dict[str, Any], after: dict[str, Any], keys: Any) -> dict[str, Any]:
    """{key: {"from": old, "to": new}} for the given keys whose value changed."""
    return {
        k: {"from": before.get(k), "to": after.get(k)}
        for k in keys
        if before.get(k) != after.get(k)
    }
