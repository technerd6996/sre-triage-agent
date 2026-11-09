"""Owner approval by email: reading a pasted reply, the guards (self host, reboot spacing), the reboot check, and the whole
request -> reply -> confirm flow with mocks (no mail is sent, no server is touched)."""
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import chat_actions, config, owner_approval as OA, owner_contacts
from app.api import approver_chat as ac
from app.tools import chat_changes as CC

OWNERS = ["ann@lab.local", "ops@lab.local"]
QUOTE = ("From: TRIAGE-1 <triage@lab.local>\nSent: Monday, November 9, 2026 9:00 PM\nTo: Ann Owner\n"
         "Subject: [TRIAGE-1 REQ-ABC234] Approval requested: restart server victim-test\n\n"
         "To approve, reply to this email with a clear approval, for example: approved, go ahead.\n"
         "To refuse or hold it, reply with: not approved, hold off.\n")


def outlook(body, sender="Ann Owner <ann@lab.local>"):
    return (f"From: {sender}\nSent: Monday, November 9, 2026 10:00 PM\nTo: TRIAGE-1\n"
            f"Subject: RE: [TRIAGE-1 REQ-ABC234] Approval requested: restart server victim-test\n\n{body}\n\n{QUOTE}")


# ---------------------------------------------------------------- reading a reply
def test_the_reply_is_read_without_the_quoted_original():
    p = OA.split_email(outlook("Approved, go ahead tonight."))
    assert p["from"] == "ann@lab.local" and p["top"] == "Approved, go ahead tonight."


def test_gmail_style_and_plain_replies():
    gmail = ("From: Ops <ops@lab.local>\nSubject: Re: [TRIAGE-1 REQ-ABC234] x\n\nYes, go ahead\n\n"
             "On Mon, 9 Nov 2026 at 22:00, TRIAGE-1 <triage@lab.local> wrote:\n> To approve, reply with approved\n")
    p = OA.split_email(gmail)
    assert p["from"] == "ops@lab.local" and p["top"] == "Yes, go ahead"
    assert OA.split_email("approved REQ-ABC234")["from"] == ""


@pytest.mark.parametrize("body", ["Approved, go ahead tonight.", "Go ahead.", "No objection, proceed.", "OK to proceed.", "Yes - agreed."])
def test_clear_approvals_pass(body):
    r = OA.evaluate(outlook(body), OWNERS)
    assert r["verdict"] == "approved", (body, r)


@pytest.mark.parametrize("body,word", [("Not approved, hold off.", "not approved"), ("I do not approve this.", "do not"),
                                       ("Don't reboot it today.", "don't"), ("Wait for my call, then go ahead.", "wait"),
                                       ("Denied.", "denied"), ("No.", "no")])
def test_refusals_and_holds_never_pass(body, word):
    r = OA.evaluate(outlook(body), OWNERS)
    assert r["verdict"] == "refused" and word in r["why"]


@pytest.mark.parametrize("body", ["Approved if the backup finishes first.", "Go ahead after 22:00.", "Approved, but tell the team.",
                                  "Proceed unless the batch job is running."])
def test_conditional_approvals_go_back_to_the_engineer(body):
    r = OA.evaluate(outlook(body), OWNERS)
    assert r["verdict"] == "unclear" and "condition" in r["why"]


def test_no_clear_word_is_not_an_approval():
    for body in ("ok", "Thanks for letting me know.", "Seen."):
        assert OA.evaluate(outlook(body), OWNERS)["verdict"] == "unclear"


def test_our_own_approval_wording_quoted_below_never_counts():
    # the owner says nothing useful; the quoted original contains 'approved' and 'go ahead'
    assert OA.evaluate(outlook("Thanks."), OWNERS)["verdict"] == "unclear"
    # and a refusal on top wins over the quoted approval words
    assert OA.evaluate(outlook("Hold off please."), OWNERS)["verdict"] == "refused"


