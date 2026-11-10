"""What the non-ServiceNow ticketing adapters share. Every port operation is written once here, in terms of a few
vendor primitives each adapter supplies:

    _raw_get(id)            one vendor record
    _to_ticket(raw)         vendor record -> Ticket
    _queue()                ids waiting in the L1 queue
    _add_note(id, text)     private note
    _update(id, **f)        change any of: state, group, assignee, urgency, impact (only the ones given)
    _create(fields)         new ticket -> vendor record
    _find_open(caller, host, ticket_id, limit)   open tickets of one person
    _users(text, limit)     [{user_name, name, email}]
    _ping()                 raises if the system cannot be reached or refuses the login

States used in _update are TicketState values; each adapter maps them to its own."""
from __future__ import annotations

import requests

from app import config
from app.ports.errors import AuthFailed, NotFound, PortError, Unavailable
from app.ports.models import Ticket, TicketState


class RestTicketing:
    provider = ""
    capabilities = frozenset()
    prefix = ""                             # settings prefix, for example "jira"

    def __init__(self, role: str = "agent"):
        self.role = role                    # the reviewer role uses the same account: these tools have one integration login

    # ---- settings and HTTP
    def _cfg(self, name: str) -> str:
        return str(config.get(f"{self.prefix}.{name}") or "").strip()

    def _secret(self, name: str) -> str:
        return config.get_secret(f"{self.prefix}.{name}")

    def _base(self) -> str:
        return self._cfg("url").rstrip("/")

    def _auth(self) -> dict:
        """Keyword arguments for requests (auth or headers)."""
        return {}

    def _http(self, method: str, path: str, **kw):
        """The parsed reply. Every failure becomes one of the port's errors; a reply body is never put in the message."""
        if not self._base().lower().startswith("https://"):
            raise PortError(f"{self.provider}: the address must be set and start with https://")
        auth = self._auth()
        headers = {"Accept": "application/json", **auth.pop("headers", {}), **kw.pop("headers", {})}
        try:
            r = requests.request(method, self._base() + path, headers=headers, timeout=30, **auth, **kw)
        except requests.RequestException as e:
            raise Unavailable(f"{self.provider} unreachable: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise AuthFailed(f"{self.provider} refused the login or the action (HTTP {r.status_code})")
        if r.status_code == 404:
            raise NotFound(f"{self.provider}: not found ({method} {path.split('?')[0]})")
        if r.status_code == 429 or r.status_code >= 500:
            raise Unavailable(f"{self.provider} answered HTTP {r.status_code}")
        if r.status_code >= 400:
            raise PortError(f"{self.provider} refused the request (HTTP {r.status_code})")
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    # ---- vendor primitives
    def _raw_get(self, ticket_id: str) -> dict: raise NotImplementedError
    def _to_ticket(self, raw: dict) -> Ticket: raise NotImplementedError
    def _queue(self) -> list: raise NotImplementedError
    def _add_note(self, ticket_id: str, text: str) -> None: raise NotImplementedError
    def _update(self, ticket_id: str, **fields) -> None: raise NotImplementedError
    def _create(self, fields: dict) -> dict: raise NotImplementedError
    def _find_open(self, caller: str, host: str, ticket_id: str, limit: int) -> list: raise NotImplementedError
    def _users(self, text: str, limit: int) -> list: raise NotImplementedError
    def _ping(self) -> None: raise NotImplementedError

    # ---- the port
    def list_queue(self, exclude=()) -> list:
        return [i for i in self._queue() if i not in set(exclude)]

    def get(self, ticket_id: str) -> Ticket:
        return self._to_ticket(self._raw_get(ticket_id))

    def add_note(self, ticket_id: str, note: str) -> None:
        self._add_note(ticket_id, note[:8000])

    def claim(self, ticket_id: str, note: str | None = None) -> Ticket:
        self._update(ticket_id, assignee=self._cfg("assignee"), state=TicketState.IN_PROGRESS)
        if note:
            self.add_note(ticket_id, note)
        return self.get(ticket_id)

    def resolve(self, ticket_id: str, close_notes: str) -> Ticket:
        self.add_note(ticket_id, close_notes)
        self._update(ticket_id, state=TicketState.RESOLVED)
        return self.get(ticket_id)

    def escalate(self, ticket_id: str, reason: str) -> Ticket:
        self.add_note(ticket_id, reason)
        self._update(ticket_id, group=self._cfg("escalation_group"), assignee="")
        return self.get(ticket_id)

    def create(self, title: str, description: str, urgency: int = 3, impact: int = 3, caller: str = "", host: str = "") -> Ticket:
        raw = self._create({"title": title, "description": description, "urgency": urgency, "impact": impact,
                            "caller": caller, "host": host, "group": self._cfg("group"), "assignee": self._cfg("assignee")})
        return self._to_ticket(raw)

    def create_for_user(self, title: str, description: str, user_email: str, urgency: int = 3, impact: int = 3,
                        host: str = "", note: str = "") -> Ticket:
        """Owned by the person and outside the L1 group, so the automatic agent never picks it up."""
        t = self._to_ticket(self._create({"title": title, "description": description, "urgency": urgency, "impact": impact,
                                          "caller": user_email, "host": host, "group": "", "assignee": user_email}))
        if note:
            self.add_note(t.id, note)
        return t

    def search_callers(self, text: str, limit: int = 8) -> list:
        return self._users(text[:50], limit)

    def get_sla(self, ticket_id: str) -> dict:
        return {"available": False, "reason": f"SLA details are not read from {self.provider} yet"}

    def ping(self) -> None:
        self._ping()

    def open_tickets(self, caller: str, host: str = "", ticket_id: str = "", limit: int = 3) -> list:
        return [self._to_ticket(r) for r in self._find_open(caller, host, ticket_id, limit)] if caller else []

    def cancel(self, ticket_id: str, note: str) -> None:
        self.add_note(ticket_id, note)
        self._update(ticket_id, state=TicketState.CLOSED)

    def set_severity(self, ticket_id: str, urgency: int, impact: int, note: str) -> None:
        self._update(ticket_id, urgency=urgency, impact=impact)
        self.add_note(ticket_id, note)

    def close(self, ticket_id: str, note: str) -> None:
        self.add_note(ticket_id, note)
        self._update(ticket_id, state=TicketState.CLOSED)

    def reopen(self, ticket_id: str, note: str, group: str = "") -> None:
        self.add_note(ticket_id, note)
        self._update(ticket_id, state=TicketState.IN_PROGRESS, **({"group": group} if group else {}))


def common_settings(prefix: str, title: str) -> None:
    """The fields every tool needs. Group = the prefix, so each tool gets its own section in the admin panel."""
    config.register(f"{prefix}.url", f"{title} address", "", "text", prefix, max_len=300, help="Must start with https://")
    config.register(f"{prefix}.group", "L1 queue (group the agent works)", "", "text", prefix, max_len=200)
    config.register(f"{prefix}.assignee", "Agent account (assignee)", "", "text", prefix, max_len=200)
    config.register(f"{prefix}.escalation_group", "L2 escalation group", "", "text", prefix, max_len=200)


def priority_of(urgency: int, impact: int) -> int:
    """1 = highest, 4 = lowest, from urgency and impact (1 = high, 3 = low), the usual ITIL matrix."""
    return min(4, max(1, int(urgency) + int(impact) - 1))


def clean(text: str) -> str:
    """Text safe to place inside a vendor's query language: operators and quotes removed."""
    return "".join(c for c in (text or "") if c.isalnum() or c in "@._ -")
