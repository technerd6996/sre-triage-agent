"""P5 checker: independent, deterministic verification of tickets TRIAGE-1 resolved.

Maker-checker: the checker uses its own ServiceNow account (SNOW_CHECKER_USER) and only read-only
infra tools. No LLM is involved: pass or fail is a rule applied to a fresh check.

verify_run() is called from app/api/agent.py::_consume while the run still holds the active slot,
because the infra tools find the server through aws.ACTIVE and the next run would clear it.

CHECKER_MODE: off | shadow (default: verify and write a work note, change nothing) | on (close or reopen).
"""
import logging
import os
import time
import uuid

from app import store
from app.agent.report import outcome_code
from app.tools.snow import _request

log = logging.getLogger("triage.checker")
ACTOR = "triage-checker"
STATE_RESOLVED, STATE_IN_PROGRESS, STATE_CLOSED = "6", "2", "7"


def mode() -> str:
    m = os.getenv("CHECKER_MODE", "shadow").strip().lower()
    return m if m in ("off", "shadow", "on") else "shadow"


def _soak() -> float:
    return float(os.getenv("VERIFY_SOAK_SECONDS", "60"))


# ---- ServiceNow, as the checker identity ----
def _snow(method: str, path: str, **kw):
    return _request(method, path, identity="checker", **kw)


def _sys_id(table: str, query: str) -> str:
    rows = _snow("GET", table, params={"sysparm_query": query, "sysparm_limit": 1, "sysparm_fields": "sys_id"})
    if not rows:
        raise ValueError(f"Not found in ServiceNow: {table} {query}")
    return rows[0]["sys_id"]


def _ticket(number: str) -> tuple[str, str]:
    """(sys_id, raw state code) read with the checker's own account."""
    rows = _snow("GET", "incident", params={
        "sysparm_query": f"number={number}", "sysparm_limit": 1,
        "sysparm_fields": "sys_id,state", "sysparm_display_value": "false"})
    if not rows:
        raise ValueError(f"Incident {number} not found")
    return rows[0]["sys_id"], str(rows[0]["state"])


def _note(sys_id: str, text: str, **fields) -> None:
    _snow("PATCH", f"incident/{sys_id}", json={**fields, "work_notes": text[:8000]})


def _set_state(number: str, sys_id: str, state: str, text: str, **fields) -> None:
    """Changes the state and re-reads it: ServiceNow can silently ignore a write the account may not make."""
    _note(sys_id, text, state=state, **fields)
    _, now = _ticket(number)
    if now != state:
        try:
            _note(sys_id, f"{ACTOR}: ServiceNow did not accept the change to state {state}; the ticket is still "
                          f"{now}. Disregard the note above.")
        except Exception:
            pass
        raise RuntimeError(f"ServiceNow ignored the state change (still {now}). Check the checker's roles and ACLs.")


# ---- what to verify, taken from what the agent actually did ----
def _last_call(run: dict, *names: str) -> dict:
    for e in reversed(run.get("events", [])):
        if e.get("type") == "tool_call" and e.get("name") in names:
            return e.get("args") or {}
    return {}


def _spec(run: dict):
    """(kind, args) for the skill this run used, or None when there is nothing to verify."""
    skill = run.get("skill") or ""
    if skill in ("linux-service-down", "windows-service-down"):
        a = _last_call(run, "start_service", "check_service_status")
        if a.get("service_name") and a.get("target"):
            return "service", {"target": a["target"], "service_name": a["service_name"]}
    elif skill in ("linux-disk-full", "windows-disk-full"):
        a = _last_call(run, "check_disk_usage")
        return "disk", {"target": a.get("target") or ("linux" if skill.startswith("linux") else "windows")}
    elif skill == "ad-account-locked":
        a = _last_call(run, "lookup_ad_user", "unlock_ad_account")
        if a.get("username"):
            return "ad", {"username": a["username"]}
    return None


# ---- the checks: (True | False | None = could not check, evidence text) ----
def _check(kind: str, args: dict):
    from app.tools.infra import check_disk_usage, check_service_status, lookup_ad_user   # lazy: heavy imports
    if kind == "service":
        r = check_service_status.invoke(args)
        if r.get("error"):
            return None, f"could not check {args['service_name']}: {r['error']}"
        return r.get("status") == "active", f"{args['service_name']} on {args['target']} is {r.get('status')}"
    if kind == "disk":
        r = check_disk_usage.invoke(args)
        if r.get("error"):
            return None, f"could not check disk on {args['target']}: {r['error']}"
        disks = ", ".join(f"{d.get('disk')} {d.get('used_pct')}%" for d in r.get("disks", []))
        return not r.get("over_threshold"), f"disk on {args['target']}: {disks} (limit {r.get('threshold_pct')}%)"
    if kind == "ad":
        r = lookup_ad_user.invoke(args)
        if r.get("error"):
            return None, f"could not look up {args['username']}: {r['error']}"
        ok = bool(r.get("Enabled")) and not r.get("LockedOut")
        return ok, f"AD user {args['username']}: enabled={bool(r.get('Enabled'))}, locked out={bool(r.get('LockedOut'))}"
    return None, f"unknown check kind {kind}"


