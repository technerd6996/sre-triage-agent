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
    if last == "resolve_incident":
        return "RESOLVED by TRIAGE-1"
    if last == "escalate_incident":
        return "ESCALATED to a human engineer"
    return "INCOMPLETE (the run ended without resolving or escalating)"


def _clock(ts) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ts)) if ts else "--:--:--"


def build_report(run: dict) -> str:
    """The structured work note posted to ServiceNow when a run finishes."""
    lines = ["TRIAGE-1 AUTOMATED TRIAGE REPORT", "",
             f"Ticket:   {run['ticket']}",
             f"Server:   {run.get('server') or 'unknown'}",
             f"Playbook: {run.get('skill') or 'none'}",
             f"Outcome:  {_outcome(run)}", "",
             "What happened (times in UTC):"]
    n, final = 0, ""
    for e in run["events"]:
        if e.get("type") == "final":
            final = str(e.get("text", ""))
            continue
        d = describe(e)
        if d:
            n += 1
            lines.append(f"  {n}. {_clock(e.get('ts'))}  {d}")
    if final:
        lines += ["", "Agent summary:", final.strip()[:800]]
    return "\n".join(lines)

def outcome_code(run: dict) -> str:
    """'resolved', 'escalated' or 'incomplete', for the chat UI."""
    return {"RESOLVED": "resolved", "ESCALATED": "escalated"}.get(_outcome(run).split()[0], "incomplete")