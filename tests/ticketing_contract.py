"""Contract suite: every Ticketing adapter must pass this unchanged.
Usage: run_contract(adapter, seed_ticket_id) where seed_ticket_id is a NEW ticket
in the adapter's queue (use a fake or a sandbox instance)."""
from app.ports.errors import NotFound, PortError
from app.ports.models import Ticket, TicketState
from app.ports.ticketing import TicketingPort


def run_contract(adapter, seed_id: str, missing_id: str = "INC9999999") -> None:
    assert isinstance(adapter, TicketingPort), "adapter does not satisfy TicketingPort"
    assert adapter.provider and isinstance(adapter.capabilities, frozenset)

    t = adapter.get(seed_id)
    assert isinstance(t, Ticket) and t.id == seed_id and t.provider == adapter.provider
    assert isinstance(t.state, TicketState)
    assert str(t.ref) == f"{adapter.provider}:{seed_id}"

    try:
        adapter.get(missing_id)
        raise AssertionError("get() on a missing ticket must raise NotFound")
    except NotFound:
        pass

    assert seed_id in adapter.list_queue()
    adapter.add_note(seed_id, "contract: note")
    claimed = adapter.claim(seed_id)
    assert claimed.state is TicketState.IN_PROGRESS
    done = adapter.resolve(seed_id, "contract: resolved")
    assert done.state in (TicketState.RESOLVED, TicketState.CLOSED)

    for e in (NotFound,):
        assert issubclass(e, PortError)