"""Changes from the approver chat: propose (no side effects), single-use confirm, engineer-owned ticket, direct tool call
under the engineer's identity, and the PAM rules that make that safe."""
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import chat_actions, config, pam_service
from app.adapters.servicenow import client as snow
from app.api import approver_chat as ac
from app.tools import actor, infra

INST = {"name": "srv1", "platform": "windows", "instance_id": "srv1"}


class FakeTool:
    def __init__(self, result=None):
        self.result, self.calls, self.actor_seen = result or {"status_before": "stopped", "status": "active"}, [], None

    def invoke(self, args):
        self.calls.append(args)
        self.actor_seen = actor.current()
        return self.result


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    chat_actions._PENDING.clear()
    monkeypatch.setattr(chat_actions, "_TOOLS", {n: FakeTool() for n in (
        "restart_service", "start_service", "clear_temp_files", "clean_old_logs", "unlock_ad_account")}, raising=False)
    monkeypatch.setattr(chat_actions, "_tool", lambda n: chat_actions._TOOLS.get(n))


# ---------------------------------------------------------------- propose
def test_propose_reads_the_message_and_changes_nothing():
    tool = chat_actions._TOOLS["restart_service"]
    card = chat_actions.propose("Can we restart the print spooler service? (server: srv1)", "eng@lab.local", INST)
    assert (card["tool"], card["name"], card["host"], card["level"]) == ("restart_service", "Spooler", "srv1", 1)
    assert tool.calls == []                                   # nothing ran


def test_a_single_word_service_not_in_the_table_is_used_as_typed():
    assert chat_actions.propose("restart nginx", "e", INST)["name"] == "nginx"


@pytest.mark.parametrize("msg", ["what is the weather", "restart", "restart the really big old service thing"])
def test_unclear_requests_are_asked_back(msg):
    with pytest.raises(chat_actions.AskBack):
        chat_actions.propose(msg, "e", INST)


@pytest.mark.parametrize("level", [2, 3])
def test_high_risk_tools_are_not_run_from_the_chat(monkeypatch, level):
    monkeypatch.setattr(chat_actions.policy, "risk_level", lambda t: level)
    with pytest.raises(chat_actions.AskBack):
        chat_actions.propose("restart spooler", "e", INST)


# ---------------------------------------------------------------- tokens
def _card():
    return chat_actions.propose("restart spooler", "eng@lab.local", INST)


def test_a_token_works_once():
    t = _card()["token"]
    assert chat_actions.redeem(t, "eng@lab.local")["name"] == "Spooler"
    with pytest.raises(ValueError):
        chat_actions.redeem(t, "eng@lab.local")


def test_a_token_belongs_to_the_person_who_asked():
    with pytest.raises(ValueError):
        chat_actions.redeem(_card()["token"], "someone@else")


def test_an_expired_token_is_refused():
    t = _card()["token"]
    chat_actions._PENDING[t]["expires"] = time.time() - 1
    with pytest.raises(ValueError):
        chat_actions.redeem(t, "eng@lab.local")


# ---------------------------------------------------------------- the confirm route
class FakeTickets:
    def __init__(self, boom=False):
        self.boom, self.created, self.notes = boom, [], []

    def create_for_user(self, title, description, user_email, urgency, impact, host, note=""):
        if self.boom:
            raise RuntimeError("ServiceNow down")
        self.created.append((title, user_email, host))
        return SimpleNamespace(id="INC9000001")

    def add_note(self, number, note):
        self.notes.append((number, note))


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(ac.router)
    tickets = FakeTickets()
    audits = []
    monkeypatch.setattr(ac, "_actor", lambda r: "eng@lab.local")
    monkeypatch.setattr(ac, "resolve_server", lambda q, ci_name="": (INST, ""))
    monkeypatch.setattr(ac.registry, "get_ticketing", lambda role="agent": tickets)
    monkeypatch.setattr(ac.store, "audit", lambda *a, **k: audits.append(a))
    return TestClient(app), tickets, audits


def test_confirm_opens_an_engineer_ticket_then_runs_the_tool_as_the_engineer(client):
    c, tickets, audits = client
    token = c.post("/api/approver-chat/act/propose", json={"instruction": "restart spooler"}).json()["token"]
    r = c.post("/api/approver-chat/act/confirm", json={"token": token})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["ticket"] == "INC9000001"
    assert tickets.created[0][1:] == ("eng@lab.local", "srv1")            # owned by the engineer
    tool = chat_actions._TOOLS["restart_service"]
    assert tool.calls == [{"target": "windows", "service_name": "Spooler"}]
    assert tool.actor_seen == ("eng@lab.local", "INC9000001")             # identity the PAM path sees
    assert actor.current() is None                                        # and it is cleared afterwards
    assert tickets.notes and "Restarted Spooler on srv1" in tickets.notes[0][1]
    assert any(a[3] == "chat_action_done" and a[2] == "eng@lab.local" for a in audits)


