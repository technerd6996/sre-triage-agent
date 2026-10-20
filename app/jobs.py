"""Durable job queue for ticket runs. Lowest priority number first, then oldest. Survives restarts."""
import time
import uuid

from app import store

MAX_AGE = 6 * 3600          # queued jobs older than this expire


def pick_next(rows):
    return min(rows, key=lambda r: (r["priority"], r["created"])) if rows else None


def _t():
    return store.supabase.table("jobs")


def queued() -> list:
    now, out = time.time(), []
    for r in _t().select("*").eq("status", "queued").execute().data:
        if now - r["created"] > MAX_AGE:
            _t().update({"status": "expired"}).eq("id", r["id"]).execute()
        else:
            out.append(r)
    return out


def position(job_id: str) -> int:
    rows = sorted(queued(), key=lambda r: (r["priority"], r["created"]))
    return next((i + 1 for i, r in enumerate(rows) if r["id"] == job_id), 0)


def enqueue(ticket: str, source: str, actor: str, priority: int):
    for r in queued():
        if r["ticket"] == ticket:
            return r, position(r["id"])
    row = {"id": uuid.uuid4().hex[:10], "ticket": ticket, "source": source, "actor": actor,
           "priority": priority, "status": "queued", "created": time.time()}
    _t().insert(row).execute()
    store.audit(None, ticket, actor, "job_queued", f"source={source} priority={priority}")
    return row, position(row["id"])


def set_status(job_id: str, status: str) -> None:
    _t().update({"status": status}).eq("id", job_id).execute()