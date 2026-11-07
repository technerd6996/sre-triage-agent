import json
import os

import pytest

from app import config, envimport

ENV = """
# comment
DATABASE_URL=postgresql://u:p@127.0.0.1:5432/x
TRIAGE_SECRET_KEY=abc
export SNOW_INSTANCE="https://acme.service-now.com"
SNOW_PASSWORD='p@ss #1'
APPROVAL_TIMEOUT_MIN=45
CHECKER_MODE=maybe
WIN_PASSWORD=hunter2
TRIAGE_USERS=a:b
DISK_THRESHOLD_PCT=90
SNOW_ESCALATION_GROUP=
"""


@pytest.fixture
def rows(monkeypatch):
    data = {}
    monkeypatch.setattr(config, "_rows", lambda: data)
    monkeypatch.setenv("TRIAGE_SECRET_KEY", "x")             # preview only checks that a key exists
    return data


def test_parse_handles_quotes_comments_and_empty_values():
    p = envimport.parse(ENV)
    assert p["SNOW_PASSWORD"] == "p@ss #1" and p["SNOW_INSTANCE"] == "https://acme.service-now.com"
    assert "SNOW_ESCALATION_GROUP" not in p


def test_classification():
    c = envimport.classify
    assert c("TRIAGE_SECRET_KEY") == "bootstrap" and c("WIN_PASSWORD") == "host"
    assert c("TRIAGE_USERS") == "retired" and c("WF_ADMIN") == "retired"
    assert c("SNOW_PASSWORD") == "secret" and c("SNOW_INSTANCE") == "setting"
    assert c("DISK_THRESHOLD_PCT") == "setting"      # now managed in the admin pane
    assert c("SOME_UNKNOWN_VARIABLE") == "unmanaged"


def test_preview_flags_invalid_and_never_echoes_values_it_must_hide(rows):
    p = envimport.preview(ENV)
    by = {i["name"]: i for i in p["items"]}
    assert by["CHECKER_MODE"]["status"] == "invalid"
    assert by["APPROVAL_TIMEOUT_MIN"]["status"] == "new" and by["APPROVAL_TIMEOUT_MIN"]["value"] == 45
    assert by["SNOW_PASSWORD"]["masked"] is True
    text = json.dumps(p)
    for hidden in ("p@ss", "hunter2", "sb_secret_abc", "supabase.co"):
        assert hidden not in text
    assert {"DATABASE_URL", "WIN_PASSWORD"} <= set(p["keep_in_env"])
    assert "DISK_THRESHOLD_PCT" not in p["keep_in_env"]
    assert "TRIAGE_USERS" not in p["keep_in_env"]


def test_same_value_is_not_saved_again(rows):
    rows["cfg.agent.approval_timeout_min"] = 45
    by = {i["name"]: i for i in envimport.preview("APPROVAL_TIMEOUT_MIN=45")["items"]}
    assert by["APPROVAL_TIMEOUT_MIN"]["status"] == "same"


def test_apply_refuses_secrets_without_a_key(rows, monkeypatch):
    monkeypatch.delenv("TRIAGE_SECRET_KEY", raising=False)
    with pytest.raises(envimport.ImportBlocked):
        envimport.apply("SNOW_PASSWORD=x", "tester")


def test_apply_saves_nothing_when_a_line_is_invalid(rows):
    res = envimport.apply("APPROVAL_TIMEOUT_MIN=45\nCHECKER_MODE=maybe", "tester")
    assert res["ok"] is False and res["errors"][0]["name"] == "CHECKER_MODE"


def test_secret_roundtrip_and_get_refuses_secrets(monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    token = config._fernet().encrypt(b"s3cret").decode()
    monkeypatch.setattr(config, "_rows", lambda: {"sec.ticketing.password": {"v": token, "ver": 2, "at": 5}})
    assert config.get_secret("ticketing.password") == "s3cret"
    with pytest.raises(ValueError):
        config.get("ticketing.password")
    d = {x["key"]: x for x in config.describe("ticketing")}["ticketing.password"]
    assert d["value"] is None and d["is_set"] is True and d["version"] == 2


def test_hydrate_environ_copies_only_saved_values(rows, monkeypatch):
    monkeypatch.setenv("SNOW_CALLER", "from_env")            # monkeypatch restores it after the test
    monkeypatch.delenv("SKILLS_TTL_SECONDS", raising=False)
    rows["cfg.ticketing.caller"] = "svc_saved"
    assert config.hydrate_environ() >= 1
    assert os.environ["SNOW_CALLER"] == "svc_saved"
    assert "SKILLS_TTL_SECONDS" not in os.environ