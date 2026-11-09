"""Ad-hoc approver runs: an approver types an instruction against a named server,
TRIAGE-1 opens a ticket for it and runs the normal pipeline. Distinct from /api/issues:
no confirm/undo window, no rate limit/dedup meant for end users, and gated to console.act
(approvers), not chat.create (users) -- this capability must not be inherited by regular users.
"""
import re
import time

from fastapi import APIRouter, HTTPException, Request
from app import config
from pydantic import BaseModel, Field

from app import chat_actions, chat_reads, jobs, owner_approval, owner_contacts, store
from app.agent.hosts import resolve_default_windows, resolve_server
from app.api.agent import _actor, start_run
from app.api.issues import redact
from app.ports import registry
from app.ports.models import TicketState
from app.tools import actor, chat_tools, cmdb
from app.tools.host_lock import host_lock
from app.tools.infra import check_disk_usage
from app.tools.infra_perf import check_cpu_usage, check_memory_usage, set_audience, top_processes
from app.tools.snow import create_incident

router = APIRouter(prefix="/api/approver-chat")

# Read-only checks an approver can ask for directly, no ticket, no run, no approval gate --
# these never change anything. A mutating action (restart, kill) is NOT listed here: it must
# go through /run so it gets a ticket, a real pipeline run, and the normal approval gate for
# L1/L2-risk tools (policy.py), instead of bypassing approval as a bare tool call.
# Order matters: checked top to bottom, first match wins. "process"/"processes" must come before
# "cpu"/"memory" so a question like "which process is consuming the cpu" resolves to the process
# list (sorted by cpu), not a bare CPU reading -- both keywords can appear in the same question.
_INQUIRE_TOOLS = (
    ("process", top_processes),
    ("processes", top_processes),
    ("cpu", check_cpu_usage),
    ("memory", check_memory_usage),
    ("mem", check_memory_usage),
    ("disk", check_disk_usage),
    ("drive", check_disk_usage),
    ("storage", check_disk_usage),
)
_CMDB_KEYWORDS = ("cmdb", "owner", "support group", "criticality", "service context", "health check")


class AdhocRequest(BaseModel):
    instruction: str = Field(min_length=3, max_length=2000)


class InquireRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    server: str = Field(default="", max_length=100)


@router.post("/run")
def run_adhoc(req: AdhocRequest, request: Request):
    # only requests people are waiting on count; the automatic backlog must not lock approvers out
    if len([j for j in jobs.queued() if j.get("source") != "auto"]) >= config.get("limits.queue_max"):
        raise HTTPException(503, "TRIAGE-1 has a long queue right now. Please try again in a few minutes.")
    inst, reason = resolve_server(req.instruction)
    if not inst:
        raise HTTPException(422, reason or "I couldn't find a known server name in that instruction.")

    who = _actor(request)
    instr = redact(req.instruction.strip())
    try:
        ticket = create_incident.invoke({
            "short_description": f"[Approver ad-hoc] {inst['name']}: {instr[:70]}",
            "description": f"Affected server: {inst['name']}\n\nApprover-initiated instruction ({who}):\n{instr}",
            "urgency": 2,
            "impact": 2,
            "caller": who,
            "ci": inst["name"],
        })
    except Exception as e:
        raise HTTPException(502, f"Could not create the ticket: {str(e)[:150]}")
    number = ticket.get("number")
    if not number:
        raise HTTPException(502, ticket.get("error", "Could not create the ticket"))

    if not jobs.queued():
        run = start_run(number, source="approver_adhoc", actor=who)
        if run:
            return {"number": number, "run_id": run["id"], "queued": False, "position": 0}
    job, pos = jobs.enqueue(number, "approver_adhoc", who, 1)
    return {"number": number, "run_id": None, "queued": True, "position": pos}

