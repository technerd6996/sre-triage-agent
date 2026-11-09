"""Chat-only change tools (Phase 2): stop, start-up mode, log off, run a job, install packages. Commands are fixed templates,
values are validated, protected names are refused, and the owner mail goes out after a package change."""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import chat_actions, owner_contacts
from app.api import approver_chat as ac
from app.tools import actor, chat_changes as C, infra

WIN = {"name": "win1", "platform": "windows", "instance_id": "win1"}
LIN = {"name": "lin1", "platform": "linux", "instance_id": "lin1"}


class Box:
    """Fake server: records commands, answers from a function."""

    def __init__(self):
        self.win_cmds, self.lin_cmds, self.win, self.lin = [], [], (lambda s: ""), (lambda c: "")

    def _w(self, s, **k):
        self.win_cmds.append(s)
        return self.win(s)

    def _l(self, c, **k):
        self.lin_cmds.append(c)
        return self.lin(c)


@pytest.fixture
def box(monkeypatch):
    b = Box()
    monkeypatch.setattr(infra, "_win", b._w)
    monkeypatch.setattr(infra, "_linux", b._l)
    return b


# ---------------------------------------------------------------- stop service and start-up mode
def test_stop_service_runs_a_fixed_command_and_reports_before_and_after(box, monkeypatch):
    states = iter(["active", "inactive"])
    monkeypatch.setattr(infra, "_service_state", lambda t, s: next(states))
    r = C.stop_service.invoke({"target": "linux", "service_name": "nginx"})
    assert box.lin_cmds == ["sudo -n systemctl stop nginx"] and r["status_before"] == "active" and r["status"] == "inactive"


def test_stop_service_windows(box, monkeypatch):
    monkeypatch.setattr(infra, "_service_state", lambda t, s: "stopped")
    C.stop_service.invoke({"target": "windows", "service_name": "Spooler"})
    assert box.win_cmds == ["Stop-Service -Name 'Spooler' -ErrorAction Stop"]


@pytest.mark.parametrize("name", ["sshd", "WinRM", "TermService", "dbus"])
def test_protected_services_are_never_stopped_or_disabled(box, name):
    assert "protected" in C.stop_service.invoke({"target": "linux", "service_name": name})["error"]
    assert "protected" in C.set_service_startup.invoke({"target": "linux", "service_name": name, "mode": "disabled"})["error"]
    assert box.lin_cmds == [] and box.win_cmds == []


@pytest.mark.parametrize("name", ["nginx; reboot", "a b", "$(id)", "x`y`"])
def test_service_names_cannot_carry_a_command(box, name):
    assert "error" in C.stop_service.invoke({"target": "linux", "service_name": name}) and box.lin_cmds == []


def test_startup_mode_linux_and_windows(box):
    box.lin = lambda c: "enabled" if "is-enabled" in c else ""
    r = C.set_service_startup.invoke({"target": "linux", "service_name": "nginx", "mode": "enabled"})
    assert box.lin_cmds[0] == "sudo -n systemctl enable nginx" and r["startup"] == "enabled"
    box.win = lambda s: "Disabled"
    r = C.set_service_startup.invoke({"target": "windows", "service_name": "Spooler", "mode": "disabled"})
    assert "-StartupType Disabled" in box.win_cmds[0] and r["startup"] == "Disabled"
    assert "mode must be" in C.set_service_startup.invoke({"target": "linux", "service_name": "nginx", "mode": "manual"})["error"]


# ---------------------------------------------------------------- sessions
def test_session_parsing():
    win = [" USERNAME   SESSIONNAME  ID  STATE", " >vedavidyadaran  rdp-tcp#0  2  Active  5  11/9/2026 10:26 PM",
           "  bob   3  Disc  1:02  11/9/2026 9:00 PM"]
    assert C.parse_sessions("windows", win) == [("2", "vedavidyadaran"), ("3", "bob")]
    assert C.parse_sessions("linux", ["alice pts/0 2026-11-09", " 1 1000 alice seat0", "c2 1001 bob"]) == [("1", "alice"), ("c2", "bob")]


