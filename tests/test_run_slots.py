"""runs.reserved_slots: automatic work leaves room for people (chat, ad-hoc, console runs)."""
import pytest

from app.api import agent as ag


@pytest.fixture
def slots(monkeypatch):
    runs = {}
    monkeypatch.setattr(ag, "RUNS", runs)
    vals = {"runs.max_concurrent": 5, "runs.reserved_slots": 1}
    real = ag.config.get
    monkeypatch.setattr(ag.config, "get", lambda k: vals[k] if k in vals else real(k))

    def add(n, source):
        for i in range(n):
            runs[f"{source}{i}{len(runs)}"] = {"status": "running", "source": source}
    return vals, add


def test_automatic_runs_stop_one_short_of_the_cap(slots):
    _, add = slots
    add(3, "auto")
    assert ag._can_start("auto")             # 4th automatic run is fine
    add(1, "auto")
    assert not ag._can_start("auto")         # the 5th place is kept for a person


def test_a_person_can_use_the_reserved_slot(slots):
    _, add = slots
    add(4, "auto")
    for src in ("approver_adhoc", "chat", "manual"):
        assert ag._can_start(src)


def test_everything_waits_when_all_slots_are_used(slots):
    _, add = slots
    add(4, "auto")
    add(1, "approver_adhoc")
    assert not ag._can_start("approver_adhoc") and not ag._can_start("auto")


def test_reserved_zero_behaves_as_before(slots):
    vals, add = slots
    vals["runs.reserved_slots"] = 0
    add(4, "auto")
    assert ag._can_start("auto")


def test_automatic_work_always_keeps_one_slot(slots):
    vals, _ = slots
    vals["runs.max_concurrent"], vals["runs.reserved_slots"] = 2, 7      # nonsense value
    assert ag._can_start("auto")


def test_dispatcher_choice_skips_a_blocked_automatic_job(slots, monkeypatch):
    from app import jobs
    _, add = slots
    add(4, "auto")
    queue = [{"id": "a", "source": "auto", "priority": 1, "created": 1},       # older and same priority, but blocked
             {"id": "b", "source": "approver_adhoc", "priority": 1, "created": 2}]
    ready = [j for j in queue if ag._can_start(j["source"])]
    assert jobs.pick_next(ready)["id"] == "b"
