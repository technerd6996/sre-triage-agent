"""Disk questions in the approver chat: read-only, answered through the inquiry credential path."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import approver_chat as ac
from app.tools import actor, infra

INST = {"name": "srv1", "platform": "windows", "instance_id": "srv1"}


class _Tool:
    def __init__(self, out, takes_inquiry_user=False):
        self.out, self.calls, self.inquirer = out, [], None
        self.args = {"target": {}, **({"inquiry_user": {}} if takes_inquiry_user else {})}

    def invoke(self, args):
        self.calls.append(args)
        self.inquirer = actor.inquirer()
        return self.out


DISKS = {"target": "windows", "threshold_pct": 85, "over_threshold": [],
         "disks": [{"disk": "C:", "size_gb": 250.0, "free_gb": 100.0, "used_pct": 60.0, "over_threshold": False},
                   {"disk": "D:", "size_gb": 500.0, "free_gb": 50.0, "used_pct": 90.0, "over_threshold": True}]}


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(ac.router)
    disk = _Tool(DISKS)
    monkeypatch.setattr(ac, "_actor", lambda r: "eng@lab.local")
    monkeypatch.setattr(ac, "resolve_server", lambda q, ci_name="": (INST, ""))
    monkeypatch.setattr(ac, "check_disk_usage", disk)
    monkeypatch.setattr(ac, "_INQUIRE_TOOLS", (("disk", disk), ("drive", disk), ("storage", disk)))
    return TestClient(app), disk


def test_a_drive_question_runs_the_disk_check_as_a_read_only_inquiry(client):
    c, disk = client
    r = c.post("/api/approver-chat/inquire", json={"question": "What is the C drive utilization in srv1?"})
    assert r.status_code == 200 and r.json()["result"]["disks"][0]["disk"] == "C:"
    assert disk.calls == [{"target": "windows"}]                  # this tool has no inquiry_user argument, so none is sent
    assert disk.inquirer == "eng@lab.local"                      # but its credential fetch used the inquiry path
    assert actor.inquirer() is None                              # and that is cleared afterwards


def test_tools_that_take_inquiry_user_still_get_it(client, monkeypatch):
    c, _ = client
    cpu = _Tool({"cpu_pct": 4.0}, takes_inquiry_user=True)
    monkeypatch.setattr(ac, "check_cpu_usage", cpu)
    monkeypatch.setattr(ac, "_INQUIRE_TOOLS", (("cpu", cpu),))
    c.post("/api/approver-chat/inquire", json={"question": "cpu on srv1"})
    assert cpu.calls == [{"target": "windows", "inquiry_user": "eng@lab.local"}]


def test_infra_uses_the_inquiry_path_while_a_read_only_check_runs(monkeypatch):
    seen = []
    monkeypatch.setattr(infra.pam, "checkout_for_inquiry", lambda u, *k: seen.append(("inquiry", u)) or {})
    monkeypatch.setattr(infra.pam, "checkout", lambda *k: seen.append(("run",)) or {})
    infra._credentials("windows", "1.2.3.4", None)
    with actor.inquiring_as("eng@lab.local"):
        infra._credentials("windows", "1.2.3.4", None)
    assert seen == [("run",), ("inquiry", "eng@lab.local")]
