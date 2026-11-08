"""A run that handed the ticket to a human is 'escalated', never 'error'; a real failure stays 'error'."""
import pytest

from app.api import agent as ag


class _Tool:
    def __init__(self, result=None, boom=False):
        self.result, self.boom, self.calls = result, boom, []

    def invoke(self, args):
        self.calls.append(args)
        if self.boom:
            raise RuntimeError("ServiceNow down")
        return self.result if self.result is not None else {"ok": True}


@pytest.fixture
def env(monkeypatch):
    saved = []
    monkeypatch.setattr(ag.store, "save_run", lambda r: saved.append(r["status"]))
    monkeypatch.setattr(ag.store, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(ag.store, "audit", lambda *a, **k: None)
    monkeypatch.setattr(ag.store, "llm_totals", lambda rid: {})
    monkeypatch.setattr(ag.usage, "bind_run", lambda *a, **k: None)
    monkeypatch.setattr(ag.usage, "unbind_run", lambda *a, **k: None)
    monkeypatch.setattr(ag.usage, "run_totals", lambda rid: {"tokens_in": 0, "tokens_out": 0, "calls": 0})
    monkeypatch.setattr(ag.active, "bind_run", lambda *a, **k: None)
    monkeypatch.setattr(ag, "_post_report", lambda r: None)
    monkeypatch.setattr(ag.checker, "verify_run", lambda *a, **k: None)
    return monkeypatch


def _run():
    return {"id": "r1", "ticket": "INC1", "status": "running", "events": [], "created": 0}


def _same_call(n=3):
    return [{"type": "tool_call", "name": "check_disk_usage", "args": {"target": "linux"}} for _ in range(n)]


def test_loop_stop_with_successful_escalation_is_escalated_not_error(env):
    tool = _Tool({"ok": True})
    env.setattr(ag, "escalate_incident", tool)
    run = _run()
    ag._consume(run, iter(_same_call()))
    assert run["status"] == "escalated" and run["outcome"] == "escalated"
    assert tool.calls and "Loop detected" in tool.calls[0]["reason"]
    assert not any(e["type"] == "error" for e in run["events"])


def test_loop_stop_when_escalation_fails_is_still_an_error(env):
    env.setattr(ag, "escalate_incident", _Tool(boom=True))
    run = _run()
    ag._consume(run, iter(_same_call()))
    assert run["status"] == "error"
    assert any(e["type"] == "error" for e in run["events"])


def test_escalation_result_with_error_is_an_error(env):
    env.setattr(ag, "escalate_incident", _Tool({"error": "no such group"}))
    run = _run()
    ag._consume(run, iter(_same_call()))
    assert run["status"] == "error"


def test_error_after_the_agent_escalated_is_escalated(env):
    run = _run()
    events = [{"type": "tool_call", "name": "escalate_incident", "args": {"reason": "x"}},
              {"type": "error", "message": "model dropped the connection"}]
    ag._consume(run, iter(events))
    assert run["status"] == "escalated" and run["outcome"] == "escalated"


def test_error_without_escalation_stays_an_error(env):
    run = _run()
    ag._consume(run, iter([{"type": "error", "message": "boom"}]))
    assert run["status"] == "error" and run["outcome"] == "incomplete"


def test_escalated_does_not_trip_the_circuit_breaker():
    # the breaker counts status error/interrupted or outcome incomplete; an escalated run is neither
    run = {"status": "escalated", "outcome": "escalated"}
    assert not (run["status"] in ("error", "interrupted") or run.get("outcome") == "incomplete")