@router.post("/inquire")
def inquire(req: InquireRequest, request: Request):
    """Read-only check against a real host: no ticket, no run, no PAM checkout. 'What is the CPU
    utilization on <server>' / 'what owns <server> in the CMDB' style questions. The server is
    identified the same way a real ticket is: resolve_server() / find_in_text() scan the question
    for a known CI name -- no client-side parsing of sentence structure."""
    q = req.question.lower()
    chat_tool = chat_reads.pick(req.question)           # uptime, network, events, sessions, updates, packages ...
    hint = chat_reads.source_hint(req.question) if chat_tool is chat_tools.check_network else ""

    instance, reason = resolve_server(req.question, ci_name=hint or req.server)
    if not instance:
        raise HTTPException(422, reason or "I couldn't find a known server name in that question.")

    if any(kw in q for kw in _CMDB_KEYWORDS):
        ctx = cmdb.service_context(instance["name"])
        if ctx.get("error"):
            raise HTTPException(502, f"CMDB lookup failed: {ctx['error']}")
        return {"server": instance["name"], "platform": instance["platform"], "result": ctx}

    calls = []                                          # (tool, arguments) pairs, run in order
    if chat_tool:
        tool = chat_reads.for_platform(chat_tool, instance["platform"])
        try:
            calls.append((tool, chat_reads.args(tool, req.question, instance["platform"], instance["name"])))
        except chat_reads.NeedsMore as e:
            raise HTTPException(422, str(e))
    else:
        tool = next((t for kw, t in _INQUIRE_TOOLS if kw in q), None)
        if not tool:
            raise HTTPException(422, "I can check CPU, memory, disk space, top processes, uptime, pending reboots, network "
                                     "reachability, recent errors, sessions, failed services, scheduled jobs, updates, packages "
                                     "or CMDB context directly. For anything that changes the server, ask for it as an action "
                                     "(for example: restart the spooler service).")
        tools = [tool]
        if tool in (check_cpu_usage, check_memory_usage, check_disk_usage):    # "CPU, memory and disk" asks for each reading
            tools = [t for t, kws in ((check_cpu_usage, ("cpu",)), (check_memory_usage, ("memory", "mem")),
                                      (check_disk_usage, ("disk", "drive", "storage")))
                     if any(re.search(r"\b" + k + r"\b", q) for k in kws)] or [tool]
        for t in tools:
            a = {"target": instance["platform"]}
            if t is top_processes:
                a["sort_by"] = "cpu" if "cpu" in q else "memory"
            calls.append((t, a))

    who = _actor(request)
    token = set_audience("l2")
    results = []
    try:
        with actor.inquiring_as(who):             # read-only credential path for tools without an inquiry_user argument
            for t, a in calls:
                if "inquiry_user" in t.args:
                    a["inquiry_user"] = who
                results.append(t.invoke(a))
    except Exception as e:
        raise HTTPException(502, f"Check failed: {str(e)[:200]}")
    finally:
        set_audience.__globals__["_AUDIENCE"].reset(token)

    return {"server": instance["name"], "platform": instance["platform"], "result": results[0], "results": results}

class ActRequest(BaseModel):
    instruction: str = Field(min_length=3, max_length=2000)
    server: str = Field(default="", max_length=100)


class ConfirmRequest(BaseModel):
    token: str = Field(min_length=8, max_length=200)


def _own_history(number: str, who: str) -> list:
    """What this person did on this ticket from the chat (audit rows written by act_confirm)."""
    rows = store.supabase.table("audit").select("action,detail").eq("ticket", number).eq("actor", who).execute().data
    return [r for r in rows if str(r.get("action", "")).startswith("chat_action_")]


def _propose_close(message: str, who: str) -> dict:
    number = chat_actions.ticket_in(message)
    tk = registry.get_ticketing()
    try:
        t = tk.get(number)
    except Exception:
        raise HTTPException(422, f"I could not find {number} in ServiceNow.")
    if t.state in (TicketState.RESOLVED, TicketState.CLOSED):
        raise HTTPException(422, f"{number} is already {t.state.value}.")
    queue = str(config.get("ticketing.assignment_group") or "").strip().lower()
    if t.group and t.group.strip().lower() == queue:
        raise HTTPException(422, f"{number} is in the agent queue ({t.group}), so the agent owns it. I will not close it from the chat.")
    history = _own_history(number, who)
    if not any(r["action"] == "chat_action_confirmed" for r in history):
        raise HTTPException(422, f"I can close tickets that you opened from this chat. {number} was not opened that way, "
                                 "so close it in ServiceNow for now.")
    done = [str(r.get("detail", "")).split(": ", 1)[-1] for r in history if r["action"] == "chat_action_done"]
    notes = f"Resolved from the TRIAGE-1 chat by {who}." + ("\nWhat was done: " + " ".join(done) if done else "")
    store.audit(None, number, who, "chat_close_proposed", "")
    return chat_actions.propose_close(who, number, notes)


