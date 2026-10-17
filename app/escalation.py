"""P1 declaration: proposal, approver sign-off, ServiceNow update, bridge link and one disruption notice."""
import logging
import os
import re
import smtplib
import time
import uuid
from email.message import EmailMessage
from email.utils import make_msgid

from app import store
from app.tools.snow import _request, _incident_id, add_work_note

log = logging.getLogger("triage.escalation")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
UPDATE_MIN = int(os.getenv("P1_UPDATE_MIN", "30"))


def conf() -> dict:
    if os.getenv("NOTICE_TRANSPORT", "smtp").lower() == "sns":
        return {"bridge": store.get_setting("bridge_link", "") or "",
                "to": ["All confirmed subscribers of the SNS topic"], "cc": []}
    return {"bridge": store.get_setting("bridge_link", "") or "",
            "to": store.get_setting("p1_notice_to", []) or [], "cc": store.get_setting("p1_notice_cc", []) or []}


def touches_critical(run: dict) -> bool:
    from app.agent.policy import _critical
    crit = _critical()
    return any(str((e.get("args") or {}).get("service_name", "")).lower() in crit
               for e in run.get("events", []) if e.get("type") == "tool_call")


def draft(run: dict) -> str:
    c = conf()
    started = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(run.get("started", time.time())))
    return (f"Incident {run['ticket']} has been declared a P1.\n\n"
            f"Affected server: {run.get('server') or 'see ticket'}\nStarted: {started}\n"
            f"Bridge: {c['bridge'] or '(not configured)'}\nNext update: within {UPDATE_MIN} minutes.\n\n"
            "This notice states confirmed facts only. Cause and ETA will follow once known.")


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
    """Picks the transport from NOTICE_TRANSPORT (smtp | ses | sns). Returns a message id."""
    mode = os.getenv("NOTICE_TRANSPORT", "smtp").lower()
    if mode == "ses":
        return _send_ses(to, cc, subject, body)
    if mode == "sns":
        return _send_sns(subject, body)
    return _send_smtp(to, cc, subject, body)


def _send_smtp(to: list, cc: list, subject: str, body: str) -> str:
    host = os.getenv("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP_HOST is not set in .env")
    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER", "")
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=20) as s:
        s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
        s.send_message(msg, to_addrs=to + cc)
    return msg["Message-ID"]


def _send_ses(to: list, cc: list, subject: str, body: str) -> str:
    import boto3
    sender = os.getenv("SES_FROM")
    if not sender:
        raise RuntimeError("SES_FROM is not set in .env")
    client = boto3.client("ses", region_name=os.getenv("AWS_REGION"))
    dest = {"ToAddresses": to}
    if cc:
        dest["CcAddresses"] = cc
    r = client.send_email(Source=sender, Destination=dest,
                          Message={"Subject": {"Data": subject[:200]}, "Body": {"Text": {"Data": body}}})
    return r["MessageId"]


def _send_sns(subject: str, body: str) -> str:
    """SNS delivers to the topic's subscribers, so the To and Cc lists on the card are not used."""
    import boto3
    topic = os.getenv("SNS_TOPIC_ARN")
    if not topic:
        raise RuntimeError("SNS_TOPIC_ARN is not set in .env")
    client = boto3.client("sns", region_name=os.getenv("AWS_REGION"))
    r = client.publish(TopicArn=topic, Subject=subject[:100], Message=body)
    return r["MessageId"]


def declare(run: dict, approver: str, body: str) -> None:
    """Idempotent: ServiceNow is updated once, the email is sent once (retries only when the send failed)."""
    p, c, inc = run["p1"], conf(), run["ticket"]
    p["draft"] = body
    if not p.get("snow_done"):
        _request("PATCH", f"incident/{_incident_id(inc)}", json={
            "impact": "1", "urgency": "1",
            "work_notes": f"P1 declared by {approver}. Reason: {p['reason']}. Bridge: {c['bridge'] or 'not configured'}"})
        p["snow_done"] = True
        store.audit(run["id"], inc, approver, "p1_declared", p["reason"])
    done = store.supabase.table("notices").select("id").eq("incident", inc).eq("type", "p1_declared").execute().data
    if done:
        p["status"], p["notice_error"] = "declared", ""
        return
    try:
        if not c["to"]:
            raise RuntimeError("No notice recipients configured (Admin page)")
        mid = _send(c["to"], c["cc"], f"[P1] {inc} - major incident declared", body)
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