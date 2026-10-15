"""ServiceNow tools for the triage agent.

Each function decorated with @tool is something the LangChain agent can call.
The docstring is what the LLM reads to decide when to use it, so keep it short.
"""
import os
import requests
from langchain_core.tools import tool

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


# ---- low-level helpers ----
def _request(method: str, path: str, **kwargs):
    """Single place that talks to SNOW. Raises with the response body on errors."""
    base = os.environ["SNOW_INSTANCE"].strip().rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}.service-now.com" if "." not in base else f"https://{base}"
    auth = (os.environ["SNOW_USER"], os.environ["SNOW_PASSWORD"])
    resp = requests.request(
        method,
        f"{base}/api/now/table/{path}",
        auth=auth,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=20,
        **kwargs,
    )
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

@tool
def create_incident(short_description: str, description: str, urgency: int = 3) -> dict:
    """Create a ServiceNow incident, assign it to the triage agent in the L1 group, and move it to
    In Progress. urgency: 1=high, 2=medium, 3=low. Returns the incident as it now stands."""
    created = _request(
        "POST",
        "incident",
        json={
            "short_description": short_description,
            "description": description,
            "urgency": str(urgency),
            "contact_type": os.getenv("SNOW_CHANNEL", "virtual_agent"),
            "caller_id": _user_id(_caller()),
            "assignment_group": _group_id(_group()),
            "assigned_to": _user_id(_assignee()),
        },
    )
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

# The agent will import this list in Phase 3
SNOW_TOOLS = [get_incident, create_incident, claim_incident, add_work_note, escalate_incident, resolve_incident]