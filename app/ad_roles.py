"""Turn a user's AD groups into TRIAGE-1 roles, and keep them fresh while the user is signed in."""
from __future__ import annotations

import logging
import time

from app import rbac
from app.store import supabase, audit

log = logging.getLogger("uvicorn.error")

_ORDER = list(rbac.ROLES)          # stable, canonical ordering
_MAPPINGS_CHANGED = 0.0            # set when a group mapping is saved or deleted
_RETRY_AT: dict[str, float] = {}   # user_id -> do not ask AD again before this time
_RETRY_SECONDS = 60


def sam_from(username: str) -> str:
    """'user@lab.local' or 'LAB\\user' -> 'user'."""
    u = (username or "").strip()
    if "\\" in u:
        u = u.split("\\", 1)[1]
    return u.split("@", 1)[0]


def merge_roles(mapped: list[str]) -> list[str]:
    """Union of mapped roles, valid names only, in canonical order."""
    wanted = {r.strip() for r in mapped if r and r.strip() in rbac.ROLES}
    return [r for r in _ORDER if r in wanted]


def roles_for_group_ids(group_ids: list[str]) -> list[str]:
    ids = {g.strip().lower() for g in group_ids if g}
    if not ids:
        return []
    rows = supabase.table("ad_group_mappings").select("group_id,role").execute().data or []
    return merge_roles([r["role"] for r in rows if (r.get("group_id") or "").strip().lower() in ids])


def resolve_roles(identity, username: str) -> list[str]:
    """May raise Unavailable if the directory cannot be read. Login must fail closed on that."""
    return roles_for_group_ids(identity.get_groups(sam_from(username)))


# ---- Refresh while signed in ----
def mark_mappings_changed() -> None:
    """Call after a mapping is saved or deleted: every AD user is re-checked at their next lookup."""
    global _MAPPINGS_CHANGED
    _MAPPINGS_CHANGED = time.time()
    _RETRY_AT.clear()


def _interval_seconds() -> float:
    try:
        from app import config
        return float(config.get("identity.refresh_minutes")) * 60
    except Exception:
        return 900.0


def is_stale(checked_at, now: float | None = None) -> bool:
    """checked_at is None for local accounts, which are never refreshed."""
    if checked_at is None:
        return False
    now = now or time.time()
    return checked_at < _MAPPINGS_CHANGED or now - checked_at > _interval_seconds()


def decide(current_role: str, roles: list[str]) -> tuple[str, str]:
    """('revoked'|'changed'|'same', role value to store)."""
    if not roles:
        return "revoked", current_role
    new = ",".join(roles)
    return ("same" if new == current_role else "changed"), new


def refresh_user(row: dict) -> tuple[str, dict]:
    """Re-read an AD user's groups. Returns (status, row) where status is
    'same', 'changed', 'revoked' or 'unavailable'. On 'unavailable' the last-known roles stay."""
    uid, now = row["user_id"], time.time()
    if _RETRY_AT.get(uid, 0) > now:
        return "unavailable", row
    try:
        from app.ports import registry
        roles = resolve_roles(registry.get_identity(), uid)
    except Exception as e:                                # directory down or not configured
        log.warning("AD refresh failed for %s: %s", uid, e)
        _RETRY_AT[uid] = now + _RETRY_SECONDS
        return "unavailable", row

    status, new = decide(row.get("role") or "", roles)
    if status == "revoked":
        supabase.table("sessions").delete().eq("user_id", uid).execute()
        supabase.table("users").update({"roles_checked_at": now}).eq("user_id", uid).execute()
        audit(None, None, uid, "ad_access_revoked", "no mapped AD group")
        return status, row

    patch = {"roles_checked_at": now}
    if status == "changed":
        patch["role"] = new
        audit(None, None, uid, "ad_roles_changed", f"{row.get('role')} -> {new} (refresh)")
    supabase.table("users").update(patch).eq("user_id", uid).execute()
    row = dict(row, role=new, roles_checked_at=now)
    return status, row