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
from app.agent import policy
from app import store
from app import jobs
from app.llm import usage
from app.agent.agent import THREAD_TOOLS, resume, start
from app.agent.policy import RISK
from app.agent.report import build_report, describe, outcome_code, _fmt_call
from app.tools.snow import add_work_note, get_incident, list_queue
from app import escalation, checker

log = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/agent")

MAX_RUNS = 20
RUNS = {r["id"]: r for r in store.load_runs(MAX_RUNS)}  # run_id -> run dict, oldest first
for _r in RUNS.values():            # rebuild what resume() needs after a restart
    for _e in _r["events"]:
        if _e.get("type") == "start" and _e.get("tools"):
            THREAD_TOOLS[_e["thread_id"]] = tuple(_e["tools"])
_lock = threading.Lock()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
APPROVAL_TIMEOUT = int(os.getenv("APPROVAL_TIMEOUT_MIN", "30")) * 60   # no answer -> auto-reject -> escalate
P1_BUDGET = int(os.getenv("P1_TIME_BUDGET_MIN", "15")) * 60            # run longer than this -> propose a P1
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
        checker_will_run = (checker.mode() != "off" and outcome_code(run) == "resolved"
                            and checker._spec(run) is not None)
        add_work_note.invoke({"number": run["ticket"],
                              "note": build_report(run, full=not checker_will_run)[:30000]})
    except Exception as e:
        log.warning("Could not post the run report to %s: %s", run["ticket"], str(e)[:200])

def _finalize_metrics(run: dict):
    """Folds token totals and counters into the run so they are saved with it."""
    run.update(store.llm_totals(run["id"]))
    run["steps"] = sum(1 for e in run["events"] if e.get("type") == "tool_call")
    run["nudges"] = sum(1 for e in run["events"] if e.get("type") == "nudge")
    if run["status"] in ("done", "error"):
        run["ended"] = time.time()
        try:
            run["outcome"] = outcome_code(run)
        except Exception:
            run["outcome"] = None

def _post_service_context(run: dict, actions) -> None:
    """Once per run: which services depend on the one being restarted, as a ServiceNow work note."""
    if run.get("service_context") is not None:
        return
    act = next((a for a in actions if a.get("name") == "start_service"), None)
    if not act:
        return
    svc = str((act.get("args") or {}).get("service_name", ""))
    host = str(run.get("server") or "").split(" (")[0]
    run["service_context"] = {}                      # set first, so a failure is not retried in a loop
    try:
        from app.tools import cmdb
        ctx = cmdb.service_context(host, svc)
        run["service_context"] = ctx
        if ctx.get("found"):
            add_work_note.invoke({"number": run["ticket"], "note": ctx["summary"]})
        else:
            store.audit(run["id"], run["ticket"], "system", "service_context_none",
                        ctx.get("error") or "no dependent service in the CMDB")
    except Exception as e:
        log.warning("Service context failed for %s: %s", run["ticket"], str(e)[:200])