def test_logoff_checks_the_session_exists_and_then_logs_it_off(box):
    calls = {"n": 0}

    def win(s):
        if s.startswith("(quser"):
            calls["n"] += 1
            return "USERNAME SESSIONNAME ID STATE\n bob  rdp-tcp#1  3  Active  1  11/9/2026 9:00 PM" if calls["n"] == 1 else ""
        return ""
    box.win = win
    r = C.logoff_session.invoke({"target": "windows", "session_id": "3"})
    assert "logoff 3" in box.win_cmds and r["logged_off"] is True and r["user"] == "bob"
    assert "no session 9" in C.logoff_session.invoke({"target": "windows", "session_id": "9"})["error"]


@pytest.mark.parametrize("sid", ["2; reboot", "abc", "../1", "1 2"])
def test_logoff_refuses_odd_session_ids(box, sid):
    assert "error" in C.logoff_session.invoke({"target": "windows", "session_id": sid}) and not any(c.startswith("logoff") for c in box.win_cmds)


# ---------------------------------------------------------------- scheduled jobs
def test_linux_job_must_be_a_listed_timer_or_service_and_starts_without_waiting(box):
    box.lin = lambda c: ("Mon 2026-11-09 17:30:00 UTC 2min left Mon 2026-11-09 17:20:00 UTC 7min ago "
                         "sysstat-collect.timer sysstat-collect.service") if "list-timers" in c else ""
    r = C.run_scheduled_job.invoke({"target": "linux", "name": "sysstat-collect.timer"})
    assert "sudo -n systemctl start --no-block sysstat-collect.service" in box.lin_cmds and r["job"] == "sysstat-collect.service"
    assert "no systemd timer" in C.run_scheduled_job.invoke({"target": "linux", "name": "evil.service"})["error"]


def test_windows_job_must_exist_outside_microsoft_and_be_unique(box):
    box.win = lambda s: json.dumps({"count": 0, "path": ""}) if "ConvertTo-Json" in s else "Ready"
    assert "no task named" in C.run_scheduled_job.invoke({"target": "windows", "name": "Nope"})["error"]
    box.win = lambda s: json.dumps({"count": 2, "path": ""}) if "ConvertTo-Json" in s else "Ready"
    assert "more than one" in C.run_scheduled_job.invoke({"target": "windows", "name": "Dup"})["error"]
    box.win = lambda s: json.dumps({"count": 1, "path": "\\GoogleSystem\\"}) if "ConvertTo-Json" in s else "Running"
    r = C.run_scheduled_job.invoke({"target": "windows", "name": "Backup"})
    assert r["state"] == "Running" and any("Start-ScheduledTask" in c for c in box.win_cmds)


# ---------------------------------------------------------------- packages
def test_package_names_are_validated_and_protected_ones_refused():
    assert C.validate_packages(["nginx", "curl", "nginx"]) == ["nginx", "curl"]
    for bad in (["sudo"], ["linux-image-6.1"], ["polkit"], ["x; reboot"], ["--force"], []):
        with pytest.raises(ValueError):
            C.validate_packages(bad)
    with pytest.raises(ValueError, match="at most"):
        C.validate_packages([f"pkg{i}" for i in range(11)])


def test_apt_and_dnf_dry_runs_are_read_only_commands():
    assert C._pkg_cmd("apt", "install", ["a"], True).startswith("apt-get -s install -- 'a'")
    assert "--only-upgrade" in C._pkg_cmd("apt", "update", ["a"], True)
    assert C._pkg_cmd("dnf", "install", ["a", "b"], True) == "sudo -n dnf --assumeno install -- 'a' 'b' 2>&1 || true"
    real = C._pkg_cmd("dnf", "update", ["a"], False)
    assert real == "sudo -n dnf -y upgrade -- 'a'" and "su " not in real and "sudo -i" not in real


APT = "Inst nginx (1.24 Ubuntu:24.04 [amd64])\nInst nginx-common (1.24 [all])\nRemv oldlib [1.0]\n"
DNF = ("Last metadata expiration check: 0:01:00 ago.\nDependencies resolved.\n=====\n Package   Arch   Version   Repo   Size\n=====\n"
       "Installing:\n htop      x86_64 3.2.1-87  amazonlinux  100 k\nInstalling dependencies:\n libfoo    x86_64 1.0  amazonlinux 10 k\n\n"
       "Transaction Summary\n=====\nInstall  2 Packages\n")


