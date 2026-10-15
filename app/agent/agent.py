"""Web API for the triage agent. One ticket is worked at a time (one GPU, one 'active server').
The UI polls /runs/{id} for new events and posts approve/reject to /runs/{id}/decision.
Runs, events and an audit trail are persisted to SQLite (app/store.py)."""
import logging
import os
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import store
from app.agent.agent import resume, start
from app.agent.policy import RISK
from app.tools.snow import get_incident, list_queue

log = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/agent")

MAX_RUNS = 20
RUNS = {r["id"]: r for r in store.load_runs(MAX_RUNS)}  # run_id -> run dict, oldest first
_lock = threading.Lock()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))


class RunRequest(BaseModel):
    ticket: str = Field(pattern=r"^INC\d{7,}$")


class DecisionRequest(BaseModel):
    approve: bool
    feedback: str = Field(default="", max_length=300)


class AutoRequest(BaseModel):
    enabled: bool


def _actor(request: Request | None) -> str:
    """Who did this. Client IP for now; replaced by the authenticated user in Phase 2."""
    return request.client.host if request and request.client else "unknown"


def _active():
    return next((r for r in RUNS.values() if r["status"] in ("running", "awaiting_approval")), None)


def _add_event(run: dict, ev: dict):
    run["events"].append(ev)
    store.add_event(run["id"], len(run["events"]) - 1, ev)


def _consume(run: dict, events):
    """Runs in a background thread: copies agent events into the run and tracks its status."""
    run["status"] = "running"
    run["pending"] = None
    store.save_run(run)
    try:
        for ev in events:
            if ev["type"] == "start":
                run.update(thread_id=ev["thread_id"], skill=ev["skill"], server=ev["server"])
            _add_event(run, ev)
            if ev["type"] == "approval_required":
                run["status"] = "awaiting_approval"
                run["pending"] = [{**a, "risk": f"L{RISK.get(a.get('name'), 2)}"} for a in ev["actions"]]
                store.audit(run["id"], run["ticket"], "agent", "approval_requested",
                            ", ".join(a.get("name", "?") for a in ev["actions"]))
            elif ev["type"] == "error":
                run["status"] = "error"
            store.save_run(run)
    except Exception as e:
        _add_event(run, {"type": "error", "message": str(e)[:300]})
        run["status"] = "error"
    if run["status"] == "running":
        run["status"] = "done"
    store.save_run(run)
    store.audit(run["id"], run["ticket"], "agent", f"run_{run['status']}")


def start_run(ticket: str, source: str = "manual", actor: str = "system"):
    """Starts a run unless another is active. Returns the run, or None if busy.
    source: manual | chat | auto (only recorded in the audit trail)."""
    with _lock:
        if _active():
            return None
        run = {"id": uuid.uuid4().hex[:8], "ticket": ticket, "status": "running", "events": [],
               "thread_id": None, "skill": None, "server": None, "pending": None,
               "created": time.time()}
        RUNS[run["id"]] = run
        while len(RUNS) > MAX_RUNS:
            RUNS.pop(next(iter(RUNS)))
    store.save_run(run)
    store.audit(run["id"], ticket, actor, "run_started", f"source={source}")
    threading.Thread(target=_consume, args=(run, start(ticket)), daemon=True).start()
    return run


@router.get("/queue")
def queue():
    try:
        rows = []
        for n in list_queue():
            t = get_incident.invoke({"number": n})
            rows.append({"number": n, "short_description": t.get("short_description", ""),
                         "priority": t.get("priority", "")})
        return rows
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=502)


@router.post("/run")
def run_ticket(req: RunRequest, request: Request):
    run = start_run(req.ticket, source="manual", actor=_actor(request))
    if not run:
        raise HTTPException(409, "Another ticket is being worked. Finish it (or reject its approval) first.")
    return {"run_id": run["id"]}


@router.get("/runs")
def list_runs():
    return [{"id": r["id"], "ticket": r["ticket"], "status": r["status"], "skill": r["skill"],
             "server": r["server"]} for r in reversed(list(RUNS.values()))]


@router.get("/runs/{run_id}")
def get_run(run_id: str, after: int = 0):
    r = RUNS.get(run_id)
    if not r:
        raise HTTPException(404, "Unknown run")
    ev = r["events"]
    return {"id": r["id"], "ticket": r["ticket"], "status": r["status"], "skill": r["skill"],
            "server": r["server"], "pending": r["pending"], "events": ev[max(after, 0):], "next": len(ev)}


@router.post("/runs/{run_id}/decision")
def decide(run_id: str, req: DecisionRequest, request: Request):
    r = RUNS.get(run_id)
    if not r:
        raise HTTPException(404, "Unknown run")
    with _lock:
        if r["status"] != "awaiting_approval":
            raise HTTPException(409, "No approval is pending for this run")
        pending = r["pending"] or []
        r["status"] = "running"
        r["pending"] = None
    tools = ", ".join(a.get("name", "?") for a in pending)
    _add_event(r, {"type": "decision", "approve": req.approve})
    store.save_run(r)
    store.audit(r["id"], r["ticket"], _actor(request),
                "approved" if req.approve else "rejected",
                f"tools={tools}; feedback={req.feedback}")
    threading.Thread(target=_consume, args=(r, resume(r["thread_id"], req.approve, req.feedback)),
                     daemon=True).start()
    return {"ok": True}


@router.get("/audit")
def audit_log(limit: int = 100):
    return store.recent_audit(max(1, min(limit, 500)))


# ---- auto mode: poll the L1 queue and work new tickets one at a time ----
_auto = {"enabled": False, "thread": None, "tried": set()}


def _auto_loop():
    while _auto["enabled"]:
        try:
            if not _active():
                for n in list_queue():
                    if n not in _auto["tried"]:
                        _auto["tried"].add(n)  # each ticket is attempted once per server session
                        if not start_run(n, source="auto", actor="auto-mode"):
                            _auto["tried"].discard(n)
                        break
        except Exception as e:
            log.warning("Auto-poll error: %s", str(e)[:200])
        time.sleep(POLL_SECONDS)


@router.get("/auto")
def get_auto():
    return {"enabled": _auto["enabled"]}


@router.post("/auto")
def set_auto(req: AutoRequest, request: Request):
    _auto["enabled"] = req.enabled
    store.audit(None, None, _actor(request), "auto_mode_on" if req.enabled else "auto_mode_off")
    if req.enabled and not (_auto["thread"] and _auto["thread"].is_alive()):
        _auto["thread"] = threading.Thread(target=_auto_loop, daemon=True)
        _auto["thread"].start()
    return {"enabled": _auto["enabled"]}