def _confirm_close(action: dict, who: str) -> dict:
    number = action["ticket"]
    try:
        registry.get_ticketing().resolve(number, action["notes"])
    except Exception as e:
        store.audit(None, number, who, "chat_close_failed", str(e)[:300])
        return {"ticket": number, "ok": False, "message": f"Could not resolve {number}: {str(e)[:200]}"}
    store.audit(None, number, who, "chat_ticket_resolved", action["notes"][:300])
    return {"ticket": number, "ok": True, "message": f"Resolved {number}."}


@router.post("/act/propose")
def act_propose(req: ActRequest, request: Request):
    """Step 1 of a change from the chat: work out exactly one action and return a card. Changes nothing."""
    if chat_actions.close_intent(req.instruction):
        return _propose_close(req.instruction, _actor(request))
    who = _actor(request)

    def resolve(mode: str) -> dict:
        if mode == "windows":                       # AD commands run through the one running Windows server
            inst = resolve_default_windows()
            if not inst:
                raise chat_actions.AskBack("AD commands need exactly one running Windows server to run through, "
                                           "and I found none or more than one.")
            return inst
        inst, reason = resolve_server(req.instruction, ci_name=req.server)
        if not inst:
            raise chat_actions.AskBack(reason or "I couldn't find a known server name in that message.")
        return inst

    try:
        with actor.inquiring_as(who):               # reading a session list or a package dry run is read only
            card = chat_actions.propose(req.instruction, who, resolve)
    except chat_actions.AskBack as e:
        raise HTTPException(422, str(e))
    store.audit(None, None, who, "chat_action_proposed", f"tool={card['tool']} name={card['name']} host={card['host']}")
    return card


def _mail_owners_after(action: dict, inst: dict, who: str, number: str, text: str, lines: list) -> str:
    """Tell the server's owners what was done (after the fact). Never raises; the answer is one sentence for the ticket and chat."""
    oc = owner_contacts.contacts(inst["name"])
    if not oc["emails"]:
        return f"No owner was mailed: {oc['error']}"
    body = "\n".join([f"TRIAGE-1 made a change on {inst['name']} at the request of {who}.", "",
                      f"What: {action['label']}", f"Result: {text}", f"Ticket: {number}", ""] + lines[:12] +
                     ["", "This message is a notice. No reply is needed unless something looks wrong."])
    ok, detail = owner_contacts.send(oc["emails"], [who] if "@" in who else [], f"[TRIAGE-1] {action['label']} - done ({number})", body)
    store.audit(None, number, who, "chat_owner_mail_sent" if ok else "chat_owner_mail_failed",
                f"to={','.join(oc['emails'])} {'' if ok else detail}"[:300])
    return (f"Owners mailed: {', '.join(oc['emails'])}." if ok else f"The owner mail FAILED ({detail}). Tell the owners yourself.")


class OwnerRequestBody(BaseModel):
    token: str = Field(min_length=8, max_length=200)


class OwnerReplyBody(BaseModel):
    text: str = Field(min_length=20, max_length=20000)


