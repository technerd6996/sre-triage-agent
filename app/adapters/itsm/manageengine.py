"""ManageEngine ServiceDesk Plus (REST API v3, on-premises). Sign-in: technician key in the `authtoken` header.
A ticket id is the request id. The API takes its data as a form field `input_data` holding JSON."""
from __future__ import annotations

import json

from app import config
from app.adapters.itsm.base import RestTicketing, clean, common_settings, priority_of
from app.ports.errors import NotFound
from app.ports.manifest import Field, Manifest
from app.ports.models import Ticket, TicketState

PROVIDER = "manageengine"
MANIFEST = Manifest(port="ticketing", provider=PROVIDER, title="ManageEngine ServiceDesk Plus",
                    capabilities=frozenset({"work_notes", "create", "caller_search", "review"}),
                    fields=(Field("url", "Address", required=True), Field("api_key", "Technician key", kind="secret", required=True)))
common_settings("manageengine", "ServiceDesk Plus")
config.register("manageengine.api_key", "Technician key", "", "secret", "manageengine")
config.register("manageengine.status_open", "Status name: open", "Open", "text", "manageengine", max_len=60)
config.register("manageengine.status_progress", "Status name: in progress", "In Progress", "text", "manageengine", max_len=60)
config.register("manageengine.status_resolved", "Status name: resolved", "Resolved", "text", "manageengine", max_len=60)
config.register("manageengine.status_closed", "Status name: closed", "Closed", "text", "manageengine", max_len=60)
config.register("manageengine.priority_names", "Priority names, highest first", "High,Normal,Medium,Low", "text", "manageengine", max_len=200)


def _tid(ticket_id) -> str:
    t = "".join(c for c in str(ticket_id) if c.isdigit())
    if not t:
        raise NotFound(f"manageengine: {clean(str(ticket_id))} is not a request id")
    return t


def _name(v) -> str:
    return (v or {}).get("name", "") if isinstance(v, dict) else ""


class ManageEngineTicketing(RestTicketing):
    provider = PROVIDER
    capabilities = MANIFEST.capabilities
    prefix = "manageengine"

    def _auth(self):
        return {"headers": {"authtoken": self._secret("api_key"), "Accept": "application/vnd.manageengine.sdp.v3+json"}}

    def _send(self, method, path, data):
        return self._http(method, path, data={"input_data": json.dumps(data)})

    def _list(self, criteria: list, limit: int) -> list:
        info = {"list_info": {"row_count": limit, "start_index": 1, "sort_field": "created_time", "sort_order": "desc", "search_criteria": criteria}}
        return self._http("GET", "/api/v3/requests", params={"input_data": json.dumps(info)}).get("requests", [])

    def _raw_get(self, ticket_id):
        return self._http("GET", f"/api/v3/requests/{_tid(ticket_id)}")["request"]

    def _to_ticket(self, r) -> Ticket:
        status = _name(r.get("status"))
        by = {self._cfg("status_open").lower(): TicketState.NEW, self._cfg("status_progress").lower(): TicketState.IN_PROGRESS,
              "onhold": TicketState.ON_HOLD, "on hold": TicketState.ON_HOLD,
              self._cfg("status_resolved").lower(): TicketState.RESOLVED, self._cfg("status_closed").lower(): TicketState.CLOSED}
        return Ticket(id=str(r.get("id", "")), title=r.get("subject") or "", description=r.get("description") or "",
                      state=by.get(status.lower(), TicketState.UNKNOWN), priority=_name(r.get("priority")), group=_name(r.get("group")),
                      assignee=_name(r.get("technician")), caller=(r.get("requester") or {}).get("email_id", "") or _name(r.get("requester")),
                      provider=PROVIDER, extras={"raw_state": status})

    def _queue(self):
        crit = [{"field": "status.name", "condition": "is", "value": self._cfg("status_open")},
                {"field": "group.name", "condition": "is", "value": self._cfg("group"), "logical_operator": "AND"}]
        return [str(r["id"]) for r in self._list(crit, 50) if not r.get("technician")]

    def _add_note(self, ticket_id, text):
        self._send("POST", f"/api/v3/requests/{_tid(ticket_id)}/notes",
                   {"note": {"description": text.replace("\n", "<br>"), "show_to_requester": False}})

    def _status_name(self, state) -> str:
        return self._cfg({TicketState.IN_PROGRESS: "status_progress", TicketState.RESOLVED: "status_resolved"}.get(state, "status_closed"))

    def _priority(self, urgency, impact) -> str:
        names = [p.strip() for p in self._cfg("priority_names").split(",") if p.strip()]
        return names[min(priority_of(urgency, impact), len(names)) - 1]

    def _update(self, ticket_id, **f):
        body = {}
        if "state" in f:
            body["status"] = {"name": self._status_name(f["state"])}
        if "group" in f:
            body["group"] = {"name": f["group"]}
        if "assignee" in f:
            body["technician"] = {"email_id": f["assignee"]} if "@" in (f["assignee"] or "") else ({"name": f["assignee"]} if f["assignee"] else None)
        if "urgency" in f:
            body["priority"] = {"name": self._priority(f["urgency"], f["impact"])}
        self._send("PUT", f"/api/v3/requests/{_tid(ticket_id)}", {"request": body})

    def _create(self, x):
        body = {"subject": x["title"][:250], "description": x["description"].replace("\n", "<br>") + (f"<br><br>Server: {x['host']}" if x["host"] else ""),
                "priority": {"name": self._priority(x["urgency"], x["impact"])}}
        if x["caller"]:
            body["requester"] = {"email_id": x["caller"]} if "@" in x["caller"] else {"name": x["caller"]}
        if x["group"]:
            body["group"] = {"name": x["group"]}
        if x["assignee"]:
            body["technician"] = {"email_id": x["assignee"]} if "@" in x["assignee"] else {"name": x["assignee"]}
        return self._send("POST", "/api/v3/requests", {"request": body})["request"]

    def _find_open(self, caller, host, ticket_id, limit):
        if ticket_id:
            return [self._raw_get(ticket_id)]
        crit = [{"field": "requester.email_id", "condition": "is", "value": caller},
                {"field": "status.in_progress", "condition": "is", "value": True, "logical_operator": "AND"}]
        rows = self._list(crit, 30)
        return [r for r in rows if not host or host.lower() in (r.get("subject") or "").lower()][:limit]

    def _users(self, text, limit):
        info = {"list_info": {"row_count": limit, "search_criteria": [{"field": "name", "condition": "contains", "value": clean(text)}]}}
        rows = self._http("GET", "/api/v3/users", params={"input_data": json.dumps(info)}).get("users", [])
        return [{"user_name": r.get("email_id") or r.get("name", ""), "name": r.get("name", ""), "email": r.get("email_id", "")} for r in rows]

    def _ping(self):
        self._list([], 1)