def test_no_ticket_means_no_change(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setattr(ac.registry, "get_ticketing", lambda role="agent": FakeTickets(boom=True))
    token = c.post("/api/approver-chat/act/propose", json={"instruction": "restart spooler"}).json()["token"]
    r = c.post("/api/approver-chat/act/confirm", json={"token": token})
    assert r.status_code == 502 and "nothing was changed" in r.json()["detail"]
    assert chat_actions._TOOLS["restart_service"].calls == []


def test_confirm_twice_runs_once(client):
    c, _, _ = client
    token = c.post("/api/approver-chat/act/propose", json={"instruction": "restart spooler"}).json()["token"]
    assert c.post("/api/approver-chat/act/confirm", json={"token": token}).status_code == 200
    assert c.post("/api/approver-chat/act/confirm", json={"token": token}).status_code == 409
    assert len(chat_actions._TOOLS["restart_service"].calls) == 1


def test_a_failed_action_is_reported_and_recorded(client, monkeypatch):
    c, tickets, audits = client
    monkeypatch.setattr(chat_actions, "_TOOLS", {"restart_service": FakeTool({"error": "access denied"})}, raising=False)
    token = c.post("/api/approver-chat/act/propose", json={"instruction": "restart spooler"}).json()["token"]
    r = c.post("/api/approver-chat/act/confirm", json={"token": token}).json()
    assert r["ok"] is False and "access denied" in r["message"]
    assert any(a[3] == "chat_action_failed" for a in audits)


# ---------------------------------------------------------------- credentials
def test_agent_checkout_still_needs_a_run():
    with pytest.raises(RuntimeError, match="no active agent run"):
        pam_service.checkout("srv1", "1.2.3.4")


def test_infra_uses_the_action_path_for_a_person_and_the_run_path_otherwise(monkeypatch):
    seen = []
    monkeypatch.setattr(infra.pam, "checkout_for_action", lambda u, t, *k: seen.append(("action", u, t)) or {})
    monkeypatch.setattr(infra.pam, "checkout", lambda *k: seen.append(("run",)) or {})
    infra._credentials("windows", "1.2.3.4", None)
    with actor.acting_as("eng@lab.local", "INC1"):
        infra._credentials("windows", "1.2.3.4", None)
    assert seen == [("run",), ("action", "eng@lab.local", "INC1")]


def test_action_checkouts_are_limited_per_person_and_audited(monkeypatch):
    audits = []
    monkeypatch.setattr("app.store.audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(pam_service.registry, "get_credentials",
                        lambda: SimpleNamespace(provider="t", fetch=lambda *k: ("srv1", {"username": "u", "password": "p"})))
    real = config.get
    monkeypatch.setattr(pam_service.config, "get",
                        lambda k: 2 if k == "credentials.action_max_per_user_per_hour" else real(k))
    pam_service._action_per_user.clear()
    for _ in range(3):                                                     # one restart = 3 fetches = ONE action
        pam_service.checkout_for_action("eng@lab.local", "INC1", "srv1")
    pam_service.checkout_for_action("eng@lab.local", "INC2", "srv1")       # second action, still within the limit of 2
    with pytest.raises(RuntimeError, match="limit"):
        pam_service.checkout_for_action("eng@lab.local", "INC3", "srv1")   # third distinct ticket is refused
    pam_service.checkout_for_action("eng@lab.local", "INC1", "srv1")       # but the first ticket may carry on
    pam_service.checkout_for_action("other@lab.local", "INC9", "srv1")     # a different person is unaffected
    assert any(a[3] == "pam_action_rate_limited" and a[2] == "eng@lab.local" for a in audits)


# ---------------------------------------------------------------- ServiceNow ticket
def _snow_calls(monkeypatch, user_sys_id):
    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw))
        if path == "sys_user":
            return [{"sys_id": user_sys_id}] if user_sys_id and "email=" in kw["params"]["sysparm_query"] else (
                [{"sys_id": "svc"}] if user_sys_id is None else [])
        if method == "POST":
            return {"sys_id": "abc", "number": "INC1"}
        return {}
    monkeypatch.setattr(snow, "_request", fake)
    monkeypatch.setattr(snow, "find_ci_id", lambda n: None)
    monkeypatch.setattr(snow, "_fetch", lambda n, identity="agent": {"number": n})
    return calls


