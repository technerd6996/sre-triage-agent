"""Supabase persistence for runs, run events and the audit trail."""
import os
import time

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()   # must run before create_client: main.py imports this module before it calls load_dotenv()

_url, _key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
if not _url or not _key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
supabase: Client = create_client(_url, _key)

import logging
log = logging.getLogger("uvicorn.error")


def _safe(fn):
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            log.warning("store.%s failed: %s", fn.__name__, str(e)[:200])
    wrapper.__name__ = fn.__name__
    return wrapper

@_safe
def save_run(run: dict) -> None:
    supabase.table("runs").upsert({
        "id": run["id"], "ticket": run["ticket"], "status": run["status"],
        "skill": run.get("skill"), "server": run.get("server"),
        "thread_id": run.get("thread_id"),
        "created": run.get("created", time.time()), "updated": time.time(),
        "outcome": run.get("outcome"), "started": run.get("started"), "ended": run.get("ended"),
        "requester": run.get("requester"), "steps": run.get("steps", 0), "nudges": run.get("nudges", 0),
        "tokens_in": run.get("tokens_in", 0), "tokens_out": run.get("tokens_out", 0),
        "llm_calls": run.get("llm_calls", 0), "skill_version": run.get("skill_version"),
    }).execute()

def add_event(run_id: str, seq: int, ev: dict) -> None:
    supabase.table("events").upsert({
        "run_id": run_id, "seq": seq, "ts": time.time(), "body": ev
    }).execute()

def audit(run_id: str | None, ticket: str | None, actor: str, action: str, detail: str = "") -> None:
    supabase.table("audit").insert({
        "ts": time.time(), "run_id": run_id, "ticket": ticket,
        "actor": actor, "action": action, "detail": detail[:1000]
    }).execute()

def recent_audit(limit: int = 100) -> list[dict]:
    res = supabase.table("audit").select("*").order("ts", desc=True).limit(limit).execute()
    return res.data

def load_runs(limit: int = 20) -> list[dict]:
    res = supabase.table("runs").select("*").order("created", desc=True).limit(limit).execute()
    out = []
    for r in reversed(res.data):
        ev_res = supabase.table("events").select("body").eq("run_id", r["id"]).order("seq").execute()
        evs = [e["body"] for e in ev_res.data]
        out.append({"id": r["id"], "ticket": r["ticket"], "status": r["status"], "skill": r["skill"],
                    "server": r["server"], "thread_id": r["thread_id"], "created": r["created"],
                    "outcome": r.get("outcome"), "started": r.get("started"), "ended": r.get("ended"),
                    "requester": r.get("requester"), "steps": r.get("steps") or 0,
                    "nudges": r.get("nudges") or 0, "tokens_in": r.get("tokens_in") or 0,
                    "tokens_out": r.get("tokens_out") or 0, "llm_calls": r.get("llm_calls") or 0,
                    "skill_version": r.get("skill_version"),
                    "events": evs, "pending": None, "last_event": time.time()})  # fresh approval clock after a restart

    for run in out:
        if run["status"] == "awaiting_approval":
            rows = (supabase.table("approvals").select("tool,args_json,risk")
                    .eq("run_id", run["id"]).is_("decided_at", "null").execute().data)
            if rows:
                # Still waiting for a human. The SQLite checkpoint lets the run resume after the decision.
                run["pending"] = [{"name": a["tool"], "args": a.get("args_json") or {},
                                   "risk": a.get("risk"), "description": ""} for a in rows]
                continue
        if run["status"] in ("running", "awaiting_approval"):
            run["status"] = "interrupted"
            run["events"].append({"type": "error",
                                  "message": "Server restarted while this run was in progress. Re-run the ticket."})
            add_event(run["id"], len(run["events"]) - 1, run["events"][-1])
            save_run(run)
            audit(run["id"], run["ticket"], "system", "run_interrupted", "server restart")
    return out

@_safe
def add_llm_call(kind: str, run_id: str | None, tokens_in: int, tokens_out: int,
                 gen_s: float, wait_s: float) -> None:
    supabase.table("llm_calls").insert({
        "ts": time.time(), "kind": kind, "run_id": run_id,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "gen_s": round(gen_s, 2), "wait_s": round(wait_s, 2),
    }).execute()


def llm_totals(run_id: str) -> dict:
    """Token and call totals for one run, summed from llm_calls (also covers resumed runs)."""
    try:
        rows = supabase.table("llm_calls").select("tokens_in,tokens_out").eq("run_id", run_id).execute().data
    except Exception as e:
        log.warning("store.llm_totals failed: %s", str(e)[:200])
        return {}
    return {"tokens_in": sum(r["tokens_in"] for r in rows),
            "tokens_out": sum(r["tokens_out"] for r in rows), "llm_calls": len(rows)}

_SET_CACHE: dict = {}

def get_setting(key: str, default=None):
    hit = _SET_CACHE.get(key)
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    try:
        rows = supabase.table("settings").select("value_json").eq("key", key).execute().data
        val = rows[0]["value_json"] if rows else default
    except Exception as e:
        log.warning("get_setting(%s) failed: %s", key, str(e)[:200])
        return default
    _SET_CACHE[key] = (time.time(), val)
    return val

@_safe
def set_setting(key: str, value, by: str = "system") -> None:
    supabase.table("settings").upsert({"key": key, "value_json": value, "updated_by": by,
                                       "updated_at": time.time()}).execute()
    _SET_CACHE.pop(key, None)
