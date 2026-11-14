"""A ticket whose server cannot be found still gets the playbook of the right OS (CMDB record, else the ticket text)."""
import pytest

from app.agent import agent as ag
from app.agent import hosts
from app.ports.models import Host

ZABBIX = '[Zabbix]: term-win-srv - Windows: "AppXSvc" (AppX Deployment Service (AppXSVC)) is not running (startup type automatic)'


class FakeCmdb:
    rows = {"term-win-srv": "windows", "victim-test": "linux", "blank": ""}

    def __init__(self, managed_only=True):
        assert managed_only is False            # the OS hint reads every CI, tagged or not

    def _h(self, name):
        return Host(id=name, name=name, os=self.rows[name], state="running", address="", private_address="",
                    provider="cmdb") if name in self.rows else None

    def find_by_name(self, name):
        return self._h(name)

    def find_in_text(self, text):
        hits = [n for n in self.rows if n in (text or "")]
        return self._h(hits[0]) if len(hits) == 1 else None


@pytest.fixture
def cmdb(monkeypatch):
    import app.adapters.cmdbhosts.hosts as ch
    monkeypatch.setattr(ch, "CmdbHosts", FakeCmdb)


@pytest.mark.parametrize("text,want", [
    ("Windows: service is not running", "windows"),
    ("nginx down on ubuntu box", "linux"),
    ("systemctl shows the unit failed", "linux"),
    ("disk full, windows and linux hosts both", ""),     # both named: no guess
    ("disk full", ""),
    ("swindows-like word is not a platform", ""),
])
def test_the_platform_is_read_from_the_ticket_text(text, want):
    assert hosts._platform_from_text(text) == want


def test_the_cmdb_record_wins_over_the_text(cmdb):
    assert hosts.guess_platform("linux agent alert on term-win-srv") == ("windows", "from the CMDB record term-win-srv")
    assert hosts.guess_platform("service down", "victim-test") == ("linux", "from the CMDB record victim-test")


def test_the_text_is_used_when_the_cmdb_has_no_os_or_no_record(cmdb):
    assert hosts.guess_platform("Windows: service down on blank") == ("windows", "from the ticket text")
    assert hosts.guess_platform("Windows: service down on nobody") == ("windows", "from the ticket text")
    assert hosts.guess_platform("service down on nobody") == ("", "")


def test_a_cmdb_failure_falls_back_to_the_text(monkeypatch):
    import app.adapters.cmdbhosts.hosts as ch

    class Down:
        def find_by_name(self, n):
            raise RuntimeError("ServiceNow is down")
        find_in_text = find_by_name
    monkeypatch.setattr(ch, "CmdbHosts", Down)
    assert hosts.guess_platform("Windows: x", "term-win-srv") == ("windows", "from the ticket text")


def test_an_unknown_windows_server_gets_a_windows_playbook_and_only_escalation_tools(cmdb, monkeypatch):
    seen = {}
    ticket = type("T", (), {"invoke": staticmethod(lambda a: {"short_description": ZABBIX, "description": "", "cmdb_ci": ""})})
    monkeypatch.setattr(ag, "get_incident", ticket)
    monkeypatch.setattr(ag, "resolve_server", lambda text, ci="": (None, "No known instance is named in the ticket."))

    def playbook(text, platform=None):
        seen["platform"] = platform
        return "PB", "windows-service-down", {"run_ps", "claim_incident"}, "v1"
    monkeypatch.setattr(ag, "build_playbook", playbook)
    monkeypatch.setattr(ag, "get_agent", lambda tools: None)

    def run(agent, payload, config, number):
        seen["msg"] = payload["messages"][0]["content"]
        yield {"type": "done"}
    monkeypatch.setattr(ag, "_run_guarded", run)
    events = list(ag.start("INC0010899", "INC0010899-abc"))
    assert seen["platform"] == "windows"
    assert "Affected server: unknown" in seen["msg"] and "windows server (from the CMDB record term-win-srv)" in seen["msg"]
    assert set(events[0]["tools"]) == {"claim_incident", "get_incident", "add_work_note", "escalate_incident"}
    assert events[0]["host"] is None and events[0]["skill"] == "windows-service-down"      # still recorded on the run
    # The technical playbook is NOT given: its steps name tools this run does not have, and the model then writes
    # those calls as text instead of escalating (INC0010936).
    playbook = seen["msg"].split("PLAYBOOK:", 1)[1]
    assert "PB" not in playbook and "TECHNICAL SKILL: none (the affected server could not be found)" in playbook
    assert "Server not found" in playbook and "managed-server tag" in playbook


def test_an_unmanaged_server_is_escalated_saying_so(cmdb, monkeypatch):
    seen = {}
    ticket = type("T", (), {"invoke": staticmethod(lambda a: {"short_description": ZABBIX, "description": "", "cmdb_ci": ""})})
    monkeypatch.setattr(ag, "get_incident", ticket)
    why = "Server term-win-srv is not managed by TRIAGE-1 App: its ServiceNow CMDB record does not have the tag 'x'."
    monkeypatch.setattr(ag, "resolve_server", lambda text, ci="": (None, why))
    monkeypatch.setattr(ag, "get_agent", lambda tools: None)

    def run(agent, payload, config, number):
        seen["msg"] = payload["messages"][0]["content"]
        yield {"type": "done"}
    monkeypatch.setattr(ag, "_run_guarded", run)
    list(ag.start("INC0010942", "INC0010942-abc"))
    playbook = seen["msg"].split("PLAYBOOK:", 1)[1]
    assert "Reason: Server term-win-srv is not managed by TRIAGE-1 App" in playbook
    assert "Server not found" not in playbook and "escalate_incident with that reason" in playbook