def test_the_sender_must_be_an_owner(monkeypatch):
    assert OA.evaluate(outlook("Approved.", sender="Mallory <mallory@evil.test>"), OWNERS)["verdict"] == "wrong_sender"
    assert OA.evaluate("Approved. REQ-ABC234", OWNERS)["verdict"] == "no_sender"
    real = config.get
    monkeypatch.setattr(OA.config, "get", lambda k: False if k == "owners.require_sender_match" else real(k))
    assert OA.evaluate("Approved. REQ-ABC234", OWNERS)["verdict"] == "approved"


def test_a_signature_disclaimer_below_the_answer_is_not_read_as_a_condition():
    body = "Approved.\n\nRegards,\nAnn\n\nIf you are not the intended recipient, please delete this message and do not share it."
    assert OA.evaluate(outlook(body), OWNERS)["verdict"] == "approved"


def test_code_in_finds_the_request_code():
    assert OA.code_in(outlook("ok")) == "REQ-ABC234" and OA.code_in("nothing here") == ""


# ---------------------------------------------------------------- guards
def test_the_server_triage_runs_on_is_never_powered_off(monkeypatch):
    monkeypatch.setattr(OA, "_local_names", lambda: {"ec2amaz-abc123"})
    monkeypatch.setattr(OA, "_local_addresses", lambda: {"172.31.32.212"})
    # the CMDB name differs from the OS host name: the address gives it away
    assert "runs on" in OA.power_block({"name": "term-win-srv", "instance_id": "term-win-srv", "private_ip": "172.31.32.212", "public_ip": "1.2.3.4"})
    assert "runs on" in OA.power_block({"name": "EC2AMAZ-ABC123", "instance_id": "x", "private_ip": "", "public_ip": ""})
    assert OA.power_block({"name": "victim-test", "instance_id": "victim-test", "private_ip": "172.31.3.203", "public_ip": ""}) == ""


def test_listed_servers_are_never_powered_off(monkeypatch):
    monkeypatch.setattr(OA, "_local_names", lambda: set())
    monkeypatch.setattr(OA, "_local_addresses", lambda: set())
    real = config.get
    monkeypatch.setattr(OA.config, "get", lambda k: "db01, billing" if k == "chat.never_power_hosts" else real(k))
    assert "never reboots" in OA.power_block({"name": "BILLING", "instance_id": "billing", "private_ip": "", "public_ip": ""})


class FakeAudit:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *a):
        return self

    def eq(self, *a):
        return self

    def gt(self, *a):
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


def test_a_server_cannot_be_powered_again_within_the_minimum_gap(monkeypatch):
    from app import store
    now = time.time()
    monkeypatch.setattr(store, "supabase", SimpleNamespace(table=lambda n: FakeAudit([{"ts": now - 600, "detail": "host=victim-test tool=reboot_server"}])))
    assert OA.recent_power_action("victim-test") == now - 600
    assert OA.recent_power_action("other") == 0.0
    monkeypatch.setattr(store, "supabase", SimpleNamespace(table=lambda n: FakeAudit([])))
    assert OA.recent_power_action("victim-test") == 0.0


# ---------------------------------------------------------------- did it come back
@pytest.fixture
def verify_env(monkeypatch):
    notes, audits = [], []
    from app import store
    monkeypatch.setattr(store, "audit", lambda *a, **k: audits.append(a))
    import app.ports.registry as reg
    monkeypatch.setattr(reg, "get_ticketing", lambda role="agent": SimpleNamespace(add_note=lambda t, n: notes.append(n)))
    return notes, audits


def test_the_checker_reports_a_server_that_came_back(monkeypatch, verify_env):
    notes, audits = verify_env
    from app.tools import chat_tools
    monkeypatch.setattr(OA, "_port_open", lambda h, p: True)
    monkeypatch.setattr(chat_tools, "check_uptime", SimpleNamespace(invoke=lambda a: {"seconds": 30, "summary": "up 0 min, last started now"}))
    OA._verify_worker("victim-test", "victim-test", "linux", "INC1", "eng@lab.local", time.time() - 100, 0, sleep=lambda s: None)
    assert "is back" in notes[0] and audits[0][3] == "chat_reboot_verified"


