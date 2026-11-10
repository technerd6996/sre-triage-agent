"""Freshservice (REST API v2). Sign-in: API key as the user name. A ticket id is the numeric ticket id.
Status: 2 open, 3 pending, 4 resolved, 5 closed. Priority: 1 low .. 4 urgent (the reverse of TRIAGE-1's)."""
from __future__ import annotations

from app import config
from app.adapters.itsm.base import RestTicketing, clean, common_settings, priority_of
from app.ports.errors import NotFound, PortError
from app.ports.manifest import Field, Manifest
from app.ports.models import Ticket, TicketState

PROVIDER = "freshservice"
MANIFEST = Manifest(port="ticketing", provider=PROVIDER, title="Freshservice",
                    capabilities=frozenset({"work_notes", "create", "caller_search", "review"}),
                    fields=(Field("url", "Address", required=True), Field("api_key", "API key", kind="secret", required=True)))
common_settings("freshservice", "Freshservice")
config.register("freshservice.api_key", "API key", "", "secret", "freshservice")
config.register("freshservice.requester_email", "Requester when none is given", "", "text", "freshservice", max_len=200)

_STATE = {2: TicketState.NEW, 3: TicketState.ON_HOLD, 4: TicketState.RESOLVED, 5: TicketState.CLOSED}
_CODE = {TicketState.IN_PROGRESS: 2, TicketState.RESOLVED: 4, TicketState.CLOSED: 5}


def _tid(ticket_id) -> str:
    t = "".join(c for c in str(ticket_id) if c.isdigit())
    if not t:
        raise NotFound(f"freshservice: {clean(str(ticket_id))} is not a ticket id")
    return t


class FreshserviceTicketing(RestTicketing):
    provider = PROVIDER
    capabilities = MANIFEST.capabilities
    prefix = "freshservice"

    def _auth(self):
        return {"auth": (self._secret("api_key"), "X")}

    def _raw_get(self, ticket_id):
        return self._http("GET", f"/api/v2/tickets/{_tid(ticket_id)}")["ticket"]

    def _to_ticket(self, r) -> Ticket:
        return Ticket(id=str(r.get("id", "")), title=r.get("subject") or "", description=r.get("description_text") or r.get("description") or "",
                      state=_STATE.get(r.get("status"), TicketState.UNKNOWN), priority=str(r.get("priority") or ""),
                      group=str(r.get("group_id") or ""), assignee=str(r.get("responder_id") or ""), caller=str(r.get("requester_id") or ""),
                      provider=PROVIDER, extras={"raw_state": str(r.get("status", ""))})

    def _group_id(self, name: str) -> int:
        page = self._http("GET", "/api/v2/groups", params={"per_page": 100}).get("groups", [])
        hit = next((g["id"] for g in page if g.get("name", "").lower() == name.lower()), None)
        if hit is None:
            raise PortError(f"freshservice: no group named {clean(name)}")
        return hit

    def _agent_id(self, who: str) -> int:
        rows = self._http("GET", "/api/v2/agents", params={"email": who}).get("agents", [])
        if not rows:
            raise PortError(f"freshservice: no agent with address {clean(who)}")
        return rows[0]["id"]

    def _filter(self, query: str, limit: int) -> list:
        return self._http("GET", "/api/v2/tickets/filter", params={"query": f'"{query}"'}).get("tickets", [])[:limit]

    def _queue(self):
        rows = self._filter(f"group_id:{self._group_id(self._cfg('group'))} AND status:2 AND agent_id:null", 50)
        return [str(r["id"]) for r in rows]

    def _add_note(self, ticket_id, text):
        self._http("POST", f"/api/v2/tickets/{_tid(ticket_id)}/notes", json={"body": text.replace("\n", "<br>"), "private": True})

    def _update(self, ticket_id, **f):
        body = {}
        if "state" in f:
            body["status"] = _CODE[f["state"]]
        if "group" in f:
            body["group_id"] = self._group_id(f["group"])
        if "assignee" in f:
            body["responder_id"] = self._agent_id(f["assignee"]) if f["assignee"] else None
        if "urgency" in f:
            body["priority"] = 5 - priority_of(f["urgency"], f["impact"])
        self._http("PUT", f"/api/v2/tickets/{_tid(ticket_id)}", json=body)

    def _create(self, x):
        email = x["caller"] if "@" in (x["caller"] or "") else self._cfg("requester_email")
        if not email:
            raise PortError("freshservice: a requester email is needed; set one in the Freshservice settings")
        body = {"subject": x["title"][:250], "description": x["description"].replace("\n", "<br>") + (f"<br><br>Server: {x['host']}" if x["host"] else ""),
                "email": email, "priority": 5 - priority_of(x["urgency"], x["impact"]), "status": 2, "source": 2}
        if x["group"]:
            body["group_id"] = self._group_id(x["group"])
        if x["assignee"]:
            body["responder_id"] = self._agent_id(x["assignee"])
        return self._http("POST", "/api/v2/tickets", json=body)["ticket"]

    def _find_open(self, caller, host, ticket_id, limit):
        if ticket_id:
            r = self._raw_get(ticket_id)
            return [r] if r.get("status") in (2, 3) else []
        rows = self._http("GET", "/api/v2/tickets", params={"email": caller, "per_page": 30}).get("tickets", [])
        rows = [r for r in rows if r.get("status") in (2, 3) and (not host or host.lower() in (r.get("subject") or "").lower())]
        return rows[:limit]

    def _users(self, text, limit):
        rows = self._http("GET", "/api/v2/requesters", params={"query": f'"~[first_name|last_name|primary_email]:\'{clean(text)}\'"', "per_page": limit}).get("requesters", [])
        return [{"user_name": r.get("primary_email", ""), "name": f"{r.get('first_name', '')} {r.get('last_name') or ''}".strip(), "email": r.get("primary_email", "")} for r in rows]

    def _ping(self):
        self._http("GET", "/api/v2/tickets", params={"per_page": 1})
