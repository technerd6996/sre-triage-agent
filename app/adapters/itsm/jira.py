"""Jira Service Management (Cloud REST API v3). Sign-in: account email + API token. A ticket id is the issue key (OPS-123).
Workflows differ per project, so a state change looks for an available transition by name (the names are settings)."""
from __future__ import annotations

from app import config
from app.adapters.itsm.base import RestTicketing, clean, common_settings, priority_of
from app.ports.errors import PortError
from app.ports.manifest import Field, Manifest
from app.ports.models import Ticket, TicketState

PROVIDER = "jira"
MANIFEST = Manifest(port="ticketing", provider=PROVIDER, title="Jira Service Management",
                    capabilities=frozenset({"work_notes", "create", "caller_search", "review"}),
                    fields=(Field("url", "Address", required=True), Field("email", "Account email", required=True),
                            Field("token", "API token", kind="secret", required=True), Field("project", "Project key", required=True)))
common_settings("jira", "Jira")
config.register("jira.email", "Account email", "", "text", "jira", max_len=200)
config.register("jira.token", "API token", "", "secret", "jira")
config.register("jira.project", "Project key", "", "text", "jira", max_len=40)
config.register("jira.issue_type", "Issue type for new tickets", "Incident", "text", "jira", max_len=60)
config.register("jira.team_field", "Field that holds the group", "", "text", "jira", max_len=60,
                help="The custom field id for the assignment group, for example customfield_10050. Empty means tickets are not moved between groups.")
config.register("jira.transition_progress", "Transition name: in progress", "In Progress", "text", "jira", max_len=60)
config.register("jira.transition_resolved", "Transition name: resolved", "Resolved", "text", "jira", max_len=60)
config.register("jira.transition_closed", "Transition name: closed", "Closed", "text", "jira", max_len=60)

_STATE = {"new": TicketState.NEW, "indeterminate": TicketState.IN_PROGRESS}
_PRIORITY = {1: "Highest", 2: "High", 3: "Medium", 4: "Low"}


def _adf(text: str) -> dict:
    """Jira's document format, one paragraph per line."""
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": ln}] if ln else []} for ln in text.split("\n")]}


def _plain(node) -> str:
    if isinstance(node, str) or node is None:
        return node or ""
    if node.get("type") == "text":
        return node.get("text", "")
    sep = "\n" if node.get("type") == "doc" else ""
    return sep.join(_plain(c) for c in node.get("content", []))


class JiraTicketing(RestTicketing):
    provider = PROVIDER
    capabilities = MANIFEST.capabilities
    prefix = "jira"

    def _auth(self) -> dict:
        return {"auth": (self._cfg("email"), self._secret("token"))}

    def _raw_get(self, ticket_id):
        return self._http("GET", f"/rest/api/3/issue/{clean(ticket_id)}")

    def _to_ticket(self, raw) -> Ticket:
        f = raw.get("fields") or {}
        status = f.get("status") or {}
        cat = (status.get("statusCategory") or {}).get("key", "")
        name = (status.get("name") or "").lower()
        state = TicketState.RESOLVED if name == self._cfg("transition_resolved").lower() else (
            TicketState.CLOSED if cat == "done" else _STATE.get(cat, TicketState.UNKNOWN))
        return Ticket(id=raw.get("key", ""), title=f.get("summary") or "", description=_plain(f.get("description")), state=state,
                      priority=(f.get("priority") or {}).get("name", ""), assignee=(f.get("assignee") or {}).get("displayName", ""),
                      caller=(f.get("reporter") or {}).get("emailAddress", ""), provider=PROVIDER,
                      extras={"raw_state": status.get("name", "")})

    def _jql(self, jql: str, limit: int, fields: str = "summary,status,priority,assignee,reporter,description") -> list:
        return self._http("GET", "/rest/api/3/search/jql", params={"jql": jql, "maxResults": limit, "fields": fields}).get("issues", [])

    def _queue(self):
        jql = f'project = "{clean(self._cfg("project"))}" AND statusCategory = "To Do" AND assignee IS EMPTY ORDER BY priority ASC, created ASC'
        return [r["key"] for r in self._jql(jql, 50, "summary")]

    def _add_note(self, ticket_id, text):
        self._http("POST", f"/rest/api/3/issue/{clean(ticket_id)}/comment",
                   json={"body": _adf(text), "properties": [{"key": "sd.public.comment", "value": {"internal": True}}]})

    def _account(self, who: str) -> str:
        rows = self._http("GET", "/rest/api/3/user/search", params={"query": clean(who), "maxResults": 1})
        if not rows:
            raise PortError(f"jira: no user found for {clean(who)}")
        return rows[0]["accountId"]

    def _transition(self, ticket_id, name: str):
        rows = self._http("GET", f"/rest/api/3/issue/{clean(ticket_id)}/transitions").get("transitions", [])
        hit = next((t for t in rows if t.get("name", "").lower() == name.lower() or (t.get("to") or {}).get("name", "").lower() == name.lower()), None)
        if not hit:
            raise PortError(f"jira: the workflow offers no transition '{name}' from this ticket's state. Check the transition names in the settings.")
        self._http("POST", f"/rest/api/3/issue/{clean(ticket_id)}/transitions", json={"transition": {"id": hit["id"]}})

    def _update(self, ticket_id, **f):
        fields = {}
        if "assignee" in f:
            fields["assignee"] = {"accountId": self._account(f["assignee"])} if f["assignee"] else None
        if "group" in f and self._cfg("team_field"):
            fields[self._cfg("team_field")] = {"name": f["group"]}
        if "urgency" in f:
            fields["priority"] = {"name": _PRIORITY[priority_of(f["urgency"], f["impact"])]}
        if fields:
            self._http("PUT", f"/rest/api/3/issue/{clean(ticket_id)}", json={"fields": fields})
        if "state" in f:
            self._transition(ticket_id, self._cfg({TicketState.IN_PROGRESS: "transition_progress", TicketState.RESOLVED: "transition_resolved"}.get(f["state"], "transition_closed")))

    def _create(self, x):
        fields = {"project": {"key": self._cfg("project")}, "issuetype": {"name": self._cfg("issue_type")}, "summary": x["title"][:250],
                  "description": _adf(x["description"] + (f"\n\nServer: {x['host']}" if x["host"] else "")),
                  "priority": {"name": _PRIORITY[priority_of(x["urgency"], x["impact"])]}}
        if x["assignee"]:
            fields["assignee"] = {"accountId": self._account(x["assignee"])}
        if x["group"] and self._cfg("team_field"):
            fields[self._cfg("team_field")] = {"name": x["group"]}
        key = self._http("POST", "/rest/api/3/issue", json={"fields": fields})["key"]
        return self._raw_get(key)

    def _find_open(self, caller, host, ticket_id, limit):
        jql = f'project = "{clean(self._cfg("project"))}" AND statusCategory != Done AND reporter = "{self._account(caller)}"'
        if host:
            jql += f' AND text ~ "{clean(host)}"'
        if ticket_id:
            jql += f' AND key = "{clean(ticket_id)}"'
        return self._jql(jql + " ORDER BY created DESC", limit)

    def _users(self, text, limit):
        rows = self._http("GET", "/rest/api/3/user/search", params={"query": clean(text), "maxResults": limit})
        return [{"user_name": r.get("emailAddress") or r.get("accountId", ""), "name": r.get("displayName", ""), "email": r.get("emailAddress", "")} for r in rows]

    def _ping(self):
        self._http("GET", "/rest/api/3/myself")