def _consume(run: dict, events, kind: str = "triage"):
    """Runs in a background thread: copies agent events into the run and tracks its status."""
    run["status"] = "running"
    run["pending"] = None
    store.save_run(run)
    usage.bind_run(run["id"], kind)        # model calls made while this runs are counted against this run
    auto_resume = False
    try:
        for ev in events:
            if ev["type"] == "start":
                run.update(thread_id=ev["thread_id"], skill=ev["skill"], server=ev["server"],
                           skill_version=ev.get("skill_version"))
            _add_event(run, ev)
            if ev["type"] == "approval_required":
                _post_service_context(run, ev["actions"])
                ok, why = policy.autonomous_restore(ev["actions"], str(run.get("server") or "").split(" (")[0])
                if why:
                    store.audit(run["id"], run["ticket"], "policy", "autonomy_check", why)
                if ok:
                    auto_resume = True
                    _add_event(run, {"type": "decision", "approve": True, "by": "policy (autonomous restore)"})
                    try:
                        add_work_note.invoke({"number": run["ticket"],
                                              "note": f"TRIAGE-1 policy: {why}. No approval needed for critical-service restore."})
                    except Exception as e:
                        log.warning("Could not post autonomy note to %s: %s", run["ticket"], str(e)[:200])
                else:
                    run["status"] = "awaiting_approval"
                    pending_actions = [{**a, "risk": f"L{RISK.get(a.get('name'), 2)}"} for a in ev["actions"]]
                    run["pending"] = pending_actions
                    for act in pending_actions:
                        try:
                            store.supabase.table("approvals").insert({
                                "id": uuid.uuid4().hex[:12], "run_id": run["id"], "ticket": run["ticket"],
                                "tool": act.get("name"), "args_json": act.get("args", {}),
                                "risk": act.get("risk"), "requested_at": time.time()}).execute()
                        except Exception as e:
                            log.error("Could not save approval request for %s: %s", run["ticket"], str(e)[:200])
                    store.audit(run["id"], run["ticket"], "agent", "approval_requested",
                                ", ".join(a.get("name", "?") for a in ev["actions"]))
            elif ev["type"] == "error":
                run["status"] = "error"
            store.save_run(run)
    except Exception as e:
        _add_event(run, {"type": "error", "message": str(e)[:300]})
        run["status"] = "error"
    if auto_resume and run["status"] == "running":
        usage.unbind_run(run["id"])
        return _consume(run, resume(run["thread_id"], True, "Auto-approved by policy."), kind="resume")
    if run["status"] == "running":
        # Verify while the run still holds the active slot: the checker reuses the run's server
        # (aws.ACTIVE), and the next run would clear it.
        try:
            checker.verify_run(run, lambda text, verdict=None: _add_event(
                run, {"type": "verification", "text": text, "verdict": verdict}))
        except Exception as e:
            log.warning("Checker error for %s: %s", run["ticket"], str(e)[:200])
        run["status"] = "done"
        p1 = run.get("p1")
        unverified = any(e.get("type") == "verification" and "unverified" in str(e.get("verdict"))
                         for e in run["events"])
        if p1 and p1["status"] == "proposed" and outcome_code(run) == "resolved" and not unverified:
            p1["status"] = "withdrawn"      # fixed and verified: nothing left to escalate
            store.audit(run["id"], run["ticket"], "system", "p1_withdrawn", p1["reason"])
    usage.unbind_run(run["id"])
    _finalize_metrics(run)
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
               "created": time.time(), "last_event": time.time(),
               "requester": actor, "started": time.time()}
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
    actor = _actor(request)
    if not jobs.queued():                       # nobody waiting ahead of this one
        run = start_run(req.ticket, source="manual", actor=actor)
        if run:
            return {"run_id": run["id"]}
    job, pos = jobs.enqueue(req.ticket, "manual", actor, 1)
    return {"run_id": None, "queued": True, "position": pos}


@router.get("/runs")
def list_runs():
    return [{"id": r["id"], "ticket": r["ticket"], "status": r["status"], "skill": r["skill"],
             "server": r["server"]} for r in reversed(list(RUNS.values()))]

@router.get("/approvals")
def pending_approvals():
    """Everything waiting for a human, oldest first: agent actions and P1 proposals."""
    asked: dict = {}
    try:
        rows = (store.supabase.table("approvals").select("run_id,requested_at")
                .is_("decided_at", "null").execute().data) or []
        for a in rows:
            asked[a["run_id"]] = min(a["requested_at"], asked.get(a["run_id"], a["requested_at"]))
    except Exception as e:
        log.warning("approvals lookup failed: %s", str(e)[:200])
    out = []
    for r in list(RUNS.values()):
        if r["status"] == "awaiting_approval" and r.get("pending"):
            out.append({"run_id": r["id"], "ticket": r["ticket"], "server": r.get("server"),
                        "skill": r.get("skill"),
                        "requested_at": asked.get(r["id"], r.get("last_event", time.time())),
                        "actions": [{"name": a.get("name"), "risk": a.get("risk"),
                                     "text": _fmt_call(a.get("name"), a.get("args"))} for a in r["pending"]]})
        p = r.get("p1")
        if p and p["status"] in ("proposed", "declared_notice_failed"):
            out.append({"run_id": r["id"], "ticket": r["ticket"], "server": r.get("server"),
                        "skill": r.get("skill"), "requested_at": p["at"],
                        "actions": [{"name": "declare_p1", "risk": "P1", "text": f"Declare P1: {p['reason']}"}]})
    out.sort(key=lambda x: x["requested_at"])
    return out

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
            "steps": steps, "next": len(ev), "outcome": outcome_code(r), "p1": escalation.view(r)}

