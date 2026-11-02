from app import config
from app.api import config_pane


def test_every_setting_group_has_a_section():
    assert {s.group for s in config.SETTINGS.values()} <= {s["id"] for s in config_pane.SECTIONS}


def test_secret_fields_never_carry_a_value():
    for sec in config_pane.sections()["sections"]:
        for f in sec["fields"]:
            if f["kind"] == "secret":
                assert f["value"] is None


def test_ticketing_marks_manifest_required_fields():
    sec = next(s for s in config_pane.sections()["sections"] if s["id"] == "ticketing")
    req = {f["key"] for f in sec["fields"] if f["required"]}
    assert {"ticketing.instance", "ticketing.password", "ticketing.escalation_group"} <= req


def test_notifications_test_names_what_is_missing(monkeypatch):
    monkeypatch.setenv("NOTICE_TRANSPORT", "ses")
    monkeypatch.delenv("SES_FROM", raising=False)
    r = config_pane._run_test("notifications")
    assert not r["ok"] and "SES sender" in r["detail"]


def test_model_test_reports_an_offline_server(monkeypatch):
    class R:
        status_code = 503
    monkeypatch.setenv("COLAB_API_URL", "http://model.example/generate")
    monkeypatch.setattr(config_pane.requests, "get", lambda *a, **k: R())
    r = config_pane._run_test("model")
    assert not r["ok"] and "503" in r["detail"]


def test_agent_section_has_no_test():
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        config_pane._run_test("agent")