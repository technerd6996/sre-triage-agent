"""BMC Helix ITSM / Remedy (AR System REST API). Sign-in: user and password exchanged for a JWT (cached, renewed on expiry).
A ticket id is the Incident Number (INC...). Incidents are read and updated through HPD:IncidentInterface, created through
HPD:IncidentInterface_Create, and notes are written to HPD:WorkLog. Field names are BMC's standard ones."""
from __future__ import annotations

import threading
import time

import requests

from app import config
from app.adapters.itsm.base import RestTicketing, clean, common_settings
from app.ports.errors import AuthFailed, NotFound, PortError, Unavailable
from app.ports.manifest import Field, Manifest
from app.ports.models import Ticket, TicketState

PROVIDER = "bmc"
MANIFEST = Manifest(port="ticketing", provider=PROVIDER, title="BMC Helix / Remedy",
                    capabilities=frozenset({"work_notes", "create", "review"}),
                    fields=(Field("url", "Address", required=True), Field("user", "User", required=True),
                            Field("password", "Password", kind="secret", required=True)))
common_settings("bmc", "BMC")
config.register("bmc.user", "User", "", "text", "bmc", max_len=200)
config.register("bmc.password", "Password", "", "secret", "bmc")
config.register("bmc.company", "Company (support and contact)", "", "text", "bmc", max_len=200, help="BMC needs a company on every incident.")
config.register("bmc.organization", "Support organization", "", "text", "bmc", max_len=200)
config.register("bmc.first_name", "Default caller first name", "", "text", "bmc", max_len=100)
config.register("bmc.last_name", "Default caller last name", "", "text", "bmc", max_len=100)

_STATE = {"New": TicketState.NEW, "Assigned": TicketState.NEW, "In Progress": TicketState.IN_PROGRESS, "Pending": TicketState.ON_HOLD,
          "Resolved": TicketState.RESOLVED, "Closed": TicketState.CLOSED, "Cancelled": TicketState.CLOSED}
_STATUS = {TicketState.IN_PROGRESS: "In Progress", TicketState.RESOLVED: "Resolved", TicketState.CLOSED: "Closed"}
_LEVEL = {1: "1-Extensive/Widespread", 2: "2-Significant/Large", 3: "3-Moderate/Limited", 4: "4-Minor/Localized"}
_URGENCY = {1: "1-Critical", 2: "2-High", 3: "3-Medium", 4: "4-Low"}
_token = {"value": "", "at": 0.0}
_lock = threading.Lock()