@router.post("/act/owner-request")
def act_owner_request(req: OwnerRequestBody, request: Request):
    """The 'Email the owners' button: opens the engineer's ticket, stores the request and mails the owners from the CMDB."""
    who = _actor(request)
    try:
        action = chat_actions.redeem(req.token, who, stage="request")
    except ValueError as e:
        raise HTTPException(409, str(e))
    inst, reason = resolve_server(action["host"], ci_name=action["host"])
    if not inst:
        raise HTTPException(502, reason or "That server is no longer known.")
    if action["tool"] in ("reboot_server", "shutdown_server"):
        block = owner_approval.power_block(inst)
        if block:
            raise HTTPException(409, block)
    can, why = owner_contacts.can_address()
    if not can:
        raise HTTPException(409, f"The owners cannot be mailed: {why}.")
    oc = owner_contacts.contacts(inst["name"])
    if not oc["emails"]:
        raise HTTPException(409, f"I cannot find who to ask. {oc['error']}")
    try:
        ticket = registry.get_ticketing().create_for_user(
            f"[Chat action] awaiting owner approval: {action['label']}"[:160],
            f"Affected server: {inst['name']}\n\nRequested in the TRIAGE-1 chat by {who}:\n{redact(action['message'])}", who, 2, 2, inst["name"])
    except Exception as e:
        raise HTTPException(502, f"Could not open the ticket, so nothing was sent: {str(e)[:150]}")
    row = owner_approval.create_request(who, action, oc["emails"], ticket.id)
    subject, body = owner_approval.request_mail(row, who, action["message"])
    ok, detail = owner_contacts.send(oc["emails"], [who] if "@" in who else [], subject, body)
    tk = registry.get_ticketing()
    if not ok:
        owner_approval.mark(row["id"], status="cancelled")
        store.audit(None, ticket.id, who, "chat_owner_request_failed", detail[:250])
        try:
            tk.add_note(ticket.id, f"Owner approval request {row['id']} could NOT be sent: {detail}. Nothing was changed.")
        except Exception:
            pass
        raise HTTPException(502, f"The mail to the owners could not be sent ({detail}). Nothing was changed.")
    try:
        tk.add_note(ticket.id, f"Owner approval requested ({row['id']}) by {who} for: {action['label']}. Mailed to: {', '.join(oc['emails'])}.")
    except Exception:
        pass
    store.audit(None, ticket.id, who, "chat_owner_request_sent", f"{row['id']} to={','.join(oc['emails'])} {action['tool']} host={inst['name']}"[:300])
    return {"request_id": row["id"], "ticket": ticket.id, "owners": oc["emails"],
            "hours": int(config.get("owners.approval_ttl_hours"))}


@router.post("/act/owner-reply")
def act_owner_reply(req: OwnerReplyBody, request: Request):
    """The engineer pastes an owner's reply. If it is a clear approval from an owner, the normal Confirm card comes back."""
    who = _actor(request)
    code = owner_approval.code_in(req.text)
    if not code:
        raise HTTPException(422, "I cannot find a request code (like REQ-K7M2QX) in that text. Paste the whole reply, including the subject line.")
    row = owner_approval.get_request(code)
    if not row or row["requester"] != who:
        raise HTTPException(422, f"There is no open request {code} that you made.")
    if row["expires"] < time.time() and row["status"] in ("waiting", "approved"):
        owner_approval.mark(code, status="expired")
        raise HTTPException(409, f"{code} expired. Ask the owners again.")
    if row["status"] == "approved":                                # lost the card? the approval is still good
        return {"verdict": "approved", "request_id": code, "card": chat_actions.run_card(
            row, {"from": row.get("approved_by"), "top": row.get("approval_text")})}
    if row["status"] != "waiting":
        raise HTTPException(409, f"{code} is {row['status']}.")
    owners = row["owners_json"] if isinstance(row["owners_json"], list) else []
    ev = owner_approval.evaluate(req.text, owners)
    tk = registry.get_ticketing()
    try:
        tk.add_note(row["ticket"], f"Reply pasted by {who} for {code}: verdict {ev['verdict']} ({ev['why']}). From: {ev['from'] or 'unknown'}.\n"
                                   f"Reply text read:\n{ev['top'][:600]}")
    except Exception:
        pass
    store.audit(None, row["ticket"], who, "chat_owner_reply", f"{code} verdict={ev['verdict']} from={ev['from']}"[:300])
    if ev["verdict"] != "approved":
        if ev["verdict"] == "refused":
            owner_approval.mark(code, status="refused")
        return {"verdict": ev["verdict"], "request_id": code, "message": ev["why"]}
    owner_approval.mark(code, status="approved", approved_by=ev["from"], approved_at=time.time(), approval_text=ev["top"])
    row.update(status="approved")
    return {"verdict": "approved", "request_id": code, "card": chat_actions.run_card(row, ev)}


