"""Web API for the triage agent. One ticket is worked at a time (one GPU, one 'active server').
The UI polls /runs/{id} for new events and posts approve/reject to /runs/{id}/decision."""
import logging
import os
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agent.agent import resume, start
from app.agent.policy import RISK
from app.tools.snow import get_incident, list_queue

log = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/agent")

RUNS = {}  # run_id -> run dict, oldest first
_lock = threading.Lock()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
MAX_RUNS = 20


class RunRequest(BaseModel):
    ticket: str = Field(pattern=r"^INC\d{7,}$")


class DecisionRequest(BaseModel):
    approve: bool
    feedback: str = Field(default="", max_length=300)


class AutoRequest(BaseModel):
    enabled: bool


def _active():
    return next((r for r in RUNS.values() if r["status"] in ("running", "awaiting_approval")), None)


def _consume(run: dict, events):
    """Runs in a background thread: copies agent events into the run and tracks its status."""
    run["status"] = "running"
    run["pending"] = None
    try:
        for ev in events:
            if ev["type"] == "start":
                run.update(thread_id=ev["thread_id"], skill=ev["skill"], server=ev["server"])
            run["events"].append(ev)
            if ev["type"] == "approval_required":
                run["status"] = "awaiting_approval"
                run["pending"] = [{**a, "risk": f"L{RISK.get(a.get('name'), 2)}"} for a in ev["actions"]]
            elif ev["type"] == "error":
                run["status"] = "error"
    except Exception as e:
        run["events"].append({"type": "error", "message": str(e)[:300]})
        run["status"] = "error"
    if run["status"] == "running":
        run["status"] = "done"


def start_run(ticket: str):
    """Starts a run unless another is active. Returns the run, or None if busy."""
    with _lock:
        if _active():
            return None
        run = {"id": uuid.uuid4().hex[:8], "ticket": ticket, "status": "running", "events": [],
               "thread_id": None, "skill": None, "server": None, "pending": None}
        RUNS[run["id"]] = run
        while len(RUNS) > MAX_RUNS:
            RUNS.pop(next(iter(RUNS)))
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
def run_ticket(req: RunRequest):
    run = start_run(req.ticket)
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
def decide(run_id: str, req: DecisionRequest):
    r = RUNS.get(run_id)
    if not r:
        raise HTTPException(404, "Unknown run")
    with _lock:
        if r["status"] != "awaiting_approval":
            raise HTTPException(409, "No approval is pending for this run")
        r["status"] = "running"
        r["pending"] = None
    r["events"].append({"type": "decision", "approve": req.approve})
    threading.Thread(target=_consume, args=(r, resume(r["thread_id"], req.approve, req.feedback)),
                     daemon=True).start()
    return {"ok": True}


# ---- auto mode: poll the L1 queue and work new tickets one at a time ----
_auto = {"enabled": False, "thread": None, "tried": set()}


def _auto_loop():
    while _auto["enabled"]:
        try:
            if not _active():
                for n in list_queue():
                    if n not in _auto["tried"]:
                        _auto["tried"].add(n)  # each ticket is attempted once per server session
                        if not start_run(n):
                            _auto["tried"].discard(n)
                        break
        except Exception as e:
            log.warning("Auto-poll error: %s", str(e)[:200])
        time.sleep(POLL_SECONDS)


@router.get("/auto")
def get_auto():
    return {"enabled": _auto["enabled"]}


@router.post("/auto")
def set_auto(req: AutoRequest):
    _auto["enabled"] = req.enabled
    if req.enabled and not (_auto["thread"] and _auto["thread"].is_alive()):
        _auto["thread"] = threading.Thread(target=_auto_loop, daemon=True)
        _auto["thread"].start()
    return {"enabled": _auto["enabled"]}