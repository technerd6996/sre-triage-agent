"""Ad-hoc approver runs: an approver types an instruction against a named server,
TRIAGE-1 opens a ticket for it and runs the normal pipeline. Distinct from /api/issues:
no confirm/undo window, no rate limit/dedup meant for end users, and gated to console.act
(approvers), not chat.create (users) -- this capability must not be inherited by regular users.
"""
import re

from fastapi import APIRouter, HTTPException, Request
from app import config
from pydantic import BaseModel, Field

from app import chat_actions, jobs, store
from app.agent.hosts import resolve_default_windows, resolve_server
from app.api.agent import _actor, start_run
from app.api.issues import redact
from app.ports import registry
from app.ports.models import TicketState
from app.tools import actor, cmdb
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

    instance, reason = resolve_server(req.question, ci_name=req.server)
    if not instance:
        raise HTTPException(422, reason or "I couldn't find a known server name in that question.")

    if any(kw in q for kw in _CMDB_KEYWORDS):
        ctx = cmdb.service_context(instance["name"])
        if ctx.get("error"):
            raise HTTPException(502, f"CMDB lookup failed: {ctx['error']}")
        return {"server": instance["name"], "platform": instance["platform"], "result": ctx}

    tool = next((t for kw, t in _INQUIRE_TOOLS if kw in q), None)
    if not tool:
        raise HTTPException(422, "I can check CPU, memory, disk space, top processes, or CMDB context "
                                 "directly. For anything that changes the server, ask for it as an action "
                                 "(for example: restart the spooler service).")
    tools = [tool]
    if tool in (check_cpu_usage, check_memory_usage, check_disk_usage):    # "CPU, memory and disk" asks for each reading
        tools = [t for t, kws in ((check_cpu_usage, ("cpu",)), (check_memory_usage, ("memory", "mem")),
                                  (check_disk_usage, ("disk", "drive", "storage")))
                 if any(re.search(r"\b" + k + r"\b", q) for k in kws)] or [tool]

    who = _actor(request)
    token = set_audience("l2")
    results = []
    try:
        with actor.inquiring_as(who):             # read-only credential path for tools without an inquiry_user argument
            for t in tools:
                args = {"target": instance["platform"]}
                if "inquiry_user" in t.args:
                    args["inquiry_user"] = who
                if t is top_processes:
                    args["sort_by"] = "cpu" if "cpu" in q else "memory"
                results.append(t.invoke(args))
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
        card = chat_actions.propose(req.instruction, who, resolve)
    except chat_actions.AskBack as e:
        raise HTTPException(422, str(e))
    store.audit(None, None, who, "chat_action_proposed", f"tool={card['tool']} name={card['name']} host={card['host']}")
    return card


@router.post("/act/confirm")
def act_confirm(req: ConfirmRequest, request: Request):
    """Step 2: the engineer's Confirm. Opens a ticket owned by them (outside the L1 queue), runs the one action the card
    showed with a direct tool call (no agent run), writes the result to the ticket and the audit log."""
    who = _actor(request)
    try:
        action = chat_actions.redeem(req.token, who)
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
    try:
        registry.get_ticketing().add_note(number, f"TRIAGE-1 chat action by {who}: {text}")
    except Exception:
        pass                                   # the audit row below still records it
    store.audit(None, number, who, "chat_action_done" if ok else "chat_action_failed", f"{action['tool']} {action['name']} {host}: {text[:200]}")
    return {"ticket": number, "ok": ok, "message": text}


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