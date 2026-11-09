"""Windows update install from the chat: validated KB numbers, the one-time SYSTEM scheduled task, the job result file, the card,
the owner approval path and the background watcher. Nothing is installed: the server is faked."""
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import chat_actions, chat_reads, owner_approval as OA, owner_contacts
from app.api import approver_chat as ac
from app.tools import chat_changes as C, chat_tools as T, infra

WIN = {"name": "win-test", "platform": "windows", "instance_id": "win-test", "private_ip": "172.31.9.9", "public_ip": ""}


# ---------------------------------------------------------------- validation and the script
@pytest.mark.parametrize("bad", ["KB12", "KB123456; calc", "123456", "KB1234567890", "kb 5007651", "$(x)", "KB5007651'; Remove-Item C:\\ -Recurse"])
def test_only_real_kb_numbers_reach_the_script(bad):
    with pytest.raises(ValueError):
        C.validate_kbs([bad])


def test_kb_numbers_are_normalised_and_limited():
    assert C.validate_kbs(["kb5007651", "KB5007651", " KB5126645 "]) == ["KB5007651", "KB5126645"]
    with pytest.raises(ValueError, match="no update numbers"):
        C.validate_kbs([])
    with pytest.raises(ValueError, match="at most"):
        C.validate_kbs([f"KB50000{i:02d}" for i in range(11)])


def test_the_script_holds_only_a_checked_job_id_and_kb_numbers():
    s = C.wu_script("a1b2c3d4", ["KB5007651"])
    assert "$id = 'a1b2c3d4'" in s and "$want = @('KB5007651')" in s and "__" not in s
    with pytest.raises(ValueError):
        C.wu_script("x; calc", ["KB5007651"])
    with pytest.raises(ValueError):
        C.wu_script("a1b2c3d4", ["KB1; calc"])


def test_the_registration_command_creates_a_one_time_system_task_and_cleans_up():
    cmd = C.wu_register_command("a1b2c3d4", C.wu_script("a1b2c3d4", ["KB5007651"]))
    assert "New-ScheduledTaskPrincipal -UserId 'SYSTEM'" in cmd and "Register-ScheduledTask -TaskName 'TRIAGE1-WU-a1b2c3d4'" in cmd
    assert "Start-ScheduledTask -TaskName 'TRIAGE1-WU-a1b2c3d4'" in cmd and "\n'@\n" in cmd            # here-string end on its own line
    script = C.wu_script("a1b2c3d4", ["KB5007651"])
    assert "Unregister-ScheduledTask" in script and "Remove-Item -Path $PSCommandPath" in script      # the task and the script remove themselves
    assert "Restart-Computer" not in script and "shutdown" not in script.lower()                      # never restarts by itself


# ---------------------------------------------------------------- the tools
class Box:
    def __init__(self, win=lambda s: ""):
        self.cmds, self.win = [], win

    def _w(self, s, **k):
        self.cmds.append((s, k))
        return self.win(s)


@pytest.fixture
def box(monkeypatch):
    b = Box()
    monkeypatch.setattr(infra, "_win", b._w)
    return b


def test_install_starts_the_task_and_returns_at_once(box):
    r = C.install_windows_updates.invoke({"target": "windows", "kbs": ["KB5007651"]})
    assert not r.get("error") and len(r["job"]) == 8 and "started" in r["summary"]
    assert len(box.cmds) == 1 and "Register-ScheduledTask" in box.cmds[0][0] and box.cmds[0][1]["timeout"] == 60


def test_install_is_windows_only_and_refuses_bad_numbers(box):
    assert "Windows check" in C.install_windows_updates.invoke({"target": "linux", "kbs": ["KB5007651"]})["error"]
    assert "not an update number" in C.install_windows_updates.invoke({"target": "windows", "kbs": ["KB1; calc"]})["error"]
    assert box.cmds == []


def test_job_status_reads_the_result_file(box):
    box.win = lambda s: json.dumps({"id": "a1b2c3d4", "status": "done", "started": "2026-11-09T22:00:00", "reboot_required": True,
                                    "updates": [{"title": "Security Update (KB5007651)", "code": 2}], "error": ""})
    r = T.check_patch_job.invoke({"target": "windows", "job": "a1b2c3d4"})
    assert r["status"] == "done" and r["reboot_required"] and "Security Update (KB5007651): installed" in r["lines"][0]
    assert "restart is required" in r["lines"][-1] and "wu_a1b2c3d4.json" in box.cmds[0][0]