@router.post("/runs/{run_id}/decision")
def decide(run_id: str, req: DecisionRequest, request: Request):
    r = RUNS.get(run_id)
    if not r:
        raise HTTPException(404, "Unknown run")
    if not req.approve and not req.feedback.strip():
        raise HTTPException(422, "A comment is required when rejecting.")
    with _lock:
        if r["status"] != "awaiting_approval":
            raise HTTPException(409, "No approval is pending for this run")
        pending = r["pending"] or []
        r["status"] = "running"
        r["pending"] = None
        
    who = _actor(request)                                    # user id (email)
    name = getattr(request.state, "user_name", who)
    tools = ", ".join(a.get("name", "?") for a in pending)
    
    # Update pending approvals in Supabase
    store.supabase.table("approvals").update({
        "decided_at": time.time(),
        "decided_by": who,
        "decision": req.approve,
        "comment": req.feedback
    }).eq("run_id", run_id).is_("decided_at", "null").execute()
    
    _add_event(r, {"type": "decision", "approve": req.approve, "by": who})
    store.save_run(r)
    store.audit(r["id"], r["ticket"], who,
                "approved" if req.approve else "rejected",
                f"tools={tools}; feedback={req.feedback}")
                
    # Add ServiceNow Work Note for the decision
    decision_text = "Approved" if req.approve else "Rejected"
    note = f"{decision_text} by {name} ({who}): {req.feedback or 'No comment provided.'}"
    try:
        add_work_note.invoke({"number": r["ticket"], "note": note})
    except Exception as e:
        log.warning("Could not post approval note to %s: %s", r["ticket"], str(e)[:200])

    threading.Thread(target=_consume, args=(r, resume(r["thread_id"], req.approve, req.feedback)),
                     kwargs={"kind": "resume"}, daemon=True).start()
    return {"ok": True}

class P1Request(BaseModel):
    approve: bool
    body: str = ""


@router.post("/runs/{run_id}/p1")
def p1_decision(run_id: str, req: P1Request, request: Request):
    r = RUNS.get(run_id)
    if not r or not r.get("p1"):
        raise HTTPException(404, "No P1 proposal for this run")
    p, who = r["p1"], _actor(request)
    if p["status"] in ("declared", "dismissed", "withdrawn"):
        raise HTTPException(409, f"Already {p['status']}")
    if not req.approve:
        p["status"] = "dismissed"
        store.audit(r["id"], r["ticket"], who, "p1_dismissed", p["reason"])
        return escalation.view(r)
    escalation.declare(r, who, (req.body or p["draft"]).strip()[:4000])
    return escalation.view(r)

@router.get("/audit")
def audit_log(limit: int = 100):
    return store.recent_audit(max(1, min(limit, 500)))


# ---- auto mode: poll the L1 queue and work new tickets one at a time ----
_auto = {"enabled": False, "thread": None, "tried": set()}


AUTO_MAX_FAILS = int(os.getenv("AUTO_MAX_FAILS", "3"))

_AUTO_RUNS: list = []          # run ids started from auto-intake, judged by the circuit breaker


def _auto_loop():
    fails = 0
    while _auto["enabled"]:
        try:
            for rid in list(_AUTO_RUNS):               # circuit breaker: judge finished auto runs
                lr = RUNS.get(rid)
                if lr and lr["status"] in ("running", "awaiting_approval"):
                    continue
                _AUTO_RUNS.remove(rid)
                if not lr:
                    continue
                bad = lr["status"] in ("error", "interrupted") or lr.get("outcome") == "incomplete"
                fails = fails + 1 if bad else 0
                if fails >= AUTO_MAX_FAILS:
                    _auto["enabled"] = False
                    store.set_setting("auto_enabled", False, "circuit-breaker")
                    store.audit(None, None, "auto-mode", "auto_mode_tripped", f"{fails} failed runs in a row")
                    log.error("Auto mode switched off: %d failed runs in a row", fails)
                    break
            if not _auto["enabled"]:
                break
            if len(jobs.queued()) < 5:                  # keep the backlog short; the queue is already ordered
                worked = {r["ticket"] for r in RUNS.values()} | {j["ticket"] for j in jobs.queued()}
                for n in list_queue():
                    if n not in worked:
                        jobs.enqueue(n, "auto", "auto-mode", 3)
        except Exception as e:
            log.warning("Auto-poll error: %s", str(e)[:200])
        time.sleep(POLL_SECONDS)

