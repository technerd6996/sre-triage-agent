"""Human-readable descriptions of agent events, and the work note written to ServiceNow when a run ends."""
import json
import time

_CALL = {
    "claim_incident": "Claiming the ticket",
    "check_reachability": "Checking connectivity to the {target} server",
    "check_disk_usage": "Checking disk usage on the {target} server",
    "check_service_status": "Checking the status of {service}",
    "lookup_ad_user": "Looking up AD user {username}",
    "check_ec2_status": "Checking EC2 instance health",
    "check_security_group": "Checking security group rules",
    "get_cloudwatch_metric": "Reading CloudWatch metrics",
    "clear_temp_files": "Clearing temp files on the {target} server",
    "start_service": "Starting {service} on the {target} server",
    "unlock_ad_account": "Unlocking AD account {username}",
    "resolve_incident": "Resolving the ticket",
    "escalate_incident": "Escalating to a human engineer",
    "escalate_l3": "Notifying L3 support",
}
_QUIET = {"add_work_note", "get_incident"}  # bookkeeping calls are not shown as steps


def _fmt_call(name: str, args: dict | None) -> str:
    args = args or {}
    tpl = _CALL.get(name)
    if not tpl:
        return f"Running {name}"
    return tpl.format(target=args.get("target", "server"), service=args.get("service_name", "the service"),
                      username=args.get("username", "the user"))


def _j(content) -> dict:
    try:
        d = json.loads(content)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _yn(v) -> str:
    return "yes" if v else "no"


def _result(name: str, content) -> str | None:
    d = _j(content)
    if d.get("error"):
        return f"✗ Failed: {str(d['error'])[:160]}"
    if name == "check_reachability":
        return "✓ Server is reachable" if d.get("reachable") else "✗ Server is not reachable"
    if name == "check_disk_usage" and d.get("disks"):
        disks = ", ".join(f"{x.get('disk')} {x.get('used_pct')}%" for x in d["disks"])
        over = d.get("over_threshold")
        tail = f" (over the {d.get('threshold_pct')}% limit: {', '.join(over)})" if over else " (within limits)"
        return f"Disk usage: {disks}{tail}"
    if name in ("check_service_status", "start_service") and d.get("service"):
        return f"Service {d['service']} is {d.get('status')}"
    if name == "lookup_ad_user" and d.get("SamAccountName"):
        return (f"User {d['SamAccountName']}: enabled {_yn(d.get('Enabled'))}, "
                f"locked out {_yn(d.get('LockedOut'))}, password expired {_yn(d.get('PasswordExpired'))}")
    if name == "unlock_ad_account":
        return f"Account is locked out now: {_yn(d.get('LockedOut'))}"
    if name == "clear_temp_files":
        return (f"Freed {d['freed_mb']} MB of temp files" if "freed_mb" in d
                else f"Deleted {d.get('files_deleted', 0)} temp files")
    if name == "resolve_incident":
        return "✓ Ticket resolved"
    if name == "escalate_incident":
        return "↑ Ticket escalated to a human engineer"
    return None


def describe(ev: dict) -> str | None:
    """One plain-English line for an agent event, or None if it is not worth showing."""
    if ev.get("type") == "verification":
        return ev.get("text")
    t = ev.get("type")
    if t == "start":
        where = f" for {ev['server']}" if ev.get("server") else ""
        return f"Playbook selected: {ev.get('skill') or 'none'}{where}"
    if t == "tool_call":
        return None if ev.get("name") in _QUIET else _fmt_call(ev.get("name"), ev.get("args"))
    if t == "tool_result":
        return _result(ev.get("name"), ev.get("content"))
    if t == "approval_required":
        acts = "; ".join(_fmt_call(a.get("name"), a.get("args")) for a in ev.get("actions", []))
        return f"⏸ Waiting for an engineer to approve: {acts}"
    if t == "decision":
        who = ev.get("by", "an engineer")
        return f"✓ Approved by {who}" if ev.get("approve") else f"✗ Rejected by {who}"
    if t == "final":
        return str(ev.get("text", ""))[:300]
    if t == "error":
        return f"✗ Error: {str(ev.get('message', ''))[:200]}"
    if t == "warning":
        return f"⚠ {ev.get('message', '')}"
    return None


def _outcome(run: dict) -> str:
    last = None
    for e in run["events"]:
        if e.get("type") == "tool_call" and e.get("name") in ("resolve_incident", "escalate_incident"):
            last = e["name"]
    if any(e.get("type") == "verification" and e.get("verdict") == "fail" for e in run["events"]):
        return "ESCALATED to a human engineer (fix did not verify)"
    if last == "resolve_incident":
        return "RESOLVED by TRIAGE-1"
    if last == "escalate_incident":
        return "ESCALATED to a human engineer"
    return "INCOMPLETE (the run ended without resolving or escalating)"


def _clock(ts) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ts)) if ts else "--:--:--"


def _build_report_old(run: dict, full: bool = True) -> str:
    """The structured work note posted to ServiceNow when a run finishes.

    full=True (default): include the step-by-step list.
    full=False: short summary only — used when the checker will post the full consolidated note.
    """
    lines = ["TRIAGE-1 AUTOMATED TRIAGE REPORT", "",
             f"Ticket:   {run['ticket']}",
             f"Server:   {run.get('server') or 'unknown'}",
             f"Playbook: {run.get('skill') or 'none'}",
             f"Outcome:  {_outcome(run)}"]
    final = next((str(e.get("text", "")) for e in run["events"] if e.get("type") == "final"), "")
    if not full:
        if final:
            lines += ["", "Agent summary:", final.strip()[:800]]
        lines += ["", "(Full step list and independent verification in the next note from triage-checker.)"]
        return "\n".join(lines)
    lines += ["", "What happened (times in UTC):"]
    n = 0
    for e in run["events"]:
        if e.get("type") == "final":
            continue
        d = describe(e)
        if d:
            n += 1
            lines.append(f"  {n}. {_clock(e.get('ts'))}  {d}")
    if final:
        lines += ["", "Agent summary:", final.strip()[:800]]
    return "\n".join(lines)

