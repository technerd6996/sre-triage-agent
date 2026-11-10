"""Plan enforcement: signed keys, feature switches, counting, the grace period and what stops after it."""
import base64
import importlib.util
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import chat_actions, licence

pytestmark = pytest.mark.real_licence

spec = importlib.util.spec_from_file_location("licence_tool", Path(__file__).resolve().parent.parent / "scripts" / "licence_tool.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

DAY = 86400


class Vendor:
    pass


@pytest.fixture
def vendor(monkeypatch):
    """A throw-away signing key standing in for the vendor's; the app is pointed at its public half."""
    private = Ed25519PrivateKey.generate()
    monkeypatch.setattr(licence, "PUBLIC_KEY", tool.public_of(private))
    monkeypatch.setattr(licence, "_cache", {"key": None, "lic": None})
    monkeypatch.setattr(licence, "_status_cache", {"at": 0.0, "value": None})
    saved, audits = {}, []
    monkeypatch.setattr(licence.store, "get_setting", lambda k, d=None: saved.get(k, d))
    monkeypatch.setattr(licence.store, "set_setting", lambda k, v, by="": saved.__setitem__(k, v))
    monkeypatch.setattr(licence.store, "audit", lambda *a, **k: audits.append(a))
    counts = {"servers": 3, "seats": 1}
    monkeypatch.setattr(licence, "_count_servers", lambda: counts["servers"])
    monkeypatch.setattr(licence, "_count_seats", lambda: counts["seats"])
    state = {"key": ""}
    real = licence.config.get
    monkeypatch.setattr(licence.config, "get", lambda k: state["key"] if k == "licence.key" else real(k))

    def use(plan="Team", **kw):
        state["key"] = tool.issue(private, "Acme Ltd", plan, **kw)
        return state["key"]
    v = Vendor()
    v.private, v.use, v.saved, v.audits, v.counts = private, use, saved, audits, counts
    return v


def test_a_signed_key_is_read_and_carries_the_plan(vendor):
    lic = licence.parse(vendor.use("Team"))
    assert lic["plan"] == "Team" and lic["customer"] == "Acme Ltd" and lic["servers"] == 100 and lic["seats"] == 10
    assert lic["tickets_month"] == 1500 and lic["features"] == ["chat_changes"] and lic["valid"]


def test_an_edited_key_is_refused(vendor):
    key = vendor.use("Starter")
    body, sig = key[len("TRIAGE1-"):].split(".")
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).replace(b"Starter", b"Command")
    forged = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(ValueError, match="signature does not match"):
        licence.parse("TRIAGE1-" + forged + "." + sig)


def test_a_key_signed_by_someone_else_is_refused(vendor):
    other = tool.issue(Ed25519PrivateKey.generate(), "Mallory", "Commander")
    with pytest.raises(ValueError, match="signature does not match"):
        licence.parse(other)


@pytest.mark.parametrize("junk", ["hello", "TRIAGE1-abc", "TRIAGE1-a.b.c", "TRIAGE1-!!!.???"])
def test_junk_is_refused_with_a_message(vendor, junk):
    with pytest.raises(ValueError):
        licence.parse(junk)


def test_saving_a_bad_key_in_the_admin_panel_is_refused_but_empty_is_allowed(vendor):
    licence._check_key("")
    with pytest.raises(ValueError):
        licence._check_key("TRIAGE1-abc.def")


def test_no_key_means_starter_limits_and_no_paid_features(vendor):
    lic = licence.current()
    assert lic["plan"] == "Unlicensed" and lic["servers"] == 25 and lic["features"] == [] and not licence.has("chat_changes")
    with pytest.raises(licence.NotInPlan, match="does not include changes from the approver chat"):
        licence.require("chat_changes")


def test_features_follow_the_plan(vendor):
    vendor.use("Team")
    licence.require("chat_changes")
    with pytest.raises(licence.NotInPlan):
        licence.require("owner_approval")
    vendor.use("Commander")
    licence.require("owner_approval")
    licence.require("telemetry_export")


def test_a_custom_key_can_override_the_plan_numbers_and_features(vendor):
    lic = licence.parse(vendor.use("Starter", servers=40, seats=3, tickets=0, features=["owner_approval"]))
    assert (lic["servers"], lic["seats"], lic["tickets_month"], lic["features"]) == (40, 3, 0, ["owner_approval"])


