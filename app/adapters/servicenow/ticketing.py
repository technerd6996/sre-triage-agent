"""ServiceNow Ticketing adapter. Wraps the existing app/tools/snow.py with no behaviour
change: every call goes through the same functions the agent uses today.
snow.py is imported lazily so this module loads without langchain installed."""
from __future__ import annotations

import re

from app.ports.errors import AuthFailed, NotFound, PortError, Unavailable, Unsupported
from app.ports.manifest import Field, Manifest
from app.ports.models import Ticket, TicketState
from app.ports.ticketing import CAP_CALLER_SEARCH, CAP_CREATE, CAP_REVIEW, CAP_SLA, CAP_WORK_NOTES

PROVIDER = "servicenow"
capabilities=frozenset({CAP_WORK_NOTES, CAP_CREATE, CAP_CALLER_SEARCH, CAP_SLA, CAP_REVIEW}),

REVIEWER_ACTOR = "triage-checker"
_CODE = {"in_progress": "2", "closed": "7", "canceled": "8"}

MANIFEST = Manifest(
    port="ticketing",
    provider=PROVIDER,
    title="ServiceNow",
    capabilities=frozenset({CAP_WORK_NOTES, CAP_CREATE, CAP_CALLER_SEARCH, CAP_SLA}),
    fields=(
        Field("instance", "Instance (name or URL)", required=True, live=False),
        Field("user", "Integration user", required=True),
        Field("password", "Integration password", kind="secret", required=True),
        Field("checker_user", "Checker user"),
        Field("checker_password", "Checker password", kind="secret"),
        Field("caller", "Default caller", default="sre_triage_agent"),
        Field("assignment_group", "L1 assignment group", default="L1-SRE-Agent"),
        Field("assignee", "Agent assignee", default="svc_triage_01"),
        Field("escalation_group", "L2 escalation group", required=True),
        Field("close_code", "Resolution code", default="Solution provided"),
        Field("channel", "Contact type", default="virtual_agent"),
    ),
)

# ServiceNow state labels (display_value) and numeric codes -> normalized state
_STATE = {
    "new": TicketState.NEW, "1": TicketState.NEW,
    "in progress": TicketState.IN_PROGRESS, "2": TicketState.IN_PROGRESS,
    "on hold": TicketState.ON_HOLD, "3": TicketState.ON_HOLD,
    "resolved": TicketState.RESOLVED, "6": TicketState.RESOLVED,
    "closed": TicketState.CLOSED, "7": TicketState.CLOSED,
    "canceled": TicketState.CLOSED, "cancelled": TicketState.CLOSED, "8": TicketState.CLOSED,
}
_CORE = {"number", "short_description", "description", "state", "priority", "caller_id",
         "assignment_group", "assigned_to", "work_notes", "cmdb_ci"}


def to_ticket(rec: dict) -> Ticket:
    raw_state = str(rec.get("state", "")).strip()
    state = _STATE.get(raw_state.lower(), TicketState.UNKNOWN)
    extras = {k: v for k, v in rec.items() if k not in _CORE}
    if state is TicketState.UNKNOWN:
        extras["raw_state"] = raw_state
    return Ticket(
        id=str(rec.get("number", "")),
        title=rec.get("short_description") or "",
        description=rec.get("description") or "",
        state=state,
        priority=str(rec.get("priority") or ""),
        group=rec.get("assignment_group") or "",
        assignee=rec.get("assigned_to") or "",
        caller=rec.get("caller_id") or "",
        host=rec.get("cmdb_ci") or "",
        work_notes=rec.get("work_notes") or "",
        provider=PROVIDER,
        extras=extras,
    )


def map_error(e: Exception) -> PortError:
    """Translate snow.py failures into the uniform port errors."""
    if isinstance(e, PortError):
        return e
    msg = str(e)
    low = msg.lower()
    if "snow unreachable" in low or re.search(r"snow (429|5\d\d) ", low):
        return Unavailable(msg)
    if re.search(r"snow (401|403) ", low):
        return AuthFailed(msg)
    if "not found" in low:
        return NotFound(msg)
    if "is not set" in low or "did not accept close_code" in low:
        return Unsupported(msg)
    if isinstance(e, KeyError) and e.args and str(e.args[0]).startswith("SNOW_"):
        return Unsupported(f"ServiceNow is not configured: {e.args[0]} is missing")
    return PortError(msg)


class ServiceNowTicketing:
    provider = PROVIDER
    capabilities = MANIFEST.capabilities

    @staticmethod
    def _snow():
        import importlib
        return importlib.import_module("app.adapters.servicenow.client")

    def _run(self, fn):
        try:
            return fn(self._snow())
        except Exception as e:                      # noqa: BLE001
            raise map_error(e) from e

    def list_queue(self) -> list[str]:
        return self._run(lambda s: s.list_queue())

    def get(self, ticket_id: str) -> Ticket:
        rec = self._run(lambda s: s._fetch(ticket_id))
        if "error" in rec:
            raise NotFound(rec["error"])
        return to_ticket(rec)

    def claim(self, ticket_id: str, note: str | None = None) -> Ticket:
        args = {"number": ticket_id}
        if note:
            args["work_note"] = note
        return to_ticket(self._run(lambda s: s.claim_incident.invoke(args)))

    def add_note(self, ticket_id: str, note: str) -> None:
        self._run(lambda s: s.add_work_note.invoke({"number": ticket_id, "note": note}))

    def resolve(self, ticket_id: str, close_notes: str) -> Ticket:
        return to_ticket(self._run(
            lambda s: s.resolve_incident.invoke({"number": ticket_id, "close_notes": close_notes})))

    def escalate(self, ticket_id: str, reason: str) -> Ticket:
        return to_ticket(self._run(
            lambda s: s.escalate_incident.invoke({"number": ticket_id, "reason": reason})))

    def create(self, title: str, description: str, urgency: int = 3, impact: int = 3,
               caller: str = "", host: str = "") -> Ticket:
        args = {"short_description": title, "description": description, "urgency": urgency,
                "impact": impact, "caller": caller, "ci": host}
        return to_ticket(self._run(lambda s: s.create_incident.invoke(args)))

    def search_callers(self, text: str, limit: int = 8) -> list[dict]:
        return self._run(lambda s: s.search_users(text, limit))

    def get_sla(self, ticket_id: str) -> dict:
        return self._run(lambda s: s.get_sla(ticket_id))