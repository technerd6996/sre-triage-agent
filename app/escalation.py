"""P1 declaration: proposal, approver sign-off, ServiceNow update, bridge link and one disruption notice."""
import logging
import os
import re
import time
import uuid

from app import store
from app.ports import registry
from app.ports.errors import PortError
from app.ports.notifications import CAP_BROADCAST
from app.tools.snow import add_work_note

log = logging.getLogger("triage.escalation")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
UPDATE_MIN = int(os.getenv("P1_UPDATE_MIN", "30"))


def _broadcast() -> bool:
    try:
        return CAP_BROADCAST in registry.get_notifications().capabilities
    except PortError:                      # unknown transport: show the recipient lists, the send will report it
        return False


def conf() -> dict:
    if _broadcast():
        return {"bridge": store.get_setting("bridge_link", "") or "",
                "to": ["All confirmed subscribers of the SNS topic"], "cc": []}
    return {"bridge": store.get_setting("bridge_link", "") or "",
            "to": store.get_setting("p1_notice_to", []) or [], "cc": store.get_setting("p1_notice_cc", []) or []}


def touches_critical(run: dict) -> bool:
    """True if any service this run touched is marked critical in the CMDB, for this run's host."""
    from app.ports import registry                            # lazy import avoids a circular import
    host = str(run.get("server") or "").split(" (")[0]
    ctx = registry.get_context()
    seen = set()
    for e in run.get("events", []):
        if e.get("type") != "tool_call":
            continue
        svc = str((e.get("args") or {}).get("service_name", "")).lower()
        if not svc or svc in seen:
            continue
        seen.add(svc)
        is_crit, _ = ctx.is_critical(host, svc)
        if is_crit:
            return True
    return False


def _services(run: dict) -> list:
    """Service names the agent touched in this run (for the notice), in order, without repeats."""
    names = []
    for e in run.get("events", []):
        n = (e.get("args") or {}).get("service_name") if e.get("type") == "tool_call" else None
        if n and str(n).lower() not in names:
            names.append(str(n).lower())
    return names


def draft(run: dict) -> str:
    c = conf()
    detected = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(run.get("started", time.time())))
    affected = run.get("server") or "see ticket"
    svc = ", ".join(_services(run))
    if svc:
        affected += f" ({svc})"
    bridge = c["bridge"] or "The bridge link will follow in the next update."
    ctx = run.get("service_context") or {}
    svc_line = ""
    if ctx.get("found"):
        svc_line = "  Services  : " + ", ".join(s["name"] for s in ctx["services"]) + "\n"
    return (
        f"P1 INCIDENT DECLARED  |  {run['ticket']}\n"
        f"{'=' * 58}\n\n"
        "WHAT HAPPENED\n"
        "  A priority 1 incident has been declared and the on-call\n"
        "  team is working on it.\n\n"
        "DETAILS\n"
        f"  Incident  : {run['ticket']}\n"
        f"  Affected  : {affected}\n{svc_line}"
        f"  Detected  : {detected}\n"
        "  Priority  : P1 - Critical\n"
        "  Status    : Under investigation\n\n"
        "JOIN THE BRIDGE\n"
        f"  {bridge}\n\n"
        "NEXT UPDATE\n"
        f"  Within {UPDATE_MIN} minutes of this notice.\n\n"
        f"{'-' * 58}\n"
        "This notice states confirmed facts only. The cause and\n"
        "the estimated time to fix will follow once known.\n"
        "Sent by TRIAGE-1 on behalf of the SRE team."
    )


def propose(run: dict, reason: str) -> None:
    if run.get("p1") or not touches_critical(run):
        return
    run["p1"] = {"status": "proposed", "reason": reason, "draft": draft(run), "at": time.time()}
    store.audit(run["id"], run["ticket"], "system", "p1_proposed", reason)


def view(run: dict) -> dict | None:
    p = run.get("p1")
    if not p:
        return None
    c = conf()
    return {"status": p["status"], "reason": p["reason"], "draft": p["draft"], "to": c["to"], "cc": c["cc"],
            "bridge": c["bridge"], "notice_error": p.get("notice_error", "")}


def _send(to: list, cc: list, subject: str, body: str) -> str:
    """Sends through the active notifications adapter (smtp | ses | sns). Returns a message id."""
    return registry.get_notifications().send(to, cc, subject, body)


def declare(run: dict, approver: str, body: str) -> None:
    """Idempotent: ServiceNow is updated once, the email is sent once (retries only when the send failed)."""
    p, c, inc = run["p1"], conf(), run["ticket"]
    p["draft"] = body
    if not p.get("snow_done"):
        registry.get_ticketing().set_severity(
            inc, 1, 1,
            f"P1 declared by {approver}. Reason: {p['reason']}. Bridge: {c['bridge'] or 'not configured'}")
        p["snow_done"] = True
        store.audit(run["id"], inc, approver, "p1_declared", p["reason"])
    done = store.supabase.table("notices").select("id").eq("incident", inc).eq("type", "p1_declared").execute().data
    if done:
        p["status"], p["notice_error"] = "declared", ""
        return
    try:
        if not c["to"]:
            raise RuntimeError("No notice recipients configured (Admin page)")
        mid = _send(c["to"], c["cc"], f"[P1] {inc} - Service disruption on {run.get('server') or 'production'}", body)
        store.supabase.table("notices").insert({
            "id": uuid.uuid4().hex[:12], "incident": inc, "run_id": run["id"], "type": "p1_declared",
            "sent_at": time.time(), "approver": approver, "message_id": mid,
            "recipients_json": {"to": c["to"], "cc": c["cc"]}, "body": body}).execute()
        add_work_note.invoke({"number": inc, "note": f"Disruption notice emailed to {', '.join(c['to'] + c['cc'])}."})
        store.audit(run["id"], inc, approver, "p1_notice_sent", ", ".join(c["to"]))
        p["status"], p["notice_error"] = "declared", ""
    except Exception as e:
        p["status"], p["notice_error"] = "declared_notice_failed", str(e)[:200]
        store.audit(run["id"], inc, approver, "p1_notice_failed", p["notice_error"])