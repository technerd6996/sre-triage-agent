"""Approver quick checks: 'CPU & memory' runs both, and a remembered server name is found again."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import approver_chat as ac


class _T:
    args = {"target": {}, "inquiry_user": {}}      # what a real tool's schema exposes

    def __init__(self, out):
        self.out = out

    def invoke(self, args):
        return self.out


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(ac.router)
    monkeypatch.setattr(ac, "resolve_server", lambda q, ci_name="": ({"name": "srv1", "platform": "windows"}, ""))
    monkeypatch.setattr(ac, "_actor", lambda r: "eng@lab.local")
    monkeypatch.setattr(ac, "check_cpu_usage", _T({"cpu_pct": 4.7}))
    monkeypatch.setattr(ac, "check_memory_usage", _T({"used_pct": 61.0}))
    monkeypatch.setattr(ac, "_INQUIRE_TOOLS", (("cpu", ac.check_cpu_usage), ("memory", ac.check_memory_usage)))
    return TestClient(app)


def test_cpu_and_memory_in_one_question_runs_both(client):
    r = client.post("/api/approver-chat/inquire", json={"question": "What is the CPU & Memory utilization in srv1?"})
    assert r.status_code == 200
    assert [x.get("cpu_pct", x.get("used_pct")) for x in r.json()["results"]] == [4.7, 61.0]


def test_memory_only_runs_one(client):
    r = client.post("/api/approver-chat/inquire", json={"question": "What about memory?", "server": "srv1"})
    assert len(r.json()["results"]) == 1 and r.json()["results"][0]["used_pct"] == 61.0


def test_remembered_server_is_found_when_the_provider_has_no_find_by_name(monkeypatch):
    from app.agent import hosts
    h = SimpleNamespace(id="srv1", name="srv1", os="windows", state="running", address="1.2.3.4",
                        private_address="1.2.3.4", provider="aws")

    class P:                                    # like the AWS provider: only find_in_text
        def find_in_text(self, t):
            return h if "srv1" in t else None

        def list_hosts(self):
            return [h]

    monkeypatch.setattr(hosts.registry, "get_hosts", lambda: P())
    inst, why = hosts.resolve_server("What about memory?", ci_name="srv1")
    assert inst and inst["name"] == "srv1" and not why


def test_automatic_backlog_does_not_block_an_approver_request(monkeypatch):
    from app import config
    app = FastAPI()
    app.include_router(ac.router)
    monkeypatch.setattr(ac, "_actor", lambda r: "eng@lab.local")
    monkeypatch.setattr(ac, "resolve_server", lambda q, ci_name="": (None, "no server"))   # stops after the queue check
    monkeypatch.setattr(ac.jobs, "queued", lambda: [{"source": "auto"}] * (config.get("limits.queue_max") + 5))
    r = TestClient(app).post("/api/approver-chat/run", json={"instruction": "restart spooler"})
    assert r.status_code == 422                                  # got past the queue check, not 503
    monkeypatch.setattr(ac.jobs, "queued", lambda: [{"source": "approver_adhoc"}] * config.get("limits.queue_max"))
    assert TestClient(app).post("/api/approver-chat/run", json={"instruction": "restart spooler"}).status_code == 503