def test_dry_run_output_is_read_correctly():
    p = C.parse_plan("apt", APT)
    assert p["adds"] == ["nginx", "nginx-common"] and p["removes"] == ["oldlib"]
    assert C.parse_plan("apt", "E: Unable to locate package zzz")["error"] == "Unable to locate package zzz"
    d = C.parse_plan("dnf", DNF)
    assert d["adds"] == ["htop x86_64 3.2.1-87", "libfoo x86_64 1.0"] and d["removes"] == [] and not d["error"]
    assert "Unable to find a match" in C.parse_plan("dnf", "Error: Unable to find a match: zzz")["error"]
    assert C.parse_plan("dnf", "Removing:\n bash x86_64 5.2 @System 1 M\n")["removes"] == ["bash x86_64 5.2"]


def test_install_runs_only_when_the_dry_run_is_clean(box):
    def lin(c):
        if "command -v" in c:
            return "dnf"
        if "--assumeno" in c:
            return DNF
        if "sudo -n dnf -y install" in c:
            return "Installed:\n htop-3.2.1\nComplete!"
        return "htop 3.2.1-87"
    box.lin = lin
    r = C.install_packages.invoke({"target": "linux", "packages": ["htop"], "mode": "install"})
    assert not r.get("error") and any(c == "sudo -n dnf -y install -- 'htop'" for c in box.lin_cmds) and r["changed"] == 2
    assert any(l.startswith("now: htop") for l in r["lines"])


def test_install_refuses_a_plan_that_removes_something(box):
    box.lin = lambda c: "apt" if "command -v" in c else APT
    r = C.install_packages.invoke({"target": "linux", "packages": ["nginx"], "mode": "install"})
    assert "also remove: oldlib" in r["error"] and not any("sudo -n apt-get -y" in c for c in box.lin_cmds)


def test_install_is_linux_only(box):
    assert "Linux" in C.install_packages.invoke({"target": "windows", "packages": ["x"], "mode": "install"})["error"]


# ---------------------------------------------------------------- the agent must not have these either
def test_the_agent_has_none_of_the_change_tools():
    from app.agent import agent
    assert not ({t.name for t in agent.TOOLS} & {t.name for t in C.CHANGE_TOOLS})


# ---------------------------------------------------------------- reading the message into a card
@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(owner_contacts, "contacts", lambda host: {"emails": ["own@lab.local"], "who": [], "error": ""})
    monkeypatch.setattr(owner_contacts, "can_address", lambda: (True, ""))         # a transport that can address people


def test_stop_and_startup_messages(cfg):
    c = chat_actions.propose("stop the print spooler service on win1", "e", WIN)
    assert c["tool"] == "stop_service" and c["name"] == "Spooler" and c["level"] == 1
    c = chat_actions.propose("disable the print spooler service at boot", "e", WIN)
    assert c["tool"] == "set_service_startup" and chat_actions._PENDING[c["token"]]["args"]["mode"] == "disabled"
    with pytest.raises(chat_actions.AskBack, match="protected"):
        chat_actions.propose("stop sshd", "e", LIN)


def test_logoff_needs_a_real_session(monkeypatch, cfg):
    monkeypatch.setattr(C, "find_session", lambda t, s: ("3", "bob") if s == "3" else None)
    c = chat_actions.propose("log off session 3 on win1", "e", WIN)
    assert "bob" in c["title"] and "Unsaved work" in c["detail"][0]
    with pytest.raises(chat_actions.AskBack, match="no session 9"):
        chat_actions.propose("log off session 9", "e", WIN)
    with pytest.raises(chat_actions.AskBack, match="Which session"):
        chat_actions.propose("log off", "e", WIN)


def test_run_job_resolves_the_name(monkeypatch, cfg):
    monkeypatch.setattr(C, "find_job", lambda t, n: "\\Ops\\" + n)
    c = chat_actions.propose("run the task Backup on win1", "e", WIN)
    assert c["name"] == "\\Ops\\Backup"


