from app.api import agent as api_agent
from app.llm import usage


def _call(args=None):
    return {"type": "tool_call", "name": "check_service_status", "args": args or {"target": "windows"}}


def test_over_budget_is_stopped(monkeypatch):
    monkeypatch.setattr(api_agent.config, "get", lambda key, *a: 1000)
    monkeypatch.setitem(usage._totals, "r1", {"tokens_in": 900, "tokens_out": 200, "calls": 3})
    assert "Token budget exceeded" in api_agent._guard({"id": "r1"}, {"type": "text"}, [])


def test_under_budget_passes(monkeypatch):
    monkeypatch.setattr(api_agent.config, "get", lambda key, *a: 1000)
    monkeypatch.setitem(usage._totals, "r2", {"tokens_in": 300, "tokens_out": 100, "calls": 2})
    assert api_agent._guard({"id": "r2"}, {"type": "text"}, []) is None


def test_same_call_three_times_is_a_loop(monkeypatch):
    monkeypatch.setattr(api_agent.config, "get", lambda key, *a: 0)     # budget off
    run, sigs = {"id": "r3"}, []
    assert api_agent._guard(run, _call(), sigs) is None
    assert api_agent._guard(run, _call(), sigs) is None
    assert "Loop detected" in api_agent._guard(run, _call(), sigs)


def test_different_arguments_are_not_a_loop(monkeypatch):
    monkeypatch.setattr(api_agent.config, "get", lambda key, *a: 0)
    run, sigs = {"id": "r4"}, []
    for target in ("windows", "linux", "windows", "linux"):
        assert api_agent._guard(run, _call({"target": target}), sigs) is None