def test_within_limits_nothing_is_flagged(vendor):
    vendor.use("Team")
    s = licence.status(force=True)
    assert s["problems"] == [] and s["warnings"] == [] and not s["auto_blocked"] and licence.auto_allowed()
    assert s["usage"] == {"servers": 3, "seats": 1, "tickets_month": 0}


def test_a_warning_at_eighty_percent(vendor):
    vendor.use("Team")
    vendor.counts["servers"] = 85
    s = licence.status(force=True)
    assert s["warnings"] == ["85 of 100 servers"] and s["problems"] == []


def test_over_a_limit_starts_the_grace_period_and_work_continues(vendor):
    vendor.use("Starter")
    vendor.counts["seats"] = 5
    s = licence.status(force=True)
    assert s["problems"] == ["5 approver seats, the plan allows 2"] and not s["auto_blocked"] and licence.auto_allowed()
    assert vendor.saved["licence_over_since"] and vendor.audits[0][3] == "licence_grace_started"
    assert abs(s["grace_ends"] - (time.time() + 14 * DAY)) < 60


def test_after_the_grace_period_only_new_automatic_tickets_stop(vendor):
    vendor.use("Starter")
    vendor.counts["seats"] = 5
    vendor.saved["licence_over_since"] = time.time() - 15 * DAY
    s = licence.status(force=True)
    assert s["auto_blocked"] and not licence.auto_allowed()
    assert "NEW AUTOMATIC TICKETS ARE STOPPED" in licence.describe(s)


def test_coming_back_within_the_limits_clears_the_grace_period(vendor):
    vendor.use("Starter")
    vendor.saved["licence_over_since"] = time.time() - 15 * DAY
    s = licence.status(force=True)
    assert s["problems"] == [] and vendor.saved["licence_over_since"] is None and licence.auto_allowed()


def test_an_expired_key_gets_the_same_grace_then_stops_automatic_tickets(vendor):
    lic = licence.parse(tool.issue(vendor.private, "Acme", "Team", days=30, now=time.time() - 40 * DAY))
    used = {"servers": 1, "seats": 1, "tickets_month": 0}
    ev = licence.evaluate(lic, used, None, time.time())
    assert ev["problems"][0].startswith("the licence expired on") and not ev["auto_blocked"]
    assert licence.evaluate(lic, used, time.time() - 20 * DAY, time.time())["auto_blocked"]


def test_unlimited_is_never_over_and_an_unreadable_count_is_not_a_breach(vendor):
    lic = licence.parse(vendor.use("Commander"))
    ev = licence.evaluate(lic, {"servers": 100000, "seats": None, "tickets_month": 5}, None, time.time())
    assert ev["problems"] == []


def test_automatic_tickets_are_counted_per_month(vendor):
    vendor.use("Starter")
    for _ in range(3):
        licence.count_auto()
    assert licence.auto_tickets_this_month() == 3
    vendor.saved["licence_usage"] = {"month": "2001-01", "auto_tickets": 999}
    assert licence.auto_tickets_this_month() == 0                    # a new month starts from zero


def test_reaching_the_monthly_ticket_limit_is_a_problem(vendor):
    vendor.use("Starter")
    vendor.saved["licence_usage"] = {"month": licence._month(), "auto_tickets": 200}
    assert licence.status(force=True)["problems"] == ["200 automatic tickets this month, the plan allows 200"]


def test_a_failure_while_checking_never_stops_the_work(vendor, monkeypatch):
    def boom(force=False):
        raise RuntimeError("database is down")
    monkeypatch.setattr(licence, "status", boom)
    assert licence.auto_allowed() is True


def test_the_chat_refuses_a_change_the_plan_does_not_include(vendor):
    server = {"name": "term-win-srv", "platform": "windows"}
    with pytest.raises(chat_actions.AskBack, match="does not include changes from the approver chat"):
        chat_actions.propose("restart the spooler service on term-win-srv", "eng@lab.local", server)
    vendor.use("Team")
    with pytest.raises(chat_actions.AskBack, match="does not include owner approval by email"):
        chat_actions.propose("reboot term-win-srv", "eng@lab.local", server)


def test_the_private_key_is_not_in_the_project():
    root = Path(__file__).resolve().parent.parent
    hits = [str(p) for p in root.rglob("*.pem") if ".venv" not in p.parts and "Creds" not in p.parts]
    assert hits == [] and "PRIVATE KEY" not in (root / "app" / "licence.py").read_text(encoding="utf-8")