def test_install_card_shows_the_plan_and_the_owners_who_will_be_mailed(monkeypatch, cfg):
    monkeypatch.setattr(C, "simulate_packages", lambda t, names, mode: {"manager": "dnf", "adds": ["htop x86_64 3.2"], "removes": [],
                                                                       "error": "", "packages": names})
    c = chat_actions.propose("install htop and curl on lin1", "e", LIN)
    assert c["tool"] == "install_packages" and "Install htop, curl on lin1 (dnf)" in c["title"]
    assert any("own@lab.local" in d for d in c["detail"]) and chat_actions._PENDING[c["token"]]["owner_after"] is True
    with pytest.raises(chat_actions.AskBack, match="Windows"):
        chat_actions.propose("install htop", "e", WIN)


def test_install_card_refuses_removals_errors_and_no_ops(monkeypatch, cfg):
    def sim(plan):
        monkeypatch.setattr(C, "simulate_packages", lambda t, n, m: {"manager": "dnf", "packages": n, "adds": [], "removes": [],
                                                                    "error": "", **plan})
    sim({"removes": ["bash"], "adds": ["x"]})
    with pytest.raises(chat_actions.AskBack, match="also remove"):
        chat_actions.propose("install foo", "e", LIN)
    sim({"error": "Unable to find a match: foo"})
    with pytest.raises(chat_actions.AskBack, match="package manager says"):
        chat_actions.propose("install foo", "e", LIN)
    sim({})
    with pytest.raises(chat_actions.AskBack, match="Nothing to do"):
        chat_actions.propose("install foo", "e", LIN)
    with pytest.raises(chat_actions.AskBack, match="protected"):
        chat_actions.propose("install sudo", "e", LIN)


def test_a_card_is_prepared_through_the_read_only_inquiry_path(monkeypatch):
    app = FastAPI()
    app.include_router(ac.router)
    seen = []
    monkeypatch.setattr(ac, "_actor", lambda r: "eng@lab.local")
    monkeypatch.setattr(ac, "resolve_server", lambda q, ci_name="": (LIN, ""))
    monkeypatch.setattr(ac.store, "audit", lambda *a, **k: None)
    monkeypatch.setattr(chat_actions, "propose", lambda m, u, r: seen.append(actor.inquirer()) or {"tool": "t", "name": "n", "host": "h"})
    TestClient(app).post("/api/approver-chat/act/propose", json={"instruction": "install htop"})
    assert seen == ["eng@lab.local"]


# ---------------------------------------------------------------- confirm: the ticket note and the owner mail
class Tickets:
    def __init__(self):
        self.notes = []

    def create_for_user(self, *a, **k):
        return SimpleNamespace(id="INC9100001")

    def add_note(self, n, note):
        self.notes.append(note)