def _run_checks(kind: str, args: dict):
    """Services are checked twice, VERIFY_SOAK_SECONDS apart, to catch a service that comes up and dies again."""
    notes = []
    for i in range(2 if kind == "service" else 1):
        if i:
            time.sleep(_soak())
        ok, text = _check(kind, args)
        notes.append(text)
        if ok is not True:
            return ok, notes
    return True, notes


# ---- bookkeeping ----
def _audit(run: dict, action: str, detail: str = "") -> None:
    try:
        store.audit(run["id"], run["ticket"], ACTOR, action, detail)
    except Exception as e:
        log.warning("checker audit failed: %s", str(e)[:200])


def _record(run: dict, md: str, verdict: str, notes: list, reopen_count: int, closed: bool) -> None:
    try:
        store.supabase.table("verifications").insert({
            "id": uuid.uuid4().hex[:12], "ticket": run["ticket"], "run_id": run["id"], "ts": time.time(),
            "mode": md, "verdict": verdict, "checks_json": notes, "reopen_count": reopen_count,
            "closed_at": time.time() if closed else None}).execute()
    except Exception as e:
        log.warning("could not save verification for %s: %s", run["ticket"], str(e)[:200])


def _reopens(ticket: str) -> int:
    try:
        rows = (store.supabase.table("verifications").select("id").eq("ticket", ticket)
                .eq("verdict", "fail").eq("mode", "on").execute().data)
        return len(rows or [])
    except Exception:
        return 0


# ---- the consolidated report ----
def _report(run: dict, kind: str, notes: list, verdict: str, comment: str, prefix: str = "") -> str:
    """One work note: what TRIAGE-1 ran, what the checker ran, the checker's comment and the verdict."""
    from app.agent.report import _clock, describe          # same wording the run report and UI use
    lines = [f"{prefix}TRIAGE-1 RESOLUTION REPORT (checker)", "",
             f"Ticket:   {run['ticket']}", f"Server:   {run.get('server') or 'unknown'}",
             f"Playbook: {run.get('skill') or 'none'}", "",
             "1. Actions by the TRIAGE-1 agent (times UTC):"]
    n, final = 0, ""
    for e in run.get("events", []):
        if e.get("type") == "verification":
            continue
        if e.get("type") == "final":
            final = str(e.get("text", "")).strip()
            continue
        d = describe(e)
        if d:
            n += 1
            lines.append(f"   {n}. {_clock(e.get('ts'))}  {d}")
    if not n:
        lines.append("   (no steps recorded)")
    if final:
        lines += ["", f"   Agent summary: {final[:400]}"]
    lines += ["", f"2. Independent checks by {ACTOR} ({kind}):"]
    for i, t in enumerate(notes, 1):
        lines.append(f"   Check {i}" + (f" (after {_soak():g}s)" if i > 1 else "") + f": {t}")
    lines += ["", f"3. Checker comment: {comment}", f"   Verdict: {verdict.upper()}"]
    return "\n".join(lines)


# ---- entry point ----
def verify_run(run: dict, emit=lambda text, verdict=None: None):
    """Verifies a run that ended 'resolved'. Returns 'pass', 'fail', 'unverified' or None (nothing done).
    emit(text, verdict) adds a step to the run. In shadow mode the verdict is prefixed 'shadow-'."""
    md = mode()
    if md == "off" or outcome_code(run) != "resolved":
        return None
    spec = _spec(run)
    if not spec:
        _audit(run, "verify_skipped", f"no verification spec for skill {run.get('skill')}")
        return None
    kind, args = spec
    ticket = run["ticket"]
    tag = "" if md == "on" else "shadow-"
    shadow = "" if md == "on" else "[shadow] "
    try:
        sys_id, state = _ticket(ticket)
    except Exception as e:
        _audit(run, "verify_error", str(e)[:300])
        return None
    if state != STATE_RESOLVED:
        _audit(run, "verify_skipped", f"ticket state is {state}, not Resolved")
        return None
    emit("Checker: verifying the fix independently")
    ok, notes = _run_checks(kind, args)
    evidence = "; ".join(notes)
    try:
        if ok is True:
            verdict = "pass"
            comment = ("Fix confirmed by an independent re-check. "
                       + ("Moving the ticket from Resolved to Closed." if md == "on"
                          else "Would move the ticket fromd