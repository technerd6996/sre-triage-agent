"""Owner approval by email for actions that interrupt people (reboot, shutdown, package removal).

Flow: the chat emails the server's owners (from the CMDB) a request with a code like REQ-K7M2QX. The owner replies by email. The
engineer pastes that reply into the chat, and this module decides whether it is an approval:
  - the request code must be in the text and belong to THIS engineer's waiting, unexpired request;
  - only the top of the reply is read (quoted text and signatures below it are cut off, because our own words are quoted in every reply);
  - the From address in the pasted header must be one of the owner addresses (setting owners.require_sender_match);
  - an approval word must be present and no refusal, hold or condition word. Anything unclear goes back to the engineer.
A pasted email is EVIDENCE, not proof: the engineer could fabricate one. So the sender must match the CMDB owners, the pasted text is
stored on the ticket and in the audit log, and the owners are mailed again after the action so a fake approval is visible.

Also here: the guard that keeps TRIAGE-1 from rebooting or shutting down the server it runs on, the minimum gap between reboots of one
server, and the background check that a rebooted server came back."""
import logging
import re
import secrets
import socket
import threading
import time

from app import config

log = logging.getLogger("triage.owners")

config.register("owners.approval_ttl_hours", "Hours an owner approval request stays open", 24, "int", "notifications", lo=1, hi=168)
config.register("owners.approve_words", "Words that count as approval in an owner's reply",
                "approved, approve, go ahead, proceed, ok to proceed, ok to reboot, ok to shut down, ok to remove, okay to, you may, "
                "lgtm, yes, confirmed, agreed, no objection, no problem, fine by me",
                "text", "notifications", multiline=True, max_len=2000,
                help="Comma separated words or phrases, not case sensitive. Only the top of the reply is read.")
config.register("owners.refuse_words", "Words that make a reply a refusal or a hold",
                "not approved, do not, don't, dont, reject, rejected, denied, decline, hold off, hold on, wait, cancel, stop, postpone, "
                "not yet, cannot, can't, negative, no", "text", "notifications", multiline=True, max_len=2000,
                help="If any of these appears in the top of the reply, it is NOT accepted as approval.")
config.register("owners.condition_words", "Words that make an approval conditional (a person must read it)",
                "if, unless, until, after, but, however, provided, as long as, only when, once, before", "text", "notifications",
                multiline=True, max_len=2000, help="An approval with one of these is sent back to the engineer instead of being accepted.")
config.register("owners.require_sender_match", "Require the reply's From address to be an owner address", True, "bool", "notifications",
                help="On: the pasted email must include its From line and that address must be one the CMDB lists for the server.")
config.register("owners.reply_scan_lines", "Lines of the reply that are read for approval words", 3, "int", "notifications", lo=1, hi=20)
config.register("chat.never_power_hosts", "Servers the chat will never reboot or shut down (comma separated)", "", "text", "agent",
                help="The server TRIAGE-1 itself runs on is always protected, whatever is listed here.")
config.register("chat.reboot_delay_seconds", "Warning time before a chat reboot or shutdown (seconds)", 60, "int", "agent", lo=10, hi=600)
config.register("chat.reboot_min_interval_minutes", "Least time between two reboots or shutdowns of one server (minutes)", 60, "int", "agent",
                lo=0, hi=1440)
config.register("chat.patch_poll_seconds", "How often a running Windows update install is checked (seconds)", 120, "int", "agent", lo=30, hi=900,
                help="Each check fetches a credential, and one ticket may fetch at most 30 times, so keep this at 120 or more.")
config.register("chat.patch_wait_minutes", "How long to wait for a Windows update install (minutes)", 40, "int", "agent", lo=5, hi=120)
config.register("chat.reboot_verify_minutes", "How long to wait for a rebooted server to come back (minutes)", 15, "int", "agent", lo=2, hi=120)

