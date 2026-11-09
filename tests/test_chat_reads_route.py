"""The /inquire route with the newer read-only checks, and which server a question runs on."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import hosts
from app.api import approver_chat as ac
from app.tools import infra

LIN = {"name": "lin1", "platform": "linux", "instance_id": "lin1"}
WIN = {"name": "win1", "platform": "windows", "instance_id": "win1"}


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(ac.router)
    cmds = []
    monkeypatch.setattr(ac, "_actor", lambda r: "eng@lab.local")
    monkeypatch.setattr(ac, "resolve_server", lambda q, ci_name="": (LIN, ""))
    monkeypatch.setattr(infra, "_linux", lambda c, **k: cmds.append(c) or "3600\n2026-11-09 01:00:00")
    monkeypatch.setattr(infra, "_win", lambda s, **k: cmds.append(s) or '{"boot":"2026-11-01 08:00:00","seconds":600}')
    return TestClient(app), cmds


def test_an_uptime_question_runs_the_uptime_check(client):
    c, cmds = client
    r = c.post("/api/approver-chat/inquire", json={"question": "what is the uptime of lin1"})
    assert r.status_code == 200 and "up 1 h 0 min" in r.json()["result"]["summary"] and "/proc/uptime" in cmds[0]


def test_a_network_question_without_a_target_asks_for_one(client):
    c, cmds = client
    r = c.post("/api/approver-chat/inquire", json={"question": "can lin1 reach it"})
    assert r.status_code == 422 and "Which host or address" in r.json()["detail"] and cmds == []


def test_the_checks_use_the_read_only_inquiry_credential_path(client, monkeypatch):
    c, _ = client
    seen = []
    monkeypatch.setattr(infra.pam, "checkout_for_inquiry", lambda u, *k: seen.append(u) or {})
    monkeypatch.setattr(infra, "_linux", lambda c_, **k: (seen.append(infra.actor.inquirer()) or "3600\n2026-11-09 01:00:00"))
    c.post("/api/approver-chat/inquire", json={"question": "uptime of lin1"})
    assert seen == ["eng@lab.local"]


def test_a_windows_server_asked_about_package_updates_gets_windows_updates(client, monkeypatch):
    c, cmds = client
    monkeypatch.setattr(ac, "resolve_server", lambda q, ci_name="": (WIN, ""))
    monkeypatch.setattr(infra, "_win", lambda s, **k: cmds.append(s) or
                        '{"pending": [], "installed": [], "wu_service": "Running", "search_error": ""}')
    r = c.post("/api/approver-chat/inquire", json={"question": "which packages are upgradable on win1"})
    assert r.status_code == 200 and "Windows Update service is Running" in r.json()["result"]["summary"]


# ---------------------------------------------------------------- which server
class _Hosts:
    def __init__(self, names):
        self.names = names

    def find_in_text(self, text):
        hits = [n for n in self.names if n in (text or "")]
        return SimpleNamespace(id=hits[0], name=hits[0], os="windows", state="running", address="1.2.3.4",
                               private_address="1.2.3.4", provider="x") if len(hits) == 1 else None


def test_a_server_named_in_the_message_wins_over_the_remembered_one(monkeypatch):
    monkeypatch.setattr(hosts.registry, "get_hosts", lambda: _Hosts(["srv-a", "srv-b"]))
    inst, _ = hosts.resolve_server("cpu on srv-b", ci_name="srv-a")
    assert inst["name"] == "srv-b"


def test_the_remembered_server_is_used_when_the_message_names_none_or_several(monkeypatch):
    monkeypatch.setattr(hosts.registry, "get_hosts", lambda: _Hosts(["srv-a", "srv-b"]))
    assert hosts.resolve_server("what about memory", ci_name="srv-a")[0]["name"] == "srv-a"
    assert hosts.resolve_server("ping srv-b from srv-a", ci_name="srv-a")[0]["name"] == "srv-a"