def test_job_status_without_a_job_or_with_a_bad_id(box):
    box.win = lambda s: ""
    assert T.check_patch_job.invoke({"target": "windows"})["summary"] == "no patch job was found on this server"
    assert "8 letters or digits" in T.check_patch_job.invoke({"target": "windows", "job": "../x"})["error"]
    assert "wu_*.json" in box.cmds[0][0]
    box.win = lambda s: json.dumps({"id": "ff", "status": "failed", "started": "x", "updates": [], "error": "0x80240022"})
    r = T.check_patch_job.invoke({"target": "windows"})
    assert r["status"] == "failed" and "error: 0x80240022" in r["lines"]


def test_questions_about_a_patch_job_route_to_the_status_tool():
    assert chat_reads.pick("patch job status on win-test") is T.check_patch_job
    assert chat_reads.pick("did the update finish on win-test") is T.check_patch_job
    assert chat_reads.pick("pending windows updates on win-test") is T.check_windows_updates


# ---------------------------------------------------------------- the card
PENDING = {"target": "windows", "summary": "1 update(s) waiting", "search_error": "",
           "lines": ["pending: KB5007651 Update for Windows Security platform [Important]", "installed: KB4999999 on 2026-10-01"]}


@pytest.fixture
def cards(monkeypatch):
    monkeypatch.setattr(T, "check_windows_updates", SimpleNamespace(invoke=lambda a: dict(PENDING)))
    monkeypatch.setattr(owner_contacts, "can_address", lambda: (True, ""))
    monkeypatch.setattr(owner_contacts, "contacts", lambda h: {"emails": ["own@lab.local"], "who": ["own@lab.local - owner"], "error": ""})
    monkeypatch.setattr(OA, "_local_addresses", lambda: set())
    monkeypatch.setattr(OA, "_local_names", lambda: set())


def test_the_card_checks_the_updates_are_really_pending(cards):
    c = chat_actions.propose("install update KB5007651 on win-test", "e", WIN)
    assert c["kind"] == "owner_request" and c["tool"] == "install_windows_updates" and c["level"] == 2
    assert "Install KB5007651 on win-test" in c["title"] and "Windows security platform".lower() in c["detail"][0].lower()
    assert any("No restart is done automatically" in d for d in c["detail"])


def test_the_card_refuses_what_is_not_pending_or_cannot_be_checked(cards, monkeypatch):
    with pytest.raises(chat_actions.AskBack, match="KB5000001 is not waiting"):
        chat_actions.propose("install update KB5000001", "e", WIN)
    with pytest.raises(chat_actions.AskBack, match="update numbers|KB"):
        chat_actions.propose("install updates", "e", WIN)
    monkeypatch.setattr(T, "check_windows_updates", SimpleNamespace(invoke=lambda a: {**PENDING, "search_error": "Access is denied"}))
    with pytest.raises(chat_actions.AskBack, match="would not let me check"):
        chat_actions.propose("install update KB5007651", "e", WIN)
    with pytest.raises(chat_actions.AskBack, match="Linux packages"):
        chat_actions.propose("install update KB5007651", "e", {**WIN, "platform": "linux"})


# ---------------------------------------------------------------- the whole path, and the watcher
class Tickets:
    def __init__(self):
        self.notes = []

    def create_for_user(self, *a, **k):
        return SimpleNamespace(id="INC920001")

    def add_note(self, n, note):
        self.notes.append((n, note))


