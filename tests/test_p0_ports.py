"""P0 tests. Runs without ServiceNow, langchain or a network: snow.py is replaced by a stub."""
import sys
import types

import pytest

from app.adapters.servicenow.ticketing import ServiceNowTicketing, map_error, to_ticket
from app.ports import registry
from app.ports.errors import AuthFailed, NotFound, PortError, Unavailable, Unsupported
from app.ports.models import TicketRef, TicketState
from tests.ticketing_contract import run_contract


class _Tool:
    """Mimics a LangChain tool: .invoke(dict)."""
    def __init__(self, fn):
        self.fn = fn

    def invoke(self, args):
        return self.fn(**args)


def _stub_snow(store):
    m = types.ModuleType("app.adapters.servicenow.client")

    def rec(n):
        return dict(store[n])

    def fetch(n):
        return rec(n) if n in store else {"error": f"Incident {n} not found"}

    def claim(number, work_note=None):
        store[number]["state"] = "In Progress"
        return rec(number)

    def resolve(number, close_notes):
        store[number]["state"] = "Resolved"
        return rec(number)

    def note(number, note):
        store[number]["work_notes"] += note
        return {"number": number, "note_added": True}

    m._fetch = fetch
    m.list_queue = lambda: [n for n, r in store.items() if r["state"] == "New"]
    m.claim_incident, m.resolve_incident = _Tool(claim), _Tool(resolve)
    m.add_work_note = _Tool(note)
    m.escalate_incident = _Tool(lambda number, reason: rec(number))
    m.create_incident = _Tool(lambda **k: rec("INC1"))
    m.search_users = lambda text, limit=8: []
    m.get_sla = lambda n: {"response": "1 hour", "resolution": "1 day", "source": "fallback"}
    return m


@pytest.fixture
def adapter(monkeypatch):
    store = {"INC1": {"number": "INC1", "short_description": "disk full", "description": "d",
                      "state": "New", "priority": "2 - High", "caller_id": "bob",
                      "assignment_group": "L1-SRE-Agent", "assigned_to": "",
                      "work_notes": "", "contact_type": "web"}}
    monkeypatch.setitem(sys.modules, "app.adapters.servicenow.client", _stub_snow(store))
    return ServiceNowTicketing()


def test_contract(adapter):
    run_contract(adapter, "INC1")


def test_normalisation():
    t = to_ticket({"number": "INC5", "short_description": "x", "state": "On Hold",
                   "priority": "1 - Critical", "contact_type": "phone", "cmdb_ci": "web01"})
    assert t.state is TicketState.ON_HOLD and t.host == "web01"
    assert t.extras == {"contact_type": "phone"}
    assert to_ticket({"number": "I", "state": "7"}).state is TicketState.CLOSED
    odd = to_ticket({"number": "I", "state": "Pending Vendor"})
    assert odd.state is TicketState.UNKNOWN and odd.extras["raw_state"] == "Pending Vendor"


@pytest.mark.parametrize("exc,cls", [
    (RuntimeError("SNOW unreachable: timeout"), Unavailable),
    (RuntimeError("SNOW 503 on GET incident: busy"), Unavailable),
    (RuntimeError("SNOW 401 on GET incident: nope"), AuthFailed),
    (ValueError("Incident INC1 not found in SNOW"), NotFound),
    (RuntimeError("SNOW_ESCALATION_GROUP is not set in .env"), Unsupported),
    (KeyError("SNOW_USER"), Unsupported),
    (RuntimeError("something odd"), PortError),
])
def test_error_mapping(exc, cls):
    assert type(map_error(exc)) is cls


def test_ticket_ref_backfill():
    assert TicketRef.parse("INC0010001") == TicketRef("servicenow", "INC0010001")
    assert str(TicketRef.parse("jira:OPS-4")) == "jira:OPS-4"


def test_registry_discovers_servicenow(monkeypatch):
    monkeypatch.delenv("TICKETING_PROVIDER", raising=False)
    assert [m.provider for m in registry.available("ticketing")] == ["servicenow"]
    assert isinstance(registry.get_ticketing(), ServiceNowTicketing)
    with pytest.raises(Unsupported):
        registry.get("ticketing", "jira")
    secrets = registry.available("ticketing")[0].secret_keys
    assert set(secrets) == {"password", "checker_password"}
