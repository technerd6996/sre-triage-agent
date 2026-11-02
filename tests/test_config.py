import pytest

from app import config


@pytest.fixture
def rows(monkeypatch):
    data = {}
    monkeypatch.setattr(config, "_rows", lambda: data)
    for s in config.SETTINGS.values():
        if s.env:
            monkeypatch.delenv(s.env, raising=False)
    return data


def test_default_when_nothing_is_set(rows):
    assert config.get("agent.approval_timeout_min") == 30
    assert config.get("agent.checker_mode") == "shadow"


def test_env_is_used_when_nothing_is_saved(rows, monkeypatch):
    monkeypatch.setenv("APPROVAL_TIMEOUT_MIN", "45")
    assert config.get("agent.approval_timeout_min") == 45


def test_saved_value_beats_env(rows, monkeypatch):
    monkeypatch.setenv("APPROVAL_TIMEOUT_MIN", "45")
    rows["cfg.agent.approval_timeout_min"] = 20
    assert config.get("agent.approval_timeout_min") == 20


def test_old_admin_row_still_works(rows):
    rows["approval_timeout_min"] = 12
    assert config.get("agent.approval_timeout_min") == 12
    rows["cfg.agent.approval_timeout_min"] = 7
    assert config.get("agent.approval_timeout_min") == 7


def test_bad_saved_value_is_ignored(rows, monkeypatch):
    rows["cfg.agent.approval_timeout_min"] = "abc"
    monkeypatch.setenv("APPROVAL_TIMEOUT_MIN", "45")
    assert config.get("agent.approval_timeout_min") == 45


def test_checker_mode_matches_the_old_behaviour(rows, monkeypatch):
    monkeypatch.setenv("CHECKER_MODE", "nonsense")
    assert config.get("agent.checker_mode") == "shadow"
    monkeypatch.setenv("CHECKER_MODE", "ON")
    assert config.get("agent.checker_mode") == "on"


def test_validate():
    assert config.validate("agent.p1_time_budget_min", "20") == 20
    assert config.validate("agent.verify_soak_seconds", "0") == 0.0
    for key, bad in (("agent.approval_timeout_min", 0), ("agent.approval_timeout_min", "x"),
                     ("agent.checker_mode", "maybe"), ("agent.auto_max_fails", 2.5)):
        with pytest.raises(ValueError):
            config.validate(key, bad)


def test_describe_reports_the_source(rows, monkeypatch):
    monkeypatch.setenv("AUTO_MAX_FAILS", "5")
    d = {x["key"]: x for x in config.describe()}
    assert d["agent.auto_max_fails"]["value"] == 5 and d["agent.auto_max_fails"]["source"] == "env"
    assert d["agent.poll_seconds"]["source"] == "default"