@pytest.fixture
def confirm(monkeypatch):
    app = FastAPI()
    app.include_router(ac.router)
    tk, audits, mails = Tickets(), [], []
    monkeypatch.setattr(ac, "_actor", lambda r: "eng@lab.local")
    monkeypatch.setattr(ac, "resolve_server", lambda q, ci_name="": (LIN, ""))
    monkeypatch.setattr(ac.registry, "get_ticketing", lambda role="agent": tk)
    monkeypatch.setattr(ac.store, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(owner_contacts, "contacts", lambda host: {"emails": ["own@lab.local"], "who": [], "error": ""})
    monkeypatch.setattr(owner_contacts, "send", lambda to, cc, subj, body: mails.append((to, cc, subj, body)) or (True, "id1"))
    monkeypatch.setattr(C, "simulate_packages", lambda t, n, m: {"manager": "dnf", "adds": ["htop x86_64 3.2"], "removes": [], "error": "", "packages": n})
    result = {"target": "linux", "summary": "install finished with dnf: htop", "lines": ["now: htop 3.2.1-87", "log: Complete!"], "changed": 1}
    monkeypatch.setattr(chat_actions, "_tool", lambda name: SimpleNamespace(invoke=lambda a: result))
    c = TestClient(app)

    def go():
        token = c.post("/api/approver-chat/act/propose", json={"instruction": "install htop on lin1"}).json()["token"]
        return c.post("/api/approver-chat/act/confirm", json={"token": token}).json()
    return go, tk, audits, mails


def test_confirm_logs_the_action_on_the_ticket_and_mails_the_owners_afterwards(confirm):
    go, tk, audits, mails = confirm
    r = go()
    assert r["ok"] and r["owner_note"].startswith("Owners mailed: own@lab.local")
    assert "Action: install_packages" in tk.notes[0] and "now: htop 3.2.1-87" in tk.notes[0] and "Owners mailed" in tk.notes[0]
    to, cc, subject, body = mails[0]
    assert to == ["own@lab.local"] and cc == ["eng@lab.local"] and "INC9100001" in subject and "at the request of eng@lab.local" in body
    assert any(a[3] == "chat_owner_mail_sent" for a in audits)


def test_a_mail_failure_is_reported_not_hidden_and_does_not_undo_the_change(confirm, monkeypatch):
    go, tk, audits, mails = confirm
    monkeypatch.setattr(owner_contacts, "send", lambda *a: (False, "SMTP refused"))
    r = go()
    assert r["ok"] and "FAILED" in r["owner_note"] and "SMTP refused" in r["owner_note"]
    assert any(a[3] == "chat_owner_mail_failed" for a in audits)


def test_no_owner_address_is_said_plainly(confirm, monkeypatch):
    go, tk, audits, mails = confirm
    monkeypatch.setattr(owner_contacts, "contacts", lambda host: {"emails": [], "who": [], "error": "lin1 has no owner email in the CMDB"})
    r = go()
    assert r["ok"] and r["owner_note"].startswith("No owner was mailed") and mails == []


# ---------------------------------------------------------------- owner addresses from the CMDB
def test_owner_contacts_read_users_groups_and_the_text_field(monkeypatch):
    def fake(method, path, **kw):
        q = kw["params"]["sysparm_query"]
        if path in ("cmdb_ci_server", "cmdb_ci"):
            return [{"sys_id": "ci", "owned_by": {"value": "u1"}, "managed_by": {"value": "u2"}, "support_group": {"value": "g1"},
                     "u_owner_email": "Shared@Lab.local; ops@lab.local"}]
        if path == "sys_user":
            return [{"sys_id": "u1", "email": "ann@lab.local", "name": "Ann"}, {"sys_id": "u2", "email": "", "name": "Bob"}]
        if path == "sys_user_group":
            return [{"sys_id": "g1", "email": "web-team@lab.local", "name": "Web"}]
        return []
    monkeypatch.setattr(owner_contacts, "_request", fake)
    c = owner_contacts.contacts("web01")
    assert c["emails"] == ["shared@lab.local", "ops@lab.local", "ann@lab.local", "web-team@lab.local"] and not c["error"]


def test_owner_contacts_say_why_when_there_is_nobody(monkeypatch):
    monkeypatch.setattr(owner_contacts, "_request", lambda *a, **k: [{"sys_id": "ci"}])
    assert "no owner email" in owner_contacts.contacts("web01")["error"]
    monkeypatch.setattr(owner_contacts, "_request", lambda *a, **k: [])
    assert "no CMDB record" in owner_contacts.contacts("web01")["error"]

    def boom(*a, **k):
        raise RuntimeError("SNOW 401")
    monkeypatch.setattr(owner_contacts, "_request", boom)
    assert "owner lookup failed" in owner_contacts.contacts("web01")["error"]


def test_a_multi_line_dnf_error_keeps_its_reason():
    text = ("Last metadata expiration check: 0:01:00 ago.\nError: \n Problem: problem with installed package curl-minimal-8\n"
            "  - package curl-minimal-8 conflicts with curl provided by curl-7\n (try to add '--allowerasing' to command line)\n")
    err = C.parse_plan("dnf", text)["error"]
    assert "curl-minimal" in err and "conflicts with curl" in err and "--allowerasing" not in err


def test_a_transport_that_cannot_address_people_is_a_reported_failure(monkeypatch):
    sns = SimpleNamespace(provider="sns", capabilities=frozenset({"broadcast"}), send=lambda *a: "never")
    monkeypatch.setattr(owner_contacts.registry, "get_notifications", lambda: sns)
    ok, why = owner_contacts.can_address()
    assert not ok and "SNS" in why and "cannot address the owners" in why
    assert owner_contacts.send(["a@b.co"], [], "s", "b") == (False, why)
    smtp = SimpleNamespace(provider="smtp", capabilities=frozenset({"recipients"}), send=lambda to, cc, s, b: "id9")
    monkeypatch.setattr(owner_contacts.registry, "get_notifications", lambda: smtp)
    assert owner_contacts.can_address() == (True, "") and owner_contacts.send(["a@b.co"], [], "s", "b") == (True, "id9")


def test_the_install_card_warns_when_owner_mail_cannot_be_sent(monkeypatch, cfg):
    monkeypatch.setattr(owner_contacts, "can_address", lambda: (False, "the mail transport is SNS"))
    monkeypatch.setattr(C, "simulate_packages", lambda t, n, m: {"manager": "dnf", "adds": ["x 1"], "removes": [], "error": "", "packages": n})
    c = chat_actions.propose("install htop on lin1", "e", LIN)
    assert any("Owner mail will NOT be sent" in d and "SNS" in d for d in c["detail"])


def test_owner_mail_can_use_its_own_transport_while_p1_alerts_stay_on_sns(monkeypatch):
    sns = SimpleNamespace(provider="sns", capabilities=frozenset({"broadcast"}), send=lambda *a: "sns-id")
    ses = SimpleNamespace(provider="ses", capabilities=frozenset({"recipients"}), send=lambda to, cc, s, b: "ses-id")
    monkeypatch.setattr(owner_contacts.registry, "get_notifications", lambda: sns)
    monkeypatch.setattr(owner_contacts.registry, "get", lambda port, provider, **k: ses if (port, provider) == ("notifications", "ses") else None)
    real = owner_contacts.config.get
    monkeypatch.setattr(owner_contacts.config, "get", lambda k: "ses" if k == "owners.mail_transport" else real(k))
    assert owner_contacts.can_address() == (True, "") and owner_contacts.send(["a@b.co"], [], "s", "b") == (True, "ses-id")
    assert owner_contacts.registry.get_notifications().provider == "sns"            # the main transport is untouched
    monkeypatch.setattr(owner_contacts.config, "get", lambda k: "same" if k == "owners.mail_transport" else real(k))
    ok, why = owner_contacts.can_address()
    assert not ok and "Mail transport for owner approval" in why


@pytest.mark.real_cc
def test_the_cc_address_is_the_profile_email_not_the_login_and_never_a_duplicate(monkeypatch):
    seen = []

    def fake(method, path, **kw):
        seen.append(kw["params"]["sysparm_query"])
        return [{"email": "Real.Person@Gmail.com"}]
    monkeypatch.setattr(owner_contacts, "_request", fake)
    assert owner_contacts.cc_for("vedavidyadaran@lab.local", ["ann@lab.local"]) == ["real.person@gmail.com"]
    assert "email=vedavidyadaran@lab.local" in seen[0] and "user_name=vedavidyadaran" in seen[0]
    assert owner_contacts.cc_for("vedavidyadaran@lab.local", ["real.person@gmail.com"]) == []             # already a recipient
    monkeypatch.setattr(owner_contacts, "_request", lambda *a, **k: [])
    assert owner_contacts.cc_for("nobody@lab.local", []) == [] and owner_contacts.cc_for("", []) == []

    def boom(*a, **k):
        raise RuntimeError("SNOW down")
    monkeypatch.setattr(owner_contacts, "_request", boom)
    assert owner_contacts.cc_for("x@lab.local", []) == []                                                  # never raises


def test_a_user_is_found_by_username_when_the_profile_email_no_longer_matches(monkeypatch):
    from app.adapters.servicenow import client as snow
    asked = []

    def fake(method, path, **kw):
        q = kw["params"]["sysparm_query"]
        asked.append(q)
        if "user_name=vedavidyadaran" in q:
            return [{"sys_id": "u-veda"}]
        return []
    monkeypatch.setattr(snow, "_request", fake)
    assert snow._user_for("vedavidyadaran@lab.local") == "u-veda"
    assert asked[0].endswith("email=vedavidyadaran@lab.local") and "user_name=vedavidyadaran" in asked[1]
    assert snow._user_for("ghost@lab.local") is None and snow._user_for("") is None