_DDL = """
create table if not exists public.owner_requests (
  id text not null, ticket text, requester text, tool text, args_json jsonb, host text, host_mode text, label text,
  level integer, message text, owners_json jsonb, status text, created double precision, expires double precision,
  approved_by text, approved_at double precision, approval_text text,
  constraint owner_requests_pkey primary key (id));
create index if not exists idx_owner_requests_status on public.owner_requests (status, created desc);
"""
_ensured = False
_CODE = re.compile(r"\bREQ-[A-Z0-9]{6}\b")
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# ---------------------------------------------------------------- storage (tests replace these three)
def _ensure() -> None:
    global _ensured
    if _ensured:
        return
    try:
        from app.db import get_pool
        with get_pool().connection() as c:
            c.execute(_DDL)
        _ensured = True
    except Exception as e:                                         # noqa: BLE001  the start script also applies db/schema.sql
        log.warning("owner_requests table could not be created here: %s", str(e)[:150])


def _table():
    from app import store
    return store.supabase.table("owner_requests")


def _insert(row: dict) -> None:
    _ensure()
    _table().insert(row).execute()


def _get(req_id: str):
    _ensure()
    rows = _table().select("*").eq("id", req_id).execute().data
    return rows[0] if rows else None


def _update(req_id: str, **fields) -> None:
    _table().update(fields).eq("id", req_id).execute()


# ---------------------------------------------------------------- requests
def new_id() -> str:
    return "REQ-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


def create_request(user: str, action: dict, owners: list, ticket: str) -> dict:
    ttl = int(config.get("owners.approval_ttl_hours")) * 3600
    now = time.time()
    row = {"id": new_id(), "ticket": ticket, "requester": user, "tool": action["tool"], "args_json": action["args"],
           "host": action["host"], "host_mode": action["host_mode"], "label": action["label"], "level": action["level"],
           "message": action["message"], "owners_json": owners, "status": "waiting", "created": now, "expires": now + ttl,
           "approved_by": None, "approved_at": None, "approval_text": None}
    _insert(row)
    return row


def get_request(req_id: str):
    return _get(req_id)


def mark(req_id: str, **fields) -> None:
    _update(req_id, **fields)


def request_mail(req: dict, who: str, why: str) -> tuple:
    hours = int(config.get("owners.approval_ttl_hours"))
    subject = f"[TRIAGE-1 {req['id']}] Approval requested: {req['label']}"
    body = "\n".join([
        f"{who} asks for your approval before TRIAGE-1 does the following.", "",
        f"What:    {req['label']}", f"Server:  {req['host']}", f"Ticket:  {req['ticket']}",
        f"Reason:  {why[:300]}", f"Request: {req['id']} (valid for {hours} hours)", "",
        "To approve, reply to this email with a clear approval, for example: approved, go ahead.",
        "To refuse or hold it, reply with: not approved, hold off.",
        "Please keep the request code in the subject, and reply with a plain answer at the TOP of your message.", "",
        "Nothing happens until the engineer has your reply and confirms. You will be told again when it is done."])
    return subject, body


# ---------------------------------------------------------------- reading a pasted reply
_HDR = re.compile(r"^\s*\**\s*(from|to|cc|sent|date|subject)\s*:\s*(.*)$", re.I)
_CUT = (re.compile(r"^\s*>"), re.compile(r"^\s*-{2,}\s*(original|forwarded)\s+message", re.I), re.compile(r"^\s*_{8,}\s*$"),
        re.compile(r"^\s*\*?\s*from\s*:", re.I), re.compile(r"^\s*sent from my ", re.I))
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def split_email(text: str) -> dict:
    """{'from': address or '', 'top': the reply's own words, quoted text cut off}."""
    lines = (text or "").replace("\r", "").replace("’", "'").split("\n")
    i, hdr, from_addr = 0, 0, ""
    while i < len(lines) and not lines[i].strip():
        i += 1
    while i < len(lines):
        m = _HDR.match(lines[i])
        if not m:
            break
        if m.group(1).lower() == "from" and not from_addr:
            found = _EMAIL.findall(m.group(2))
            from_addr = found[0].lower() if found else ""
        hdr += 1
        i += 1
    body = lines[i:] if hdr else lines
    top = []
    for j, ln in enumerate(body):
        nxt = body[j + 1] if j + 1 < len(body) else ""
        if any(p.match(ln) for p in _CUT) or (re.match(r"^\s*on\s.+", ln, re.I) and ("wrote:" in ln.lower() or "wrote:" in nxt.lower())):
            break
        top.append(ln)
    return {"from": from_addr, "top": "\n".join(top).strip()}


