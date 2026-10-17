"""Unit tests for app/checker.py. No network, no ServiceNow."""
from app import checker


def _run(skill, *calls):
    return {"id": "t1", "ticket": "INC0000001", "skill": skill,
            "events": [{"type": "tool_call", "name": n, "args": a} for n, a in calls]}


def _seq(monkeypatch, results):
    it = iter(results)
    calls = []
    monkeypatch.setattr(checker, "_check", lambda kind, args: (calls.append(kind), next(it))[1])
    monkeypatch.setenv("VERIFY_SOAK_SECONDS", "0")
    return calls


def test_service_spec_uses_the_last_service_call():
    run = _run("linux-service-down",
               ("check_service_status", {"target": "linux", "service_name": "nginx"}),
               ("start_service", {"target": "linux", "service_name": "nginx"}))
    assert checker._spec(run) == ("service", {"target": "linux", "service_name": "nginx"})


def test_disk_spec_falls_back_to_the_skill_platform():
    assert checker._spec(_run("windows-disk-full")) == ("disk", {"target": "windows"})
    assert checker._spec(_run("linux-disk-full", ("check_disk_usage", {"target": "linux"}))) == ("disk", {"target": "linux"})


def test_ad_spec_needs_a_username():
    assert checker._spec(_run("ad-account-locked", ("lookup_ad_user", {"username": "jdoe"}))) == ("ad", {"username": "jdoe"})
    assert checker._spec(_run("ad-account-locked")) is None


def test_no_spec_for_read_only_skills():
    assert checker._spec(_run("aws-server-unreachable")) is None


def test_service_needs_two_clean_checks(monkeypatch):
    calls = _seq(monkeypatch, [(True, "up"), (True, "still up")])
    assert checker._run_checks("service", {}) == (True, ["up", "still up"])
    assert calls == ["service", "service"]


def test_service_that_dies_again_fails(monkeypatch):
    _seq(monkeypatch, [(True, "up"), (False, "down")])
    assert checker._run_checks("service", {}) == (False, ["up", "down"])


def test_first_failure_stops_early(monkeypatch):
    calls = _seq(monkeypatch, [(False, "down"), (True, "never reached")])
    assert checker._run_checks("service", {})[0] is False
    assert len(calls) == 1


def test_unreachable_host_is_unverified_not_failed(monkeypatch):
    _seq(monkeypatch, [(None, "could not check")])
    assert checker._run_checks("disk", {})[0] is None


def test_disk_is_checked_once(monkeypatch):
    calls = _seq(monkeypatch, [(True, "ok")])
    checker._run_checks("disk", {})
    assert calls == ["disk"]


def test_mode_defaults_to_shadow(monkeypatch):
    monkeypatch.delenv("CHECKER_MODE", raising=False)
    assert checker.mode() == "shadow"
    monkeypatch.setenv("CHECKER_MODE", "nonsense")
    assert checker.mode() == "shadow"
    monkeypatch.setenv("CHECKER_MODE", "ON")
    assert checker.mode() == "on"

def test_report_has_agent_steps_checker_checks_comment_and_verdict(monkeypatch):
    monkeypatch.setenv("VERIFY_SOAK_SECONDS", "45")
    run = _run("linux-service-down", ("start_service", {"target": "linux", "service_name": "nginx"}))
    run["events"] += [{"type": "tool_result", "name": "start_service", "content": "{}", "ts": 0},
                      {"type": "final", "text": "nginx restarted"},
                      {"type": "verification", "text": "Checker: verifying"}]
    text = checker._report(run, "service", ["nginx is active", "nginx is active"], "pass", "Fix confirmed.")
    assert "1. Actions by the TRIAGE-1 agent" in text
    assert "Agent summary: nginx restarted" in text
    assert "Check 1: nginx is active" in text and "Check 2 (after 45s)" in text
    assert "3. Checker comment: Fix confirmed." in text and "Verdict: PASS" in text
    assert "Checker: verifying" not in text