"""Ad-hoc approver runs: an approver types an instruction against a named server,
TRIAGE-1 opens a ticket for it and runs the normal pipeline. Distinct from /api/issues:
no confirm/undo window, no rate limit/dedup meant for end users, and gated to console.act
(approvers), not chat.create (users) -- this capability must not be inherited by regular users.
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import jobs
from app.agent.hosts import resolve_server
from app.api.agent import _actor, start_run
from app.api.issues import redact
from app.ports import registry
from app.tools import cmdb
from app.tools.infra_perf import check_cpu_usage, check_memory_usage, set_audience, top_processes
from app.tools.snow import create_incident

router = APIRouter(prefix="/api/approver-chat")

QUEUE_MAX = 10
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
)
_CMDB_KEYWORDS = ("cmdb", "owner", "support group", "criticality", "service context", "health check")


class AdhocRequest(BaseModel):
    instruction: str = Field(min_length=3, max_length=2000)


class InquireRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    server: str = Field(default="", max_length=100)


@router.post("/run")
def run_adhoc(req: AdhocRequest, request: Request):
    if len(jobs.queued()) >= QUEUE_MAX:
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
        raise HTTPException(422, "I can check CPU, memory, top processes, or CMDB context "
                                 "directly. For anything that changes the server, raise it as "
                                 "an ad-hoc run instead.")

    who = _actor(request)
    token = set_audience("l2")
    try:
        args = {"target": instance["platform"], "inquiry_user": who}
        if tool is top_processes:
            args["sort_by"] = "cpu" if "cpu" in q else "memory"
        result = tool.invoke(args)
    except Exception as e:
        raise HTTPException(502, f"Check failed: {str(e)[:200]}")
    finally:
        set_audience.__globals__["_AUDIENCE"].reset(token)

    return {"server": instance["name"], "platform": instance["platform"], "result": result}

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