"""Web API for the triage agent. One ticket is worked at a time (one GPU, one 'active server').
The UI polls /runs/{id} for new events and posts approve/reject to /runs/{id}/decision.
Runs, events and an audit trail are persisted to SQLite (app/store.py).
When a run ends, a readable report is written to the ServiceNow work notes."""
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
from app.agent.report import build_report, describe, outcome_code
from app.tools.snow import add_work_note, get_incident, list_queue

log = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/agent")

MAX_RUNS = 20
RUNS = {r["id"]: r for r in store.load_runs(MAX_RUNS)}  # run_id -> run dict, oldest first
_lock = threading.Lock()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
APPROVAL_TIMEOUT = int(os.getenv("APPROVAL_TIMEOUT_MIN", "30")) * 60   # no answer -> auto-reject -> escalate
RUN_STALL_TIMEOUT = int(os.getenv("RUN_STALL_TIMEOUT_MIN", "10")) * 60  # no progress -> run marked failed


class RunRequest(BaseModel):
    ticket: str = Field(pattern=r"^INC\d{7,}$")


class DecisionRequest(BaseModel):
    approve: bool
    feedback: str = Field(default="", max_length=300)


class AutoRequest(BaseModel):
    enabled: bool


def _actor(request: Request | None) -> str:
    """Authenticated user (set by AuthMiddleware), else client IP in dev mode."""
    user = getattr(getattr(request, "state", None), "user", None)
    if user:
        return user
    return request.client.host if request and request.client else "unknown"


def _active():
    return next((r for r in RUNS.values() if r["status"] in ("running", "awaiting_approval")), None)


def _add_event(run: dict, ev: dict):
    ev.setdefault("ts", time.time())
    run["last_event"] = ev["ts"]
    run["events"].append(ev)
    store.add_event(run["id"], len(run["events"]) - 1, ev)


def _post_report(run: dict):
    """Writes the structured end-of-run report to the ServiceNow work notes, once per run."""
    if run.get("reported") or run["status"] not in ("done", "error"):
        return
    run["reported"] = True
    try:
        add_work_note.invoke({"number": run["ticket"], "note": build_report(run)[:30000]})
    except Exception as e:
        log.warning("Could not post the run report to %s: %s", run["ticket"], str(e)[:200])


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
    _post_report(run)


def start_run(ticket: str, source: str = "manual", actor: str = "system"):
    """Starts a run unless another is active. Returns the run, or None if busy.
    source: manual | chat | auto (only recorded in the audit trail)."""
    with _lock:
        if _active():
            return None
        run = {"id": uuid.uuid4().hex[:8], "ticket": ticket, "status": "running", "events": [],
               "thread_id": None, "skill": None, "server": None, "pending": None,
               "created": time.time(), "last_event": time.time()}
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


@router.get("/live")
def live():
    """What the agent is doing right now. Shown to every chat user as a small status box."""
    r = _active()
    if not r:
        return {"active": False}
    if r["status"] == "awaiting_approval":
        text = "Waiting for an engineer to approve an action"
    else:
        lines = [s for s in (describe(e) for e in r["events"]) if s]
        text = lines[-1] if lines else "Starting up"
    return {"active": True, "ticket": r["ticket"], "server": r["server"], "status": r["status"], "text": text}


@router.get("/runs/{run_id}")
def get_run(run_id: str, after: int = 0):
    r = RUNS.get(run_id)
    if not r:
        raise HTTPException(404, "Unknown run")
    ev = r["events"]
    steps = [{"ts": e.get("ts"), "text": s} for e in ev[max(after, 0):] if (s := describe(e))]
    return {"id": r["id"], "ticket": r["ticket"], "status": r["status"], "skill": r["skill"],
            "server": r["server"], "pending": r["pending"], "events": ev[max(after, 0):],
            "steps": steps, "next": len(ev), "outcome": outcome_code(r)}

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
    who = _actor(request)
    tools = ", ".join(a.get("name", "?") for a in pending)
    _add_event(r, {"type": "decision", "approve": req.approve, "by": who})
    store.save_run(r)
    store.audit(r["id"], r["ticket"], who,
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


# ---- watchdog: nothing may block the single agent slot forever ----
def _expire_approval(r: dict):
    with _lock:
        if r["status"] != "awaiting_approval":
            return
        pending = r["pending"] or []
        r["status"] = "running"
        r["pending"] = None
    tools = ", ".join(a.get("name", "?") for a in pending)
    _add_event(r, {"type": "decision", "approve": False, "by": "system (approval timed out)"})
    store.save_run(r)
    store.audit(r["id"], r["ticket"], "system", "approval_timeout",
                f"tools={tools}; no response in {APPROVAL_TIMEOUT // 60} min")
    threading.Thread(
        target=_consume,
        args=(r, resume(r["thread_id"], False, "Approval timed out: no engineer responded in time.")),
        daemon=True).start()


def _fail_stalled(r: dict):
    mins = RUN_STALL_TIMEOUT // 60
    _add_event(r, {"type": "error",
                   "message": f"No progress for {mins} min (LLM or a tool may be hung). "
                              "The slot was released; check the ticket and re-run if needed."})
    r["status"] = "error"
    store.save_run(r)
    store.audit(r["id"], r["ticket"], "system", "run_stalled", f"no progress for {mins} min")
    _post_report(r)


def _watchdog():
    while True:
        time.sleep(30)
        try:
            now = time.time()
            for r in list(RUNS.values()):
                idle = now - r.get("last_event", r.get("created", now))
                if r["status"] == "awaiting_approval" and idle > APPROVAL_TIMEOUT:
                    _expire_approval(r)
                elif r["status"] == "running" and idle > RUN_STALL_TIMEOUT:
                    _fail_stalled(r)
        except Exception as e:
            log.warning("Watchdog error: %s", str(e)[:200])


threading.Thread(target=_watchdog, daemon=True, name="run-watchdog").start()