def test_the_checker_says_so_when_the_server_does_not_return(monkeypatch, verify_env):
    notes, audits = verify_env
    OA._verify_worker("victim-test", "victim-test", "linux", "INC1", "eng@lab.local", time.time() - 100000, 0, sleep=lambda s: None)
    assert "did not answer again" in notes[0] and audits[0][3] == "chat_reboot_unverified"


# ---------------------------------------------------------------- request -> reply -> confirm
LIN = {"name": "victim-test", "platform": "linux", "instance_id": "victim-test", "private_ip": "172.31.3.203", "public_ip": ""}


class Tickets:
    def __init__(self):
        self.notes, self.created = [], []

    def create_for_user(self, title, desc, user, urgency, impact, host, note=""):
        self.created.append(title)
        return SimpleNamespace(id=f"INC91000{len(self.created)}")

    def add_note(self, n, note):
        self.notes.append((n, note))


@pytest.fixture
def flow(monkeypatch):
    app = FastAPI()
    app.include_router(ac.router)
    tk, audits, mails, rows, started = Tickets(), [], [], {}, []
    monkeypatch.setattr(ac, "_actor", lambda r: "eng@lab.local")
    monkeypatch.setattr(ac, "resolve_server", lambda q, ci_name="": (LIN, ""))
    monkeypatch.setattr(ac.registry, "get_ticketing", lambda role="agent": tk)
    monkeypatch.setattr(ac.store, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(owner_contacts, "can_address", lambda: (True, ""))
    monkeypatch.setattr(owner_contacts, "contacts", lambda h: {"emails": OWNERS, "who": [f"{e} - owner" for e in OWNERS], "error": ""})
    monkeypatch.setattr(owner_contacts, "send", lambda to, cc, s, b: mails.append((to, cc, s, b)) or (True, "id"))
    monkeypatch.setattr(OA, "_insert", lambda row: rows.__setitem__(row["id"], dict(row)))
    monkeypatch.setattr(OA, "_get", lambda rid: rows.get(rid))
    monkeypatch.setattr(OA, "_update", lambda rid, **f: rows[rid].update(f))
    monkeypatch.setattr(OA, "_local_names", lambda: set())
    monkeypatch.setattr(OA, "_local_addresses", lambda: {"10.9.9.9"})
    monkeypatch.setattr(OA, "recent_power_action", lambda h: 0.0)
    monkeypatch.setattr(OA, "start_verifier", lambda *a: started.append(a))
    monkeypatch.setattr(chat_actions, "_sessions_line", lambda inst: ["Nobody is logged on right now."])
    ran = []
    result = {"target": "linux", "summary": "restart scheduled in 60 seconds", "delay_seconds": 60, "lines": ["to cancel in time, run: shutdown -c"]}
    monkeypatch.setattr(chat_actions, "_tool", lambda name: SimpleNamespace(invoke=lambda a: ran.append((name, a)) or result))
    c = TestClient(app)

    def post(path, **body):
        return c.post("/api/approver-chat" + path, json=body)
    return SimpleNamespace(post=post, tk=tk, audits=audits, mails=mails, rows=rows, started=started, ran=ran, monkeypatch=monkeypatch)


def _card(f, msg="reboot victim-test because kernel patching"):
    r = f.post("/act/propose", instruction=msg)
    assert r.status_code == 200, r.text
    return r.json()


def _approve(f, row_id, body="Approved, go ahead tonight."):
    text = outlook(body).replace("REQ-ABC234", row_id)
    return f.post("/act/owner-reply", text=text)


def test_a_reboot_asks_for_owner_approval_and_sends_nothing_yet(flow):
    card = _card(flow)
    assert card["kind"] == "owner_request" and card["level"] == 2 and card["owners"][0].startswith("ann@lab.local")
    assert "Restart the whole server victim-test in 60 seconds" in card["title"] and "Nobody is logged on" in card["detail"][-1]
    assert flow.mails == [] and flow.tk.created == [] and flow.ran == []


def test_restart_the_server_means_the_machine_not_a_service_called_server(flow):
    assert _card(flow, "restart the server victim-test")["tool"] == "reboot_server"


def test_the_whole_path_request_reply_confirm(flow):
    card = _card(flow)
    r = flow.post("/act/owner-request", token=card["token"]).json()
    rid = r["request_id"]
    assert rid.startswith("REQ-") and r["ticket"] == "INC910001" and r["owners"] == OWNERS
    to, cc, subject, body = flow.mails[0]
    assert to == OWNERS and cc == ["eng@lab.local"] and rid in subject and "victim-test" in body and "INC910001" in body
    assert flow.rows[rid]["status"] == "waiting" and flow.ran == []                 # still nothing has run

    reply = _approve(flow, rid).json()
    assert reply["verdict"] == "approved" and flow.rows[rid]["status"] == "approved" and flow.rows[rid]["approved_by"] == "ann@lab.local"
    assert "Owner approval recorded from ann@lab.local" in reply["card"]["detail"][0] and flow.ran == []

    done = flow.post("/act/confirm", token=reply["card"]["token"]).json()
    assert done["ok"] and done["ticket"] == "INC910001" and flow.tk.created == ["[Chat action] awaiting owner approval: restart server victim-test"]
    assert flow.ran[0][0] == "reboot_server" and flow.rows[rid]["status"] == "used"
    assert flow.started and flow.started[0][0] == "victim-test" and flow.started[0][-1] == 60          # the checker was started
    assert any(a[3] == "chat_power_scheduled" for a in flow.audits)
    assert len(flow.mails) == 2 and "done" in flow.mails[1][2]                                        # owners told afterwards
    assert any("Reply pasted by eng@lab.local" in n for _, n in flow.tk.notes)


def test_a_reply_from_someone_else_or_a_refusal_runs_nothing(flow):
    rid = flow.post("/act/owner-request", token=_card(flow)["token"]).json()["request_id"]
    bad = flow.post("/act/owner-reply", text=outlook("Approved.", sender="Mallory <m@evil.test>").replace("REQ-ABC234", rid)).json()
    assert bad["verdict"] == "wrong_sender" and flow.rows[rid]["status"] == "waiting"
    no = _approve(flow, rid, "Not approved, hold off.").json()
    assert no["verdict"] == "refused" and flow.rows[rid]["status"] == "refused" and flow.ran == []
    assert _approve(flow, rid).status_code == 409                                                       # a refused request stays refused


def test_a_card_token_cannot_skip_a_step(flow):
    card = _card(flow)
    assert flow.post("/act/confirm", token=card["token"]).status_code == 409                              # request card is not a run card
    rid = flow.post("/act/owner-request", token=card["token"]).json()["request_id"]
    assert flow.post("/act/owner-request", token=card["token"]).status_code == 409                        # single use
    card2 = flow.post("/act/owner-reply", text=outlook("Approved.").replace("REQ-ABC234", rid)).json()["card"]
    flow.rows[rid]["status"] = "refused"                                                                 # state changes before the click
    assert flow.post("/act/confirm", token=card2["token"]).status_code == 409 and flow.ran == []


def test_an_approval_can_only_be_used_once(flow):
    rid = flow.post("/act/owner-request", token=_card(flow)["token"]).json()["request_id"]
    token = _approve(flow, rid).json()["card"]["token"]
    assert flow.post("/act/confirm", token=token).json()["ok"]
    assert _approve(flow, rid).status_code == 409 and len(flow.ran) == 1                                  # status is 'used'


def test_a_lost_card_can_be_reissued_while_the_approval_is_valid(flow):
    rid = flow.post("/act/owner-request", token=_card(flow)["token"]).json()["request_id"]
    _approve(flow, rid)
    again = _approve(flow, rid).json()
    assert again["verdict"] == "approved" and again["card"]["token"]


def test_someone_elses_request_and_expired_requests_are_refused(flow):
    rid = flow.post("/act/owner-request", token=_card(flow)["token"]).json()["request_id"]
    flow.rows[rid]["requester"] = "other@lab.local"
    assert _approve(flow, rid).status_code == 422
    flow.rows[rid].update(requester="eng@lab.local", expires=time.time() - 1)
    assert _approve(flow, rid).status_code == 409 and flow.rows[rid]["status"] == "expired"
    assert flow.post("/act/owner-reply", text="no code in this text at all, just words to make it long enough").status_code == 422


def test_the_server_triage_runs_on_cannot_even_get_a_card(flow):
    flow.monkeypatch.setattr(OA, "_local_addresses", lambda: {"172.31.3.203"})
    r = flow.post("/act/propose", instruction="reboot victim-test")
    assert r.status_code == 422 and "runs on" in r.json()["detail"]


def test_the_guard_is_checked_again_when_the_mail_is_requested_and_at_confirm(flow):
    token = _card(flow)["token"]
    flow.monkeypatch.setattr(OA, "_local_addresses", lambda: {"172.31.3.203"})                  # became true after the card
    assert flow.post("/act/owner-request", token=token).status_code == 409 and flow.mails == []


def test_reboot_spacing_and_missing_owner_or_mail_setup_stop_the_card(flow):
    flow.monkeypatch.setattr(OA, "recent_power_action", lambda h: time.time() - 300)
    assert "already restarted" in flow.post("/act/propose", instruction="reboot victim-test").json()["detail"]
    flow.monkeypatch.setattr(OA, "recent_power_action", lambda h: 0.0)
    flow.monkeypatch.setattr(owner_contacts, "contacts", lambda h: {"emails": [], "who": [], "error": "victim-test has no owner email"})
    assert "cannot find who to ask" in flow.post("/act/propose", instruction="reboot victim-test").json()["detail"]
    flow.monkeypatch.setattr(owner_contacts, "can_address", lambda: (False, "the mail transport is SNS"))
    assert "SNS" in flow.post("/act/propose", instruction="reboot victim-test").json()["detail"]


def test_a_mail_that_cannot_be_sent_cancels_the_request_and_says_nothing_changed(flow):
    token = _card(flow)["token"]
    flow.monkeypatch.setattr(owner_contacts, "send", lambda *a: (False, "SMTP refused"))
    r = flow.post("/act/owner-request", token=token)
    assert r.status_code == 502 and "Nothing was changed" in r.json()["detail"]
    assert list(flow.rows.values())[0]["status"] == "cancelled" and any("could NOT be sent" in n for _, n in flow.tk.notes)


# ---------------------------------------------------------------- shutdown and package removal use the same machinery
def test_shutdown_and_removal_cards_need_the_owners_too(flow, monkeypatch):
    assert _card(flow, "shut down victim-test")["tool"] == "shutdown_server"
    monkeypatch.setattr(CC, "simulate_removal", lambda t, n: {"manager": "dnf", "packages": n, "removes": ["telnet x86_64 1.0"], "extra": [], "error": ""})
    card = _card(flow, "remove telnet from victim-test")
    assert card["kind"] == "owner_request" and card["tool"] == "remove_packages" and "would remove: telnet" in card["detail"][0]


def test_a_removal_that_drags_other_packages_with_it_is_refused(flow, monkeypatch):
    monkeypatch.setattr(CC, "simulate_removal", lambda t, n: {"manager": "dnf", "packages": n, "removes": ["telnet x", "libfoo x"], "extra": ["libfoo x"], "error": ""})
    r = flow.post("/act/propose", instruction="remove telnet from victim-test")
    assert r.status_code == 422 and "also remove: libfoo" in r.json()["detail"]
    r = flow.post("/act/propose", instruction="remove sudo from victim-test")
    assert r.status_code == 422 and "protected" in r.json()["detail"]