def test_request_reply_confirm_then_the_watcher_is_started(cards, monkeypatch):
    app = FastAPI()
    app.include_router(ac.router)
    tk, mails, rows, watch, audits = Tickets(), [], {}, [], []
    monkeypatch.setattr(ac, "_actor", lambda r: "eng@lab.local")
    monkeypatch.setattr(ac, "resolve_server", lambda q, ci_name="": (WIN, ""))
    monkeypatch.setattr(ac.registry, "get_ticketing", lambda role="agent": tk)
    monkeypatch.setattr(ac.store, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(owner_contacts, "send", lambda to, cc, s, b: mails.append((to, s)) or (True, "id"))
    monkeypatch.setattr(OA, "_insert", lambda row: rows.__setitem__(row["id"], dict(row)))
    monkeypatch.setattr(OA, "_get", lambda rid: rows.get(rid))
    monkeypatch.setattr(OA, "_update", lambda rid, **f: rows[rid].update(f))
    monkeypatch.setattr(OA, "start_patch_watch", lambda *a: watch.append(a))
    started = {"target": "windows", "job": "a1b2c3d4", "summary": "install of KB5007651 started (job a1b2c3d4)", "lines": ["result file ..."]}
    monkeypatch.setattr(chat_actions, "_tool", lambda name: SimpleNamespace(invoke=lambda a: started))
    c = TestClient(app)
    post = lambda path, **b: c.post("/api/approver-chat" + path, json=b)

    card = post("/act/propose", instruction="install update KB5007651 on win-test").json()
    rid = post("/act/owner-request", token=card["token"]).json()["request_id"]
    reply = (f"From: Ann <own@lab.local>\nSubject: RE: [TRIAGE-1 {rid}] x\n\nApproved, go ahead.\n\n> quoted text\n")
    run = post("/act/owner-reply", text=reply).json()
    assert run["verdict"] == "approved"
    done = post("/act/confirm", token=run["card"]["token"]).json()
    assert done["ok"] and watch and watch[0][-1] == "a1b2c3d4" and watch[0][0] == "win-test" and watch[0][4] == "eng@lab.local"
    assert rows[rid]["status"] == "used" and "started" in done["message"]


def _watch_env(monkeypatch, status_replies):
    notes, audits, mails = [], [], []
    from app import store
    monkeypatch.setattr(store, "audit", lambda *a, **k: audits.append(a))
    import app.ports.registry as reg
    monkeypatch.setattr(reg, "get_ticketing", lambda role="agent": SimpleNamespace(add_note=lambda t, n: notes.append(n)))
    monkeypatch.setattr(owner_contacts, "contacts", lambda h: {"emails": ["own@lab.local"], "who": [], "error": ""})
    monkeypatch.setattr(owner_contacts, "send", lambda to, cc, s, b: mails.append((to, cc, s, b)) or (True, "id"))
    replies = iter(status_replies)
    monkeypatch.setattr(T, "check_patch_job", SimpleNamespace(invoke=lambda a: next(replies)))
    return notes, audits, mails


def test_the_watcher_waits_then_reports_the_result_on_the_ticket_and_to_the_owners(monkeypatch):
    notes, audits, mails = _watch_env(monkeypatch, [{"status": "running"}, {"status": "done", "summary": "patch job a1 is done", "lines": ["KB1: installed", "a restart is required to finish"]}])
    OA._patch_worker("win-test", "win-test", "windows", "INC1", "eng@lab.local", "a1b2c3d4", time.time(), sleep=lambda s: None)
    assert "patch job a1 is done" in notes[0] and "a restart is required" in notes[0] and audits[0][3] == "chat_patch_done"
    assert mails[0][0] == ["own@lab.local"] and mails[0][1] == ["eng@lab.local"] and "result" in mails[0][2]


def test_a_failed_install_is_reported_as_failed(monkeypatch):
    notes, audits, mails = _watch_env(monkeypatch, [{"status": "failed", "summary": "patch job a1 is failed", "lines": ["error: 0x80240022"]}])
    OA._patch_worker("win-test", "win-test", "windows", "INC1", "eng@lab.local", "a1b2c3d4", time.time(), sleep=lambda s: None)
    assert audits[0][3] == "chat_patch_failed" and "0x80240022" in notes[0]


def test_an_install_that_never_finishes_says_so(monkeypatch):
    notes, audits, mails = _watch_env(monkeypatch, [])
    OA._patch_worker("win-test", "win-test", "windows", "INC1", "eng@lab.local", "a1b2c3d4", time.time() - 100000, sleep=lambda s: None)
    assert "did not finish" in notes[0] and "patch job status" in notes[0] and audits[0][3] == "chat_patch_unfinished"


def test_the_agent_still_has_none_of_the_chat_tools():
    from app.agent import agent
    chat_names = {t.name for t in C.CHANGE_TOOLS} | {t.name for t in T.READ_ONLY_TOOLS}
    assert "install_windows_updates" in chat_names and "check_patch_job" in chat_names
    assert not ({t.name for t in agent.TOOLS} & chat_names)