def _words(setting: str) -> list:
    return [w.strip().lower().replace("’", "'") for w in str(config.get(setting) or "").split(",") if w.strip()]


def _hits(words: list, text: str) -> list:
    return [w for w in words if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", text)]


def code_in(text: str) -> str:
    m = _CODE.search(text or "")
    return m.group(0) if m else ""


def evaluate(text: str, owner_emails: list) -> dict:
    """{'verdict': approved|refused|unclear|no_sender|wrong_sender, 'why': str, 'from': addr, 'top': str}.
    The caller has already matched the request code. Never raises."""
    parts = split_email(text)
    n = int(config.get("owners.reply_scan_lines"))
    scan_lines = [ln for ln in parts["top"].split("\n") if ln.strip()][:n]
    scan = " ".join(scan_lines).lower().replace("’", "'")
    out = {"verdict": "unclear", "why": "", "from": parts["from"], "top": "\n".join(scan_lines)[:600]}
    if config.get("owners.require_sender_match"):
        if not parts["from"]:
            out.update(verdict="no_sender", why="I cannot see who sent this. Paste the whole email, including its From line.")
            return out
        if parts["from"] not in [e.lower() for e in owner_emails]:
            out.update(verdict="wrong_sender", why=f"The reply is from {parts['from']}, who is not one of the owners listed for this server "
                                                   f"({', '.join(owner_emails)}).")
            return out
    neutral = re.sub(r"\bno (objections?|problems?|issues?)\b", " ", scan)         # 'no objection' is an approval, not a refusal
    refusals = _hits(_words("owners.refuse_words"), neutral)
    if refusals:
        out.update(verdict="refused", why="The reply reads as a refusal or a hold (" + ", ".join(refusals[:3]) + "). Nothing will run.")
        return out
    approvals = _hits(_words("owners.approve_words"), scan)
    if not approvals:
        out.update(why="I cannot find a clear approval such as 'approved' or 'go ahead' at the top of the reply.")
        return out
    conditions = _hits(_words("owners.condition_words"), neutral)
    if conditions:
        out.update(why="The approval has a condition (" + ", ".join(conditions[:3]) + "). Ask the owner for a plain answer, or read it yourself.")
        return out
    out.update(verdict="approved", why="approved: " + ", ".join(approvals[:2]))
    return out


# ---------------------------------------------------------------- the machine TRIAGE-1 runs on, and reboot spacing
def _local_names() -> set:
    names = set()
    for fn in (socket.gethostname, socket.getfqdn):
        try:
            n = fn().lower()
            names |= {n, n.split(".")[0]}
        except Exception:                                          # noqa: BLE001
            pass
    return names


def _local_addresses() -> set:
    try:
        return set(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:                                              # noqa: BLE001
        return set()


def power_block(inst: dict) -> str:
    """Why this server may not be rebooted or shut down from the chat, or ''. The machine TRIAGE-1 runs on is always blocked."""
    listed = {x.strip().lower() for x in str(config.get("chat.never_power_hosts") or "").split(",") if x.strip()}
    mine = {str(inst.get("name", "")).lower(), str(inst.get("instance_id", "")).lower()}
    addrs = {a for a in (inst.get("private_ip"), inst.get("public_ip")) if a}
    if (mine & _local_names()) or (addrs & _local_addresses()):
        return (f"{inst.get('name')} is the server TRIAGE-1 itself runs on. Rebooting or shutting it down would stop TRIAGE-1 "
                "(and this chat), so it is never done from the chat.")
    if mine & listed:
        return f"{inst.get('name')} is on the list of servers the chat never reboots or shuts down (setting chat.never_power_hosts)."
    return ""


def recent_power_action(host: str) -> float:
    """Timestamp of the last reboot or shutdown scheduled from the chat for this server within the minimum gap, or 0."""
    gap = int(config.get("chat.reboot_min_interval_minutes")) * 60
    if gap <= 0:
        return 0.0
    try:
        from app import store
        rows = store.supabase.table("audit").select("ts,detail").eq("action", "chat_power_scheduled").gt("ts", time.time() - gap).execute().data
        hits = [r["ts"] for r in rows if f"host={host} " in (r.get("detail") or "") + " "]
        return max(hits) if hits else 0.0
    except Exception:                                              # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------- did it come back?
def _port_open(host_id: str, platform: str) -> bool:
    from app.tools import infra
    addr = registry_connect_address(host_id)
    with socket.create_connection((addr, infra._port(platform)), timeout=4):
        return True


def registry_connect_address(host_id: str) -> str:
    from app.ports import registry
    return registry.get_hosts().connect_address(host_id)


def _verify_worker(host: str, host_id: str, platform: str, ticket: str, user: str, started: float, delay: int, sleep=time.sleep) -> None:
    from app import store
    from app.ports import registry
    from app.tools import actor, active, chat_tools
    deadline = started + delay + int(config.get("chat.reboot_verify_minutes")) * 60
    sleep(delay + 20)                                              # let it go down first
    note = ""
    while time.time() < deadline:
        try:
            active.bind({platform: host_id})
            if _port_open(host_id, platform):
                with actor.acting_as(user, ticket):
                    up = chat_tools.check_uptime.invoke({"target": platform})
                secs = up.get("seconds")
                if secs is not None and secs < (time.time() - started) + 60:
                    note = f"{host} is back after about {int((time.time() - started) // 60)} min; {up.get('summary')}."
                    break
        except Exception as e:                                     # noqa: BLE001  down or not ready yet: keep waiting
            log.debug("verify %s: %s", host, str(e)[:100])
        sleep(30)
    note = note or f"{host} did not answer again within {config.get('chat.reboot_verify_minutes')} minutes. Check it."
    try:
        registry.get_ticketing().add_note(ticket, f"TRIAGE-1 reboot check: {note}")
    except Exception:                                              # noqa: BLE001
        pass
    store.audit(None, ticket, user, "chat_reboot_verified" if "is back" in note else "chat_reboot_unverified", note[:300])


def _patch_worker(host: str, host_id: str, platform: str, ticket: str, user: str, job: str, started: float, sleep=time.sleep) -> None:
    """Watch a Windows update install until it finishes, then write the result on the ticket and mail the owners."""
    from app import owner_contacts, store
    from app.ports import registry
    from app.tools import actor, active, chat_tools
    deadline = started + int(config.get("chat.patch_wait_minutes")) * 60
    result = None
    while time.time() < deadline:
        sleep(int(config.get("chat.patch_poll_seconds")))
        try:
            active.bind({platform: host_id})
            with actor.acting_as(user, ticket):
                res = chat_tools.check_patch_job.invoke({"target": platform, "job": job})
            if res.get("status") in ("done", "failed", "nothing"):
                result = res
                break
        except Exception as e:                                     # noqa: BLE001  keep waiting
            log.debug("patch watch %s: %s", host, str(e)[:100])
    if result:
        text = f"{result.get('summary')}\n" + "\n".join(result.get("lines") or [])
        action = "chat_patch_done" if result.get("status") == "done" else "chat_patch_failed"
    else:
        text = f"The update install on {host} (job {job}) did not finish within {config.get('chat.patch_wait_minutes')} minutes. Ask: patch job status on {host}."
        action = "chat_patch_unfinished"
    try:
        registry.get_ticketing().add_note(ticket, f"TRIAGE-1 patch check: {text}")
    except Exception:                                              # noqa: BLE001
        pass
    store.audit(None, ticket, user, action, text[:300])
    try:
        oc = owner_contacts.contacts(host)
        if oc["emails"]:
            owner_contacts.send(oc["emails"], [user] if "@" in user else [], f"[TRIAGE-1] Windows updates on {host}: result ({ticket})",
                                f"Result of the update install on {host}, started at the request of {user}.\n\n{text}\n\nTicket: {ticket}")
    except Exception:                                              # noqa: BLE001
        pass


def start_patch_watch(host: str, host_id: str, platform: str, ticket: str, user: str, job: str) -> threading.Thread:
    t = threading.Thread(target=_patch_worker, args=(host, host_id, platform, ticket, user, job, time.time()), daemon=True, name=f"patch-watch-{host}")
    t.start()
    return t


def start_verifier(host: str, host_id: str, platform: str, ticket: str, user: str, delay: int) -> threading.Thread:
    t = threading.Thread(target=_verify_worker, args=(host, host_id, platform, ticket, user, time.time(), delay),
                         daemon=True, name=f"reboot-verify-{host}")
    t.start()
    return t