def test_the_ticket_is_the_engineers_and_not_in_the_agent_group(monkeypatch):
    calls = _snow_calls(monkeypatch, "eng-sys")
    snow.create_incident_for("t", "d", "eng@lab.local", ci="srv1")
    body = next(kw["json"] for m, p, kw in calls if m == "POST")
    assert body["assigned_to"] == "eng-sys" and body["caller_id"] == "eng-sys" and "assignment_group" not in body


def test_an_unknown_email_still_gets_an_unassigned_ticket_with_a_note(monkeypatch):
    calls = _snow_calls(monkeypatch, None)
    monkeypatch.setattr(snow, "_user_for", lambda login: None)            # neither the email nor the user name matches anyone
    monkeypatch.setattr(snow, "_user_id", lambda u: "svc")
    snow.create_incident_for("t", "d", "nobody@lab.local")
    body = next(kw["json"] for m, p, kw in calls if m == "POST")
    patch = next(kw["json"] for m, p, kw in calls if m == "PATCH")
    assert "assigned_to" not in body and "assignment_group" not in body
    assert "nobody@lab.local" in patch["work_notes"]


# ---------------------------------------------------------------- closing a ticket from the chat
from app.ports.models import Ticket, TicketState


class CloseTickets(FakeTickets):
    def __init__(self, ticket=None, boom=False):
        super().__init__()
        self.ticket, self.resolved, self.boom_resolve = ticket, [], boom

    def get(self, number):
        if self.ticket is None:
            raise RuntimeError("not found")
        return self.ticket

    def resolve(self, number, notes):
        if self.boom_resolve:
            raise RuntimeError("SNOW did not accept close_code")
        self.resolved.append((number, notes))


def _closer(monkeypatch, ticket, history, boom=False):
    app = FastAPI()
    app.include_router(ac.router)
    tk, audits = CloseTickets(ticket, boom), []
    monkeypatch.setattr(ac, "_actor", lambda r: "eng@lab.local")
    monkeypatch.setattr(ac.registry, "get_ticketing", lambda role="agent": tk)
    monkeypatch.setattr(ac.store, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(ac, "_own_history", lambda number, who: history)
    return TestClient(app), tk, audits


MINE = [{"action": "chat_action_confirmed", "detail": ""},
        {"action": "chat_action_done", "detail": "restart_service Spooler srv1: Restarted Spooler on srv1: active -> active."}]


def _t(state=TicketState.IN_PROGRESS, group=""):
    return Ticket(id="INC0010689", state=state, group=group)


def test_close_intent_needs_both_a_verb_and_a_ticket_number():
    assert chat_actions.close_intent("INC0010689 - close the incident")
    assert chat_actions.close_intent("please resolve inc0010689")
    assert not chat_actions.close_intent("close the door") and not chat_actions.close_intent("restart INC0010689")
    assert chat_actions.ticket_in("close inc0010689 now") == "INC0010689"


def test_a_ticket_opened_from_the_chat_can_be_resolved_with_drafted_notes(monkeypatch):
    c, tk, audits = _closer(monkeypatch, _t(), MINE)
    card = c.post("/api/approver-chat/act/propose", json={"instruction": "INC0010689 - close the incident"}).json()
    assert card["kind"] == "close" and "Restarted Spooler on srv1" in card["notes"]
    assert tk.resolved == []                                              # proposing resolves nothing
    r = c.post("/api/approver-chat/act/confirm", json={"token": card["token"]}).json()
    assert r["ok"] and tk.resolved == [("INC0010689", card["notes"])]
    assert any(a[3] == "chat_ticket_resolved" and a[2] == "eng@lab.local" for a in audits)


@pytest.mark.parametrize("ticket,history,why", [
    (None, MINE, "could not find"),
    (_t(TicketState.RESOLVED), MINE, "already resolved"),
    (_t(group="L1-SRE-Agent"), MINE, "agent queue"),
    (_t(), [], "opened from this chat"),
])
def test_closing_is_refused_when_it_is_not_the_engineers_chat_ticket(monkeypatch, ticket, history, why):
    c, tk, _ = _closer(monkeypatch, ticket, history)
    r = c.post("/api/approver-chat/act/propose", json={"instruction": "close INC0010689"})
    assert r.status_code == 422 and why in r.json()["detail"] and tk.resolved == []


def test_a_resolve_failure_is_reported_not_hidden(monkeypatch):
    c, tk, audits = _closer(monkeypatch, _t(), MINE, boom=True)
    token = c.post("/api/approver-chat/act/propose", json={"instruction": "close INC0010689"}).json()["token"]
    r = c.post("/api/approver-chat/act/confirm", json={"token": token}).json()
    assert r["ok"] is False and "close_code" in r["message"] and any(a[3] == "chat_close_failed" for a in audits)
