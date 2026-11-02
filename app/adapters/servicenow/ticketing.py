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

REVIEWER_ACTOR = "triage-checker"
_CODE = {"in_progress": "2", "closed": "7", "canceled": "8"}

MANIFEST = Manifest(
    port="ticketing",
    provider=PROVIDER,
    title="ServiceNow",
        capabilities=frozenset({CAP_WORK_NOTES, CAP_CREATE, CAP_CALLER_SEARCH, CAP_SLA, CAP_REVIEW}),
    fields=(
        Field("instance", "Instance (name or URL)", required=True),
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

    def __init__(self, role: str = "agent"):
        # "agent" = the integration identity; "reviewer" = the independent checker identity.
        self.role = role

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
        rec = self._run(lambda s: s._fetch(ticket_id, identity="checker")
                        if self.role == "reviewer" else s._fetch(ticket_id))
        if "error" in rec:
            raise NotFound(rec["error"])
        return to_ticket(rec)

    def claim(self, ticket_id: str, note: str | None = None) -> Ticket:
        args = {"number": ticket_id}
        if note:
            args["work_note"] = note
        return to_ticket(self._run(lambda s: s.claim_incident.invoke(args)))

    def add_note(self, ticket_id: str, note: str) -> None:
        if self.role == "reviewer":              # the checker writes as its own account
            self._run(lambda s: s._request(
                "PATCH", f"incident/{self._sys_id(s, ticket_id)}", identity="checker",
                json={"work_notes": note[:8000]}))
            return
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

    @property
    def _identity(self) -> str:
        return "checker" if self.role == "reviewer" else "agent"

    @staticmethod
    def _q(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9@._ -]", "", text or "")     # keeps ^ and other query operators out

    def ping(self) -> None:
        self._run(lambda s: s._request("GET", "incident", identity=self._identity,
                                       params={"sysparm_limit": 1, "sysparm_fields": "number"}))

    def open_tickets(self, caller: str, host: str = "", ticket_id: str = "", limit: int = 3) -> list[Ticket]:
        u = self._q(caller)
        if not u:
            return []
        q = f"active=true^caller_id.user_name={u}^ORcaller_id.email={u}"
        if host:
            q += f"^cmdb_ci.name={self._q(host)}"
        if ticket_id:
            q += f"^number={self._q(ticket_id)}"
        rows = self._run(lambda s: s._request("GET", "incident", identity=self._identity, params={
            "sysparm_query": q + "^ORDERBYDESCsys_created_on",
            "sysparm_fields": "number,short_description,state",
            "sysparm_display_value": "true", "sysparm_limit": limit}))
        return [to_ticket(r) for r in rows]

    def cancel(self, ticket_id: str, note: str) -> None:
        self._run(lambda s: s._request(
            "PATCH", f"incident/{s._incident_id(ticket_id)}", identity=self._identity,
            json={"state": _CODE["canceled"], "work_notes": note}))

    def set_severity(self, ticket_id: str, urgency: int, impact: int, note: str) -> None:
        self._run(lambda s: s._request(
            "PATCH", f"incident/{s._incident_id(ticket_id)}", identity=self._identity,
            json={"impact": str(impact), "urgency": str(urgency), "work_notes": note}))

    def _sys_id(self, s, ticket_id: str) -> str:
        rows = s._request("GET", "incident", identity=self._identity, params={
            "sysparm_query": f"number={self._q(ticket_id)}", "sysparm_limit": 1, "sysparm_fields": "sys_id"})
        if not rows:
            raise ValueError(f"Incident {ticket_id} not found in SNOW")
        return rows[0]["sys_id"]

    def _raw_state(self, s, ticket_id: str) -> str:
        rows = s._request("GET", "incident", identity=self._identity, params={
            "sysparm_query": f"number={self._q(ticket_id)}", "sysparm_limit": 1,
            "sysparm_fields": "state", "sysparm_display_value": "false"})
        return str(rows[0]["state"]) if rows else ""

    def _set_state(self, ticket_id: str, code: str, note: str, group: str = "") -> None:
        def go(s):
            sys_id = self._sys_id(s, ticket_id)
            fields = {}
            if group:
                g = s._request("GET", "sys_user_group", identity=self._identity, params={
                    "sysparm_query": f"name={group}", "sysparm_limit": 1, "sysparm_fields": "sys_id"})
                if not g:
                    raise ValueError(f"Group {group} not found in SNOW")
                fields["assignment_group"] = g[0]["sys_id"]
            s._request("PATCH", f"incident/{sys_id}", identity=self._identity,
                       json={**fields, "state": code, "work_notes": note[:8000]})
            now = self._raw_state(s, ticket_id)
            if now != code:
                try:
                    s._request("PATCH", f"incident/{sys_id}", identity=self._identity, json={
                        "work_notes": f"{REVIEWER_ACTOR}: ServiceNow did not accept the change to state "
                                      f"{code}; the ticket is still {now}. Disregard the note above."})
                except Exception:
                    pass
                raise RuntimeError(f"ServiceNow ignored the state change (still {now}). "
                                   "Check the checker's roles and ACLs.")
        self._run(go)

    def close(self, ticket_id: str, note: str) -> None:
        self._set_state(ticket_id, _CODE["closed"], note)

    def reopen(self, ticket_id: str, note: str, group: str = "") -> None:
        self._set_state(ticket_id, _CODE["in_progress"], note, group)

    def get_sla(self, ticket_id: str) -> dict:
        return self._run(lambda s: s.get_sla(ticket_id))