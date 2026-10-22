"""Compatibility shim. Old imports keep working while callers move to the port."""
from app.adapters.servicenow.client import (  # noqa: F401
    FIELDS, STATE_IN_PROGRESS, STATE_RESOLVED, SLA_FALLBACK,
    _request, _lookup_sys_id, _user_id, _group_id, _incident_id, _flatten, _fetch,
    _caller, _group, _assignee, _human, find_ci_id,
)
from app.ports import registry
from app.tools.ticketing_tools import (  # noqa: F401
    CLAIM_NOTE, SNOW_TOOLS, get_incident, create_incident, claim_incident,
    add_work_note, escalate_incident, resolve_incident,
)


def list_queue() -> list:
    return registry.get_ticketing().list_queue()


def search_users(text: str, limit: int = 8) -> list[dict]:
    return registry.get_ticketing().search_callers(text, limit)


def get_sla(number: str) -> dict:
    return registry.get_ticketing().get_sla(number)