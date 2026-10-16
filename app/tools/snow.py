"""ServiceNow tools for the triage agent.

Each function decorated with @tool is something the LangChain agent can call.
The docstring is what the LLM reads to decide when to use it, so keep it short.
"""
import os
import requests
from langchain_core.tools import tool
import time
from datetime import datetime, timezone

STATE_IN_PROGRESS = 2  # "WIP"
STATE_RESOLVED = 6

# Only return these fields so we don't waste tokens on the 80+ others
FIELDS = "number,short_description,description,state,priority,caller_id,assignment_group,assigned_to,contact_type,work_notes"


# ---- config (read at call time so load_dotenv() has already run) ----
def _caller() -> str:
    return os.getenv("SNOW_CALLER", "sre_triage_agent")


def _group() -> str:
    return os.getenv("SNOW_ASSIGNMENT_GROUP", "L1-SRE-Agent")


def _assignee() -> str:
    return os.getenv("SNOW_ASSIGNEE", "svc_triage_01")


RETRY_METHODS = {"GET", "PATCH", "PUT"}  # POST (create) is never retried: it could duplicate a ticket


def _request(method: str, path: str, **kwargs):
    """Single place that talks to SNOW. Retries transient failures on safe methods.
    Raises with the response body on errors."""
    base = os.environ["SNOW_INSTANCE"].strip().rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}.service-now.com" if "." not in base else f"https://{base}"
    auth = (os.environ["SNOW_USER"], os.environ["SNOW_PASSWORD"])
    attempts = 3 if method.upper() in RETRY_METHODS else 1
    resp = None
    for i in range(attempts):
        try:
            resp = requests.request(
                method,
                f"{base}/api/now/table/{path}",
                auth=auth,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=(5, 20),
                **kwargs,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            if i == attempts - 1:
                raise RuntimeError(f"SNOW unreachable: {str(e)[:200]}")
            time.sleep(2 ** i)
            continue
        if resp.status_code in (429, 502, 503, 504) and i < attempts - 1:
            time.sleep(2 ** i)
            continue
        break
    if not resp.ok:
        raise RuntimeError(f"SNOW {resp.status_code} on {method} {path}: {resp.text[:500]}")
    return resp.json()["result"]

def _lookup_sys_id(table: str, query: str, label: str) -> str:
    rows = _request(
        "GET", table, params={"sysparm_query": query, "sysparm_limit": 1, "sysparm_fields": "sys_id"}
    )
    if not rows:
        raise ValueError(f"{label} not found in SNOW")
    return rows[0]["sys_id"]


def _user_id(user_name: str) -> str:
    return _lookup_sys_id("sys_user", f"user_name={user_name}", f"User '{user_name}'")


def _group_id(name: str) -> str:
    return _lookup_sys_id("sys_user_group", f"name={name}", f"Group '{name}'")


def _incident_id(number: str) -> str:
    return _lookup_sys_id("incident", f"number={number}", f"Incident {number}")


def _flatten(record: dict) -> dict:
    """Reference fields come back as {'display_value':..., 'link':...}. Keep just the readable value."""
    return {k: (v.get("display_value") if isinstance(v, dict) else v) for k, v in record.items()}


def _fetch(number: str) -> dict:
    rows = _request(
        "GET",
        "incident",
        params={
            "sysparm_query": f"number={number}",
            "sysparm_limit": 1,
            "sysparm_fields": FIELDS,
            "sysparm_display_value": "true",
        },
    )
    if not rows:
        return {"error": f"Incident {number} not found"}
    return _flatten(rows[0])


# ---- tools the agent can call ----
@tool
def get_incident(number: str) -> dict:
    """Fetch a ServiceNow incident by number (e.g. INC0010001). Returns its key fields."""
    return _fetch(number)

def search_users(text: str, limit: int = 8) -> list[dict]:
    """Type-ahead for the Caller field: active users matching name, user name or email."""
    text = text.replace("^", " ").strip()[:50]
    rows = _request("GET", "sys_user", params={
        "sysparm_query": f"active=true^nameLIKE{text}^ORuser_nameLIKE{text}^ORemailLIKE{text}",
        "sysparm_limit": limit,
        "sysparm_fields": "user_name,name,email",
    })
    return [{"user_name": r.get("user_name", ""), "name": r.get("name", ""), "email": r.get("email", "")}
            for r in rows]


def find_ci_id(name: str) -> str | None:
    """sys_id of the CMDB configuration item with this name, or None if there is none."""
    try:
        rows = _request("GET", "cmdb_ci", params={
            "sysparm_query": f"name={name.replace('^', ' ')}", "sysparm_limit": 1, "sysparm_fields": "sys_id"})
        return rows[0]["sys_id"] if rows else None
    except Exception:
        return None


@tool
def create_incident(short_description: str, description: str, urgency: int = 3, impact: int = 3,
                    caller: str = "", ci: str = "") -> dict:
    """Create a ServiceNow incident, assign it to the triage agent in the L1 group, and move it to
    In Progress. urgency and impact: 1=high, 2=medium, 3=low. caller is a ServiceNow user_name
    (defaults to the integration user). ci is the configuration item (server) name.
    Returns the incident as it now stands."""
    body = {
        "short_description": short_description,
        "description": description,
        "urgency": str(urgency),
        "impact": str(impact),
        "contact_type": os.getenv("SNOW_CHANNEL", "virtual_agent"),
        "caller_id": _user_id(caller or _caller()),
        "assignment_group": _group_id(_group()),
        "assigned_to": _user_id(_assignee()),
    }
    if ci:
        ci_id = find_ci_id(ci)
        if ci_id:
            body["cmdb_ci"] = ci_id
    created = _request("POST", "incident", json=body)
    # Assignment is done. Now move to WIP as a separate step.
    _request(
        "PATCH",
        f"incident/{created['sys_id']}",
        json={"state": str(STATE_IN_PROGRESS), "work_notes": "Assigned to triage agent. Work started."},
    )
    return _fetch(created["number"])
    # Assignment is done. Now move to WIP as a separate step.
    _request(
        "PATCH",
        f"incident/{created['sys_id']}",
        json={"state": str(STATE_IN_PROGRESS), "work_notes": "Assigned to triage agent. Work started."},
    )
    return _fetch(created["number"])

@tool
def claim_incident(number: str, work_note: str = "Triage agent picked up this ticket.") -> dict:
    """Take ownership of an existing incident: assign it to the triage agent and move it to In Progress.
    Use this first when a ticket arrives in the L1 queue."""
    sys_id = _incident_id(number)
    _request("PATCH", f"incident/{sys_id}", json={"assigned_to": _user_id(_assignee())})
    _request(
        "PATCH",
        f"incident/{sys_id}",
        json={"state": str(STATE_IN_PROGRESS), "work_notes": work_note},
    )
    return _fetch(number)


@tool
def resolve_incident(number: str, close_notes: str) -> dict:
    """Resolve an incident. close_notes must describe what was fixed. Only call after the underlying issue is fixed."""
    sys_id = _incident_id(number)
    close_code = os.getenv("SNOW_CLOSE_CODE", "Solution provided")

    # Save the resolution fields first, then change the state.
    saved = _request(
        "PATCH",
        f"incident/{sys_id}",
        params={"sysparm_fields": "close_code"},
        json={"close_code": close_code, "close_notes": close_notes},
    )
    # SNOW silently ignores invalid choice values, so check the write really stuck.
    if not saved.get("close_code"):
        raise RuntimeError(
            f"SNOW did not accept close_code '{close_code}'. "
            "Use a value from the Resolution code dropdown and set SNOW_CLOSE_CODE."
        )

    _request("PATCH", f"incident/{sys_id}", json={"state": str(STATE_RESOLVED)})
    return _fetch(number)

@tool
def add_work_note(number: str, note: str) -> dict:
    """Add a work note to an incident. Use it to record checks done, findings, and actions taken."""
    _request("PATCH", f"incident/{_incident_id(number)}", json={"work_notes": note})
    return {"number": number, "note_added": True}

@tool
def escalate_incident(number: str, reason: str) -> dict:
    """Hand an incident to the human L2 team when it is beyond L1, could not be fixed, or needs approval.
    Unassigns the agent and records the reason. The L2 team is fixed by configuration: do not pass a group."""
    group = os.getenv("SNOW_ESCALATION_GROUP", "")
    if not group:
        raise RuntimeError("SNOW_ESCALATION_GROUP is not set in .env")
    _request(
        "PATCH",
        f"incident/{_incident_id(number)}",
        json={
            "assignment_group": _group_id(group),
            "assigned_to": "",
            "work_notes": f"Escalated by triage agent. Reason: {reason}",
        },
    )
    return _fetch(number)

def list_queue() -> list:
    """Ticket numbers waiting in the L1 queue: New, unassigned, oldest first."""
    rows = _request(
        "GET",
        "incident",
        params={
            "sysparm_query": f"active=true^state=1^assigned_toISEMPTY"
                             f"^assignment_group.name={_group()}^ORDERBYsys_created_on",
            "sysparm_fields": "number",
            "sysparm_limit": 10,
        },
    )
    return [r["number"] for r in rows]

# Used only when ServiceNow has no SLA records on the ticket. (response_min, resolution_min) by priority.
# These numbers are placeholders. Replace them with your real SLAs.
SLA_FALLBACK = {"1": (15, 240), "2": (30, 480), "3": (120, 1440), "4": (240, 4320)}


def _human(minutes: float) -> str:
    m = max(int(round(minutes)), 1)
    if m < 60:
        return f"{m} minute{'s' if m != 1 else ''}"
    h, rem = divmod(m, 60)
    if h < 24:
        return f"{h} hour{'s' if h != 1 else ''}" + (f" {rem} minutes" if rem else "")
    d, h = divmod(h, 24)
    return f"{d} day{'s' if d != 1 else ''}" + (f" {h} hours" if h else "")


def get_sla(number: str) -> dict:
    """Response and resolution targets as readable text. Reads the ticket's task_sla records
    (SLA names containing 'response' / 'resolution'); falls back to SLA_FALLBACK by priority."""
    out = {"response": None, "resolution": None, "source": "servicenow"}
    try:
        rows = _request("GET", "task_sla", params={
            "sysparm_query": f"task={_incident_id(number)}",
            "sysparm_fields": "sla.name,planned_end_time",
            "sysparm_limit": 10,
        })
        now = datetime.now(timezone.utc)
        for r in rows:
            name = str(r.get("sla.name", "")).lower()
            end = r.get("planned_end_time")
            if not end:
                continue
            due = datetime.strptime(end, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if due <= now:
                continue
            text = _human((due - now).total_seconds() / 60)
            if "response" in name and not out["response"]:
                out["response"] = text
            elif "resolution" in name and not out["resolution"]:
                out["resolution"] = text
    except Exception:
        pass
    if not (out["response"] and out["resolution"]):
        try:
            prio = (_fetch(number).get("priority") or "3")[:1]
        except Exception:
            prio = "3"
        resp_m, reso_m = SLA_FALLBACK.get(prio, SLA_FALLBACK["3"])
        out["response"] = out["response"] or _human(resp_m)
        out["resolution"] = out["resolution"] or _human(reso_m)
        out["source"] = "fallback"
    return out

# The agent will import this list in Phase 3
SNOW_TOOLS = [get_incident, create_incident, claim_incident, add_work_note, escalate_incident, resolve_incident]