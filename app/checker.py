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

from app import config, store
from app.agent.report import outcome_code
from app.ports import registry
from app.ports.models import TicketState

log = logging.getLogger("triage.checker")
ACTOR = "triage-checker"


def mode() -> str:
    return config.get("agent.checker_mode")


def _soak() -> float:
    return config.get("agent.verify_soak_seconds")


# ---- the ticket system, as the checker identity (through the Ticketing port) ----
def _tk():
    return registry.get_ticketing(role="reviewer")


# (nothing: these four helpers are removed; the logic now lives in the ServiceNow adapter)

# ---- the ticket system, as the checker identity (through the Ticketing port) ----
def _tk():
    return registry.get_ticketing(role="reviewer")


# ---- what to verify, taken from what the agent actually did ----


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
    elif skill in ("linux-high-memory", "windows-high-memory"):
        a = _last_call(run, "check_memory_usage")
        return "memory", {"target": a.get("target") or ("linux" if skill.startswith("linux") else "windows")}
    elif skill in ("linux-high-cpu", "windows-high-cpu"):
        a = _last_call(run, "check_cpu_usage")
        return "cpu", {"target": a.get("target") or ("linux" if skill.startswith("linux") else "windows")}
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
    if kind in ("memory", "cpu"):
        from app.tools.infra_perf import check_cpu_usage, check_memory_usage
        r = (check_memory_usage if kind == "memory" else check_cpu_usage).invoke(args)
        if r.get("error"):
            return None, f"could not check {kind} on {args['target']}: {r['error']}"
        val = r.get("used_pct") if kind == "memory" else r.get("cpu_pct")
        return not r.get("over_threshold"), f"{kind} on {args['target']}: {val}% (limit {r.get('threshold_pct')}%)"
    return None, f"unknown check kind {kind}"


def _run_checks(kind: str, args: dict):
    """Services are checked twice, VERIFY_SOAK_SECONDS apart, to catch a service that comes up and dies again."""
    notes = []
    for i in range(2 if kind in ("service", "cpu") else 1):
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
    plain = {"pass": "The fix was independently verified and holds.",
             "fail": "The independent check FAILED, so the problem is not fixed. A human engineer is needed.",
             }.get(str(verdict).lower(), "The independent check could not be completed. Please verify manually.")
    lines = [f"{prefix}TRIAGE-1 RESOLUTION REPORT (checker)", "", f"RESULT: {plain}", "",
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


def _run_llm_checker(run: dict):
    from app.agent.agent import deploy_agent
    tools = ("check_reachability", "check_disk_usage", "check_service_status", "lookup_ad_user", "get_cloudwatch_metric")
    agent, config = deploy_agent("checker", run["ticket"], tools)
    
    case_file = f"Ticket: {run['ticket']}\nServer: {run.get('server')}\nActions taken:\n"
    for e in run.get("events", []):
        if e.get("type") == "tool_call":
            case_file += f"- Called {e.get('name')} with {e.get('args')}\n"
        elif e.get("type") == "tool_result":
            case_file += f"  Result: {str(e.get('content'))[:200]}\n"
    
    payload = {"messages": [{"role": "user", "content": f"CASE FILE:\n{case_file}\n\nPlease verify the fix."}]}
    try:
        res = agent.invoke(payload, config)
        text = res["messages"][-1].content
        verdict = "pass" if "VERDICT: PASS" in text else ("fail" if "VERDICT: FAIL" in text else "unverified")
        return verdict, text
    except Exception as e:
        return "error", str(e)

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
    tk = _tk()
    try:
        state = tk.get(ticket).state
    except Exception as e:
        _audit(run, "verify_error", str(e)[:300])
        return None
    if state is not TicketState.RESOLVED:
        _audit(run, "verify_skipped", f"ticket state is {state.value}, not Resolved")
        return None
        
    emit("Checker: verifying the fix independently")
    ok, notes = _run_checks(kind, args)
    
    # P8 Multi-Agent Pipeline: Deploy LLM Checker in shadow mode next to code checker
    llm_verdict, llm_notes = _run_llm_checker(run) if config.get("agent.llm_checker") else ("off", "")
    code_verdict = "pass" if ok is True else ("fail" if ok is False else "unverified")
    
    if llm_verdict != code_verdict:
        _audit(run, "checker_disagreement", f"code={code_verdict} llm={llm_verdict} llm_notes={llm_notes[:200]}")
    else:
        _audit(run, "checker_agreement", f"verdict={code_verdict}")
        
    evidence = "; ".join(notes)
    reopen_count, closed = 0, False
    try:
        if ok is True:
            verdict = "pass"
            comment = ("Fix confirmed by an independent re-check. "
                       + ("Moving the ticket from Resolved to Closed." if md == "on"
                          else "Would move the ticket from Resolved to Closed."))
            report = _report(run, kind, notes, verdict, comment, shadow)
            if md == "on":
                tk.close(ticket, report)
                closed = True
            else:
                tk.add_note(ticket, report)
        elif ok is False:
            verdict = "fail"
            reopen_count = _reopens(ticket) + 1 if md == "on" else 0
            comment = ("The fix did not hold: " + evidence + ". "
                       + ("Reopening the ticket and assigning it to the escalation group." if md == "on"
                          else "Would reopen the ticket and assign it to the escalation group."))
            report = _report(run, kind, notes, verdict, comment, shadow)
            if md == "on":
                tk.reopen(ticket, report, group=os.getenv("SNOW_ESCALATION_GROUP", "").strip())
            else:
                tk.add_note(ticket, report)
        else:
            verdict = "unverified"
            comment = ("The checker could not verify the fix (" + evidence + "). "
                       "The ticket is left in Resolved for a person to confirm.")
            tk.add_note(ticket, _report(run, kind, notes, verdict, comment, shadow))
    except Exception as e:
        _audit(run, "verify_error", str(e)[:300])
        log.warning("Checker failed for %s: %s", ticket, str(e)[:200])
        emit(f"Checker could not complete: {str(e)[:150]}")
        try:   # best effort: a person reading the ticket must not assume it was verified
            tk.add_note(ticket, f"{shadow}{ACTOR}: could not complete the verification of this fix "
                                f"({str(e)[:150]}). The ticket was not closed. Please confirm the fix by hand.")
        except Exception:
            pass
        return None

    # Record first, so a failure in the follow-ups below can never lose the verification row.
    _record(run, md, verdict, notes, reopen_count, closed)
    _audit(run, f"verify_{verdict}", f"mode={md}; {evidence}"[:500])
    emit(f"Checker verdict: {verdict.upper()}. {evidence}", tag + verdict)

    if verdict == "fail" and md == "on":
        try:
            from app import escalation                      # lazy: avoids a circular import
            escalation.propose(run, f"Verification failed after the fix: {evidence}")
        except Exception as e:
            log.warning("P1 proposal failed for %s: %s", ticket, str(e)[:200])
        if reopen_count >= 2:
            try:
                from app.tools.aws_tools import escalate_l3
                escalate_l3.invoke({"number": ticket, "reason":
                                    f"Fix failed verification {reopen_count} times: {evidence}"[:300]})
            except Exception as e:
                log.warning("L3 notify failed for %s: %s", ticket, str(e)[:200])
    return tag + verdict