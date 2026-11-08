"""The nudge loop and auto-intake rules. A regression like the shadowed `config` name must fail here, not in production."""
import pytest

from app import config
from app.agent import agent as A
from app.api import agent as ag


def _drive(monkeypatch, closed_after=None):
    calls = []

    def fake_run(agent, payload, cfg):
        calls.append(payload)
        yield {"type": "step"}
        return "done"

    monkeypatch.setattr(A, "_run", fake_run)
    monkeypatch.setattr(A, "_is_closed", lambda n: closed_after is not None and len(calls) >= closed_after)
    # the LangGraph run config is passed as `config`, exactly as in production
    events = list(A._run_guarded(None, {"messages": []}, {"configurable": {"thread_id": "t"}}, "INC1"))
    return calls, events


def test_nudges_use_the_admin_setting_not_the_run_config(monkeypatch):
    calls, events = _drive(monkeypatch)
    assert len(calls) == config.get("limits.max_nudges") + 1
    assert events[-1]["type"] == "done" and any(e["type"] == "warning" for e in events)


def test_stops_as_soon_as_the_ticket_closes(monkeypatch):
    calls, events = _drive(monkeypatch, closed_after=1)
    assert len(calls) == 1 and not any(e["type"] == "nudge" for e in events)


def test_nudge_text_comes_from_the_prompt_setting(monkeypatch):
    calls, _ = _drive(monkeypatch)
    assert calls[1]["messages"][0]["content"] == config.get("prompt.nudge")


@pytest.fixture
def intake(monkeypatch):
    runs = {}
    monkeypatch.setattr(ag, "RUNS", runs)
    monkeypatch.setattr(ag.jobs, "queued", lambda: [])
    failed = {}
    monkeypatch.setattr(ag, "_failed_runs", lambda t: failed.get(t, 0))
    monkeypatch.setattr(ag.config, "get", lambda k: 1 if k == "limits.auto_retry_failed" else config.get(k))
    return runs, failed


def test_a_ticket_that_failed_once_is_retried(intake):
    runs, failed = intake
    runs["r1"] = {"ticket": "INC1", "status": "error"}
    failed["INC1"] = 1
    assert "INC1" not in ag._already_worked()


def test_a_ticket_that_keeps_failing_is_left_alone(intake):
    runs, failed = intake
    runs["r1"] = {"ticket": "INC1", "status": "error"}
    failed["INC1"] = 2
    assert "INC1" in ag._already_worked()


def test_running_and_finished_tickets_are_never_requeued(intake):
    runs, _ = intake
    runs["a"] = {"ticket": "INC2", "status": "running"}
    runs["b"] = {"ticket": "INC3", "status": "done"}
    runs["c"] = {"ticket": "INC4", "status": "escalated"}
    assert {"INC2", "INC3", "INC4"} <= ag._already_worked()


def test_a_failed_ticket_with_a_run_in_progress_is_not_retried_again(intake):
    runs, failed = intake
    runs["old"] = {"ticket": "INC5", "status": "error"}
    runs["new"] = {"ticket": "INC5", "status": "running"}
    failed["INC5"] = 1
    assert "INC5" in ag._already_worked()
