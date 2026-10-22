"""Agent-facing ticket tools. Same names, docstrings and return shapes as before;
every call now goes through the active Ticketing adapter."""
from langchain_core.tools import tool

from app.ports import registry
from app.ports.errors import NotFound

CLAIM_NOTE = ("TRIAGE-1 picked up this ticket and has started automated triage.\n\n"
              "What happens next: it chooses a playbook, runs read-only diagnostics, and asks a human to "
              "approve any change that carries risk. A full report is added to this ticket when it finishes.")

_KEYS = {"number": "id", "short_description": "title", "description": "description",
         "priority": "priority", "caller_id": "caller", "assignment_group": "group",
         "assigned_to": "assignee", "work_notes": "work_notes"}


def _tk():
    return registry.get_ticketing()


def _legacy(t) -> dict:
    """Old dict shape. ServiceNow tickets carry their raw record; other adapters get a mapped one."""
    if t.raw:
        return dict(t.raw)
    d = {k: getattr(t, a) for k, a in _KEYS.items()}
    d["state"] = t.extras.get("raw_state") or t.state.value.replace("_", " ").title()
    return d


@tool
def get_incident(number: str) -> dict:
    """Fetch a ServiceNow incident by number (e.g. INC0010001). Returns its key fields."""
    try:
        return _legacy(_tk().get(number))
    except NotFound as e:
        return {"error": str(e)}


@tool
def create_incident(short_description: str, description: str, urgency: int = 3, impact: int = 3,
                    caller: str = "", ci: str = "") -> dict:
    """Create a ServiceNow incident, assign it to the triage agent in the L1 group, and move it to
    In Progress. urgency and impact: 1=high, 2=medium, 3=low. caller is a ServiceNow user_name
    (defaults to the integration user). ci is the configuration item (server) name.
    Returns the incident as it now stands."""
    return _legacy(_tk().create(short_description, description, urgency, impact, caller, ci))


@tool
def claim_incident(number: str, work_note: str = CLAIM_NOTE) -> dict:
    """Take ownership of an existing incident: assign it to the triage agent and move it to In Progress.
    Use this first when a ticket arrives in the L1 queue."""
    return _legacy(_tk().claim(number, work_note))


@tool
def resolve_incident(number: str, close_notes: str) -> dict:
    """Resolve an incident. close_notes must describe what was fixed. Only call after the underlying issue is fixed."""
    return _legacy(_tk().resolve(number, close_notes))


@tool
def add_work_note(number: str, note: str) -> dict:
    """Add a work note to an incident. Use it to record checks done, findings, and actions taken."""
    _tk().add_note(number, note)
    return {"number": number, "note_added": True}


@tool
def escalate_incident(number: str, reason: str) -> dict:
    """Hand an incident to the human L2 team when it is beyond L1, could not be fixed, or needs approval.
    Unassigns the agent and records the reason. The L2 team is fixed by configuration: do not pass a group."""
    return _legacy(_tk().escalate(number, reason))


SNOW_TOOLS = [get_incident, create_incident, claim_incident, add_work_note, escalate_incident, resolve_incident]