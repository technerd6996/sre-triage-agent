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

@pytest.fixture
def saving(rows, monkeypatch):
    """save() without a database: the upsert lands in rows, the audit is dropped."""
    from app import store

    class Table:
        def upsert(self, row):
            rows[row["key"]] = row["value_json"]
            return self

        def execute(self):
            return None
    monkeypatch.setattr(store, "supabase", type("S", (), {"table": lambda self, name: Table()})())
    monkeypatch.setattr(store, "audit", lambda *a, **k: None)
    monkeypatch.setattr(config, "_orig_env", {})
    return rows


def test_an_empty_saved_value_is_not_exported(saving):
    config.save("notifications.aws_profile", "")
    assert "AWS_PROFILE" not in __import__("os").environ
    assert config.hydrate_environ() >= 0 and "AWS_PROFILE" not in __import__("os").environ


def test_a_non_empty_saved_value_is_still_exported(saving):
    import os
    config.save("notifications.aws_profile", "lab")
    assert os.environ["AWS_PROFILE"] == "lab"
    os.environ.pop("AWS_PROFILE")
    config.hydrate_environ()
    assert os.environ["AWS_PROFILE"] == "lab"
    config.save("notifications.aws_profile", "")
    assert "AWS_PROFILE" not in os.environ


def test_clearing_a_value_brings_back_what_the_environment_had(saving, monkeypatch):
    import os
    monkeypatch.setenv("AWS_PROFILE", "from-env")
    config.save("notifications.aws_profile", "lab")
    config.save("notifications.aws_profile", "")
    assert os.environ["AWS_PROFILE"] == "from-env"


def test_an_empty_saved_secret_is_refused_and_a_blank_one_is_not_exported(saving, monkeypatch):
    import os
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    monkeypatch.setenv("TRIAGE_SECRET_KEY", key.decode())
    with pytest.raises(ValueError, match="cannot be empty"):
        config.save("notifications.aws_access_key_id", "")
    saving["sec.notifications.aws_access_key_id"] = {"v": Fernet(key).encrypt(b"  ").decode(), "ver": 1}
    config.hydrate_environ()
    assert "AWS_ACCESS_KEY_ID" not in os.environ