@router.post("/act/confirm")
def act_confirm(req: ConfirmRequest, request: Request):
    """Step 2: the engineer's Confirm. Opens a ticket owned by them (outside the L1 queue), runs the one action the card
    showed with a direct tool call (no agent run), writes the result to the ticket and the audit log."""
    who = _actor(request)
    try:
        action = chat_actions.redeem(req.token, who, stage=None)       # the request-for-owners card is refused without being used up
    except ValueError as e:
        raise HTTPException(409, str(e))
    if action.get("kind") == "close":
        return _confirm_close(action, who)
    if action["host_mode"] == "windows":
        inst, reason = resolve_default_windows(), "I no longer find exactly one running Windows server."
    else:
        inst, reason = resolve_server(action["host"], ci_name=action["host"])
    if not inst:
        raise HTTPException(502, reason or "That server is no longer known.")

    host = inst["name"]
    if action.get("stage") == "request":
        raise HTTPException(409, "That card is the request for the owners' approval. Use its Email the owners button.")
    if action.get("request_id"):                                   # owners approved by email: the ticket already exists
        row = owner_approval.get_request(action["request_id"])
        if not row or row["status"] != "approved" or row["expires"] < time.time():
            raise HTTPException(409, "That owner approval is no longer valid (used, refused or expired). Ask the owners again.")
        if action["tool"] in ("reboot_server", "shutdown_server"):
            block = owner_approval.power_block(inst)
            if block:
                raise HTTPException(409, block)
        number = action["ticket"]
    else:
        try:
            ticket = registry.get_ticketing().create_for_user(
                f"[Chat action] {action['label']}"[:160],
                f"Affected server: {host}\n\nRequested in the TRIAGE-1 chat by {who}:\n{redact(action['message'])}",
                who, 2, 2, host)
        except Exception as e:
            # no change without a record of it
            raise HTTPException(502, f"Could not open the ticket, so nothing was changed: {str(e)[:150]}")
        number = ticket.id
    store.audit(None, number, who, "chat_action_confirmed", f"tool={action['tool']} name={action['name']} host={host}")

    tool = chat_actions.tool_for(action)
    try:
        with host_lock(inst["instance_id"], timeout=30), actor.acting_as(who, number):
            result = tool.invoke(chat_actions.args_for(action))
    except Exception as e:
        result = {"error": str(e)[:200]}
    text = chat_actions.summary(action, result)
    ok = not result.get("error")
    if ok and action.get("request_id"):
        owner_approval.mark(action["request_id"], status="used")
    if ok and action["tool"] in ("reboot_server", "shutdown_server"):
        store.audit(None, number, who, "chat_power_scheduled", f"host={host} tool={action['tool']}")
        if action["tool"] == "reboot_server":                      # watch for it to come back and write that on the ticket
            owner_approval.start_verifier(host, inst["instance_id"], inst["platform"], number, who, int(result.get("delay_seconds") or 60))
    if ok and action["tool"] == "install_windows_updates":         # the install runs in the background: watch it and report on the ticket
        owner_approval.start_patch_watch(host, inst["instance_id"], inst["platform"], number, who, result.get("job"))
    lines = [str(x)[:200] for x in (result.get("lines") or [])][:12]
    owner_note = _mail_owners_after(action, inst, who, number, text, lines) if ok and action.get("owner_after") else ""
    note = (f"TRIAGE-1 chat action by {who}: {text}\nAction: {action['tool']} {chat_actions.args_for(action)} on {host}"
            + ("\n" + "\n".join(lines) if lines else "") + (f"\n{owner_note}" if owner_note else ""))
    try:
        registry.get_ticketing().add_note(number, note[:4000])
    except Exception:
        pass                                   # the audit row below still records it
    store.audit(None, number, who, "chat_action_done" if ok else "chat_action_failed", f"{action['tool']} {action['name']} {host}: {text[:200]}")
    return {"ticket": number, "ok": ok, "message": text, "owner_note": owner_note}


class CmdbRequest(BaseModel):
    server: str = Field(min_length=1, max_length=100)
    service: str = Field(default="", max_length=100)


@router.post("/cmdb")
def cmdb_lookup(req: CmdbRequest):
    """Read-only CMDB context for a server (and optionally a named service on it): criticality,
    owner, support group, health-check URL. No ticket, no run."""
    inst = next((h for h in registry.get_hosts().list_hosts() if h.name == req.server), None)
    if not inst:
        raise HTTPException(422, "Pick a configuration item (server) from the list.")
    ctx = cmdb.service_context(inst.name, req.service)
    if ctx.get("error"):
        raise HTTPException(502, f"CMDB lookup failed: {ctx['error']}")
    return ctx