_DIAG = {"check_reachability", "check_disk_usage", "check_service_status", "lookup_ad_user",
         "check_ec2_status", "check_security_group", "get_cloudwatch_metric"}
_ACTION = {"clear_temp_files", "start_service", "unlock_ad_account"}


def _span(run: dict) -> str:
    ts = [e["ts"] for e in run.get("events", []) if e.get("ts")]
    if len(ts) < 2:
        return ""
    secs = int(max(ts) - min(ts))
    d = f"{secs // 60} min {secs % 60} s" if secs >= 60 else f"{secs} s"
    return f"{d} ({_clock(min(ts))} to {_clock(max(ts))} UTC)"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _next_steps(outcome: str, checker_will_run: bool) -> str:
    if outcome.startswith("RESOLVED"):
        if checker_will_run:
            return ("An independent check will now verify the fix. The ticket is closed if it passes and "
                    "reopened if it does not. See the next note from triage-checker.")
        return "No further action is needed. If the problem comes back, reopen this ticket or raise a new one."
    if outcome.startswith("ESCALATED"):
        return ("A human engineer needs to take over this ticket. The reason is in the agent summary above "
                "and in the escalation note.")
    return "The run ended without resolving or escalating. Please review the timeline and take over manually."


def build_report(run: dict, full: bool = True) -> str:
    """The verbose, plain-English work note posted to ServiceNow when a run ends.

    full=True: includes the step-by-step timeline.
    full=False: same sections without the timeline, because the checker posts the consolidated note.
    """
    events = run.get("events", [])
    outcome = _outcome(run)
    server = run.get("server") or "unknown"
    skill = run.get("skill") or "none"
    final = next((str(e.get("text", "")).strip() for e in events if e.get("type") == "final"), "")
    findings, actions, problems = [], [], []
    last_decision, pending_call = None, None
    for e in events:
        t, name = e.get("type"), e.get("name")
        if t == "decision":
            last_decision = e
            if not e.get("approve"):
                actions.append(f"{_clock(e.get('ts'))}  The proposed change was rejected by {e.get('by', 'an engineer')}.")
        elif t == "tool_call" and name in _ACTION:
            pending_call = e
        elif t == "tool_result":
            res = _result(name, e.get("content"))
            if not res:
                continue
            if name in _DIAG:
                findings.append(res)
            elif name in _ACTION:
                args = dict((pending_call or {}).get("args") or {})
                if server != "unknown":
                    args["target"] = server
                line = f"{_clock(e.get('ts'))}  {_fmt_call(name, args)}. Result: {res}"
                if last_decision and last_decision.get("approve"):
                    line += f" (approved by {last_decision.get('by', 'an engineer')} at {_clock(last_decision.get('ts'))} UTC)"
                actions.append(line)
                pending_call = None
        elif t in ("error", "warning"):
            d = describe(e)
            if d:
                problems.append(d)

    where = f"on {server}" if server != "unknown" else "(server not identified)"
    how = f"using the {skill} playbook" if skill != "none" else "without a matching playbook"
    summary = [f"TRIAGE-1 worked {run.get('ticket', 'this ticket')} {where} {how}."]
    if findings:
        summary.append(f"It ran {_plural(len(findings), 'diagnostic check')} first.")
    done = sum(1 for a in actions if "was rejected" not in a)
    if done:
        summary.append(f"It then made {_plural(done, 'change')} on the server.")
    if len(actions) > done:
        summary.append("An engineer rejected a proposed change.")
    summary.append(f"Outcome: {outcome}.")

    checker_will_run = not full
    lines = ["TRIAGE-1 INCIDENT REPORT", "=" * 24, "",
             f"Ticket:    {run.get('ticket', '')}",
             f"Server:    {server}",
             f"Playbook:  {skill}",
             f"Outcome:   {outcome}"]
    if _span(run):
        lines.append(f"Duration:  {_span(run)}")
    lines += ["", "SUMMARY", " ".join(summary)]
    if final:
        lines += ["", "Agent's own summary:", final[:1200]]
    lines += ["", "WHAT TRIAGE-1 FOUND"]
    lines += [f"  - {f}" for f in findings] or ["  - No diagnostic results were recorded."]
    lines += ["", "WHAT TRIAGE-1 DID"]
    lines += [f"  - {a}" for a in actions] or ["  - No changes were made to any system."]
    ctx = run.get("service_context") or {}
    if ctx.get("found") and ctx.get("summary"):
        lines += ["", "SERVICE IMPACT (from the CMDB)", f"  {str(ctx['summary']).strip()[:800]}"]
    if problems:
        lines += ["", "PROBLEMS DURING THE RUN"] + [f"  - {p}" for p in problems]
    lines += ["", "NEXT STEPS", f"  {_next_steps(outcome, checker_will_run)}"]
    if full:
        lines += ["", "TIMELINE (times in UTC)"]
        n = 0
        for e in events:
            if e.get("type") == "final":
                continue
            d = describe(e)
            if d:
                n += 1
                lines.append(f"  {n}. {_clock(e.get('ts'))}  {d}")
    return "\n".join(lines)[:30000]

def outcome_code(run: dict) -> str:
    """'resolved', 'escalated' or 'incomplete', for the chat UI."""
    return {"RESOLVED": "resolved", "ESCALATED": "escalated"}.get(_outcome(run).split()[0], "incomplete")