@router.get("/auto")
def get_auto():
    return {"enabled": _auto["enabled"]}


@router.post("/auto")
def set_auto(req: AutoRequest, request: Request):
    _auto["enabled"] = req.enabled
    store.set_setting("auto_enabled", req.enabled, _actor(request))
    store.audit(None, None, _actor(request), "auto_mode_on" if req.enabled else "auto_mode_off")
    if req.enabled and not (_auto["thread"] and _auto["thread"].is_alive()):
        _auto["thread"] = threading.Thread(target=_auto_loop, daemon=True)
        _auto["thread"].start()
    return {"enabled": _auto["enabled"]}


if store.get_setting("auto_enabled", False):          # restart-safe: resume intake on boot
    _auto["enabled"] = True
    _auto["thread"] = threading.Thread(target=_auto_loop, daemon=True)
    _auto["thread"].start()
    log.info("Auto mode resumed from saved setting")


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
    escalation.propose(r, "approval timed out")
    try:
        store.supabase.table("approvals").update({
            "decided_at": time.time(), "decided_by": "system", "decision": False,
            "comment": "Approval timed out"}).eq("run_id", r["id"]).is_("decided_at", "null").execute()
    except Exception as e:
        log.warning("Could not close timed-out approval for %s: %s", r["ticket"], str(e)[:200])
    threading.Thread(
        target=_consume,
        args=(r, resume(r["thread_id"], False, "Approval timed out: no engineer responded in time.")),
        kwargs={"kind": "resume"}, daemon=True).start()


def _fail_stalled(r: dict):
    mins = RUN_STALL_TIMEOUT // 60
    _add_event(r, {"type": "error",
                   "message": f"No progress for {mins} min (LLM or a tool may be hung). "
                              "The slot was released; check the ticket and re-run if needed."})
    r["status"] = "error"
    usage.unbind_run(r["id"])
    _finalize_metrics(r)
    store.save_run(r)
    store.audit(r["id"], r["ticket"], "system", "run_stalled", f"no progress for {mins} min")
    _post_report(r)


def cancel_run(run_id: str, who: str) -> str:
    """Admin cancel: frees the single run slot. Returns ok | missing | finished."""
    r = RUNS.get(run_id)
    if not r:
        return "missing"
    with _lock:
        if r["status"] not in ("running", "awaiting_approval"):
            return "finished"
        r["status"], r["pending"] = "error", None
    _add_event(r, {"type": "error", "message": f"Cancelled by {who}. The slot was released; check the ticket and re-run if needed."})
    usage.unbind_run(r["id"])
    _finalize_metrics(r)
    r["outcome"] = "cancelled"
    store.save_run(r)
    try:
        store.supabase.table("approvals").update({"decided_at": time.time(), "decided_by": who, "decision": False,
            "comment": "Run cancelled by admin"}).eq("run_id", r["id"]).is_("decided_at", "null").execute()
    except Exception as e:
        log.warning("Could not close approvals for cancelled run %s: %s", r["id"], str(e)[:200])
    store.audit(r["id"], r["ticket"], who, "run_cancelled")
    return "ok"