class BmcTicketing(RestTicketing):
    provider = PROVIDER
    capabilities = MANIFEST.capabilities
    prefix = "bmc"

    def _auth(self):
        with _lock:
            if not _token["value"] or time.time() - _token["at"] > 1800:
                try:
                    r = requests.post(self._base() + "/api/jwt/login", data={"username": self._cfg("user"), "password": self._secret("password")}, timeout=30)
                except requests.RequestException as e:
                    raise Unavailable(f"bmc unreachable: {type(e).__name__}") from e
                if r.status_code != 200:
                    raise AuthFailed(f"bmc refused the login (HTTP {r.status_code})")
                _token.update(value=r.text.strip(), at=time.time())
            return {"headers": {"Authorization": "AR-JWT " + _token["value"]}}

    def _entries(self, form: str, q: str, limit: int) -> list:
        return [e.get("values", {}) for e in self._http("GET", f"/api/arsys/v1/entry/{form}", params={"q": q, "limit": limit}).get("entries", [])]

    def _row(self, ticket_id: str) -> dict:
        rows = self._http("GET", "/api/arsys/v1/entry/HPD:IncidentInterface", params={"q": f"'Incident Number'=\"{clean(ticket_id)}\"", "limit": 1}).get("entries", [])
        if not rows:
            raise NotFound(f"bmc: {clean(ticket_id)} not found")
        return rows[0]

    def _raw_get(self, ticket_id):
        return self._row(ticket_id).get("values", {})

    def _to_ticket(self, v) -> Ticket:
        status = v.get("Status") or ""
        return Ticket(id=v.get("Incident Number") or "", title=v.get("Description") or "", description=v.get("Detailed Decription") or v.get("Detailed Description") or "",
                      state=_STATE.get(status, TicketState.UNKNOWN), priority=v.get("Priority") or "", group=v.get("Assigned Group") or "",
                      assignee=v.get("Assignee") or "", caller=v.get("Internet E-mail") or "", provider=PROVIDER, extras={"raw_state": status})

    def _queue(self):
        q = f"'Assigned Group'=\"{clean(self._cfg('group'))}\" AND ('Status'=\"New\" OR 'Status'=\"Assigned\") AND 'Assignee'=$NULL$"
        return [v["Incident Number"] for v in self._entries("HPD:IncidentInterface", q, 50)]

    def _add_note(self, ticket_id, text):
        self._http("POST", "/api/arsys/v1/entry/HPD:WorkLog", json={"values": {
            "Incident Number": clean(ticket_id), "Description": text[:250], "Detailed Description": text,
            "Work Log Type": "General Information", "View Access": "Internal", "Secure Work Log": "Yes"}})

    def _update(self, ticket_id, **f):
        row = self._row(ticket_id)
        href = row["_links"]["self"][0]["href"]
        path = href[href.index("/api/arsys/"):]
        values = {}
        if "state" in f:
            values["Status"] = _STATUS[f["state"]]
            if f["state"] is TicketState.RESOLVED:
                values["Status_Reason"] = "No Further Action Required"
                values["Resolution"] = "Resolved by TRIAGE-1. See the work log."
        if "group" in f:
            values["Assigned Group"] = f["group"]
        if "assignee" in f:
            values["Assignee"] = f["assignee"] or None
        if "urgency" in f:
            values["Urgency"] = _URGENCY[int(f["urgency"])]
            values["Impact"] = _LEVEL[int(f["impact"])]
        self._http("PUT", path, json={"values": values})

    def _create(self, x):
        values = {"z1D_Action": "CREATE", "Description": x["title"][:100], "Detailed_Decription": x["description"] + (f"\n\nServer: {x['host']}" if x["host"] else ""),
                  "Service_Type": "Infrastructure Event", "Status": "Assigned", "Reported Source": "Other", "Impact": _LEVEL[int(x["impact"])], "Urgency": _URGENCY[int(x["urgency"])],
                  "First_Name": self._cfg("first_name"), "Last_Name": self._cfg("last_name"), "Company": self._cfg("company"),
                  "Assigned Support Company": self._cfg("company"), "Assigned Support Organization": self._cfg("organization"), "Assigned Group": x["group"] or self._cfg("group")}
        if x["assignee"]:
            values["Assignee"] = x["assignee"]
        out = self._http("POST", "/api/arsys/v1/entry/HPD:IncidentInterface_Create", params={"fields": "values(Incident Number)"}, json={"values": values})
        number = (out.get("values") or {}).get("Incident Number")
        if not number:
            raise PortError("bmc: the incident was submitted but no Incident Number came back. Check the required fields on the form.")
        return self._raw_get(number)

    def _find_open(self, caller, host, ticket_id, limit):
        q = f"'Internet E-mail'=\"{clean(caller)}\" AND 'Status'<\"Resolved\""
        if ticket_id:
            q += f" AND 'Incident Number'=\"{clean(ticket_id)}\""
        rows = self._entries("HPD:IncidentInterface", q, 30)
        return [v for v in rows if not host or host.lower() in (v.get("Description") or "").lower()][:limit]

    def _users(self, text, limit):
        return []                                            # BMC people search is not wired yet (no caller_search capability)

    def _ping(self):
        self._http("GET", "/api/arsys/v1/entry/HPD:IncidentInterface", params={"limit": 1, "fields": "values(Incident Number)"})