def _approval_timeout() -> int:
    return int(store.get_setting("approval_timeout_min", APPROVAL_TIMEOUT // 60)) * 60


def _watchdog():
    while True:
        time.sleep(30)
        try:
            now = time.time()
            for r in list(RUNS.values()):
                if r["status"] in ("running", "awaiting_approval") and now - r.get("started", now) > P1_BUDGET:
                    escalation.propose(r, f"time budget of {P1_BUDGET // 60} min exceeded")                
                idle = now - r.get("last_event", r.get("created", now))
                if r["status"] == "awaiting_approval" and idle > _approval_timeout():
                    _expire_approval(r)
                elif r["status"] == "running" and idle > RUN_STALL_TIMEOUT:
                    _fail_stalled(r)
        except Exception as e:
            log.warning("Watchdog error: %s", str(e)[:200])


threading.Thread(target=_watchdog, daemon=True, name="run-watchdog").start()


def _dispatcher():
    """Starts the best queued job whenever the single run slot is free."""
    while True:
        time.sleep(2)
        try:
            if _active():
                continue
            job = jobs.pick_next(jobs.queued())
            if not job:
                continue
            run = start_run(job["ticket"], source=job["source"], actor=job["actor"])
            if run:
                jobs.set_status(job["id"], "started")
                if job["source"] == "auto":
                    _AUTO_RUNS.append(run["id"])
        except Exception as e:
            log.warning("Dispatcher error: %s", str(e)[:200])


threading.Thread(target=_dispatcher, daemon=True, name="job-dispatcher").start()

# ---- agent status (for the Agent status pane on the Agent Console and the admin page) ----
_MODEL = {"at": 0.0, "ok": False}
_QUEUE = {"at": 0.0, "n": None}


def _model_online() -> bool:
    if time.time() - _MODEL["at"] < 15:
        return _MODEL["ok"]
    import requests
    try:
        base = os.getenv("COLAB_API_URL", "").rsplit("/", 1)[0]
        _MODEL["ok"] = requests.get(f"{base}/health", timeout=2).status_code == 200
    except Exception:
        _MODEL["ok"] = False
    _MODEL["at"] = time.time()
    return _MODEL["ok"]


def _queue_len():
    if time.time() - _QUEUE["at"] < 20:
        return _QUEUE["n"]
    try:
        _QUEUE["n"] = len(list_queue())
    except Exception:
        _QUEUE["n"] = None
    _QUEUE["at"] = time.time()
    return _QUEUE["n"]


@router.get("/status")
def agent_status():
    r = _active()
    model = _model_online()
    _q = _queue_len()
    out = {"state": "busy" if r else ("online" if model else "offline"),
           "model": model, "auto": bool(_auto["enabled"]),
           "ticket_mode": store.get_setting("ticket_mode", "confirm"),
           "autonomy": store.get_setting("autonomy_mode", "shadow"),
           "queue": _q, "queue_capped": _q == 10, "run": None, "last_run": None,
           "pending_approvals": sum(1 for x in list(RUNS.values()) if x["status"] == "awaiting_approval")}
    if r:
        out["run"] = {"ticket": r["ticket"], "server": r["server"], "status": r["status"],
                      "seconds": int(time.time() - r.get("started", time.time())), "source": r.get("source")}
    done = [x for x in list(RUNS.values()) if x["status"] not in ("running", "awaiting_approval")]
    if done:
        last = max(done, key=lambda x: x.get("ended") or x.get("started") or 0)
        out["last_run"] = {"ticket": last["ticket"], "outcome": last.get("outcome")}
    return out

@router.get("/llm-stats")
def llm_stats():
    """Aggregate LLM call stats for the admin bot performance panel."""
    now = time.time()
    cutoff_7d = now - 7 * 86400
    cutoff_24h = now - 86400
    try:
        rows = store.supabase.table("llm_calls") \
            .select("ts,kind,tokens_in,tokens_out,gen_s,wait_s") \
            .gte("ts", cutoff_7d).execute().data
    except Exception:
        return {"error": "unavailable"}
    if not rows:
        return {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                "avg_gen_s": None, "avg_wait_s": None, "by_kind": {}, "last_24h": 0}
    by_kind: dict = {}
    for r in rows:
        k = r["kind"] or "other"
        s = by_kind.setdefault(k, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "gen_s": 0.0})
        s["calls"] += 1
        s["tokens_in"] += r["tokens_in"] or 0
        s["tokens_out"] += r["tokens_out"] or 0
        s["gen_s"] += r["gen_s"] or 0.0
    for s in by_kind.values():
        s["avg_gen_s"] = round(s.pop("gen_s") / s["calls"], 2) if s["calls"] else None
    gen_times = [r["gen_s"] for r in rows if r.get("gen_s") is not None]
    wait_times = [r["wait_s"] for r in rows if r.get("wait_s") is not None]
    return {
        "calls": sum(s["calls"] for s in by_kind.values()),
        "tokens_in": sum(s["tokens_in"] for s in by_kind.values()),
        "tokens_out": sum(s["tokens_out"] for s in by_kind.values()),
        "avg_gen_s": round(sum(gen_times) / len(gen_times), 2) if gen_times else None,
        "avg_wait_s": round(sum(wait_times) / len(wait_times), 2) if wait_times else None,
        "by_kind": by_kind,
        "last_24h": sum(1 for r in rows if r["ts"] >= cutoff_24h),
    }

@router.get("/skills-status")
def skills_status():
    """KB source status for the admin Configuration panel."""
    from app.agent.skills import status as _sk_status
    return _sk_status()