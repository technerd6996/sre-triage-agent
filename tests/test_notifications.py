import pytest

from app.adapters.notify import senders
from app.ports import registry
from app.ports.errors import Unavailable, Unsupported
from app.ports.notifications import CAP_BROADCAST, CAP_RECIPIENTS, NotificationsPort


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for n in ("NOTICE_TRANSPORT", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM",
              "SES_FROM", "SNS_TOPIC_ARN", "AWS_REGION"):
        monkeypatch.delenv(n, raising=False)


def test_blank_transport_is_smtp():
    assert registry.get_notifications().provider == "smtp"


def test_each_transport_is_registered_and_satisfies_the_port(monkeypatch):
    for name in ("smtp", "ses", "sns"):
        monkeypatch.setenv("NOTICE_TRANSPORT", name.upper())          # case does not matter
        n = registry.get_notifications()
        assert n.provider == name and isinstance(n, NotificationsPort)


def test_unknown_transport_is_unsupported(monkeypatch):
    monkeypatch.setenv("NOTICE_TRANSPORT", "pigeon")
    with pytest.raises(Unsupported):
        registry.get_notifications()


def test_capabilities_tell_broadcast_from_recipients():
    assert CAP_BROADCAST in senders.SnsNotifier.capabilities
    assert CAP_RECIPIENTS in senders.SmtpNotifier.capabilities and CAP_RECIPIENTS in senders.SesNotifier.capabilities


def test_unconfigured_senders_say_what_is_missing():
    with pytest.raises(Unsupported, match="SMTP host"):
        senders.SmtpNotifier().send(["a@x.io"], [], "s", "b")
    with pytest.raises(Unsupported, match="SES sender"):
        senders.SesNotifier().send(["a@x.io"], [], "s", "b")
    with pytest.raises(Unsupported, match="SNS topic ARN"):
        senders.SnsNotifier().send([], [], "s", "b")


def test_smtp_sends_with_login_and_all_recipients(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "mail.x.io")
    monkeypatch.setenv("SMTP_USER", "bot")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("SMTP_FROM", "triage@x.io")
    seen = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            seen["addr"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): seen["tls"] = True
        def login(self, u, p): seen["login"] = (u, p)
        def send_message(self, msg, to_addrs): seen["to"], seen["from"] = to_addrs, msg["From"]

    monkeypatch.setattr(senders.smtplib, "SMTP", FakeSMTP)
    mid = senders.SmtpNotifier().send(["a@x.io"], ["b@x.io"], "subj", "body")
    assert mid.startswith("<") and seen["addr"] == ("mail.x.io", 587) and seen["tls"]
    assert seen["login"] == ("bot", "pw") and seen["to"] == ["a@x.io", "b@x.io"] and seen["from"] == "triage@x.io"


def test_smtp_failure_is_unavailable_and_never_leaks_the_password(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "mail.x.io")
    monkeypatch.setenv("SMTP_PASSWORD", "hunter2")

    class Down:
        def __init__(self, *a, **k): raise ConnectionRefusedError("refused")

    monkeypatch.setattr(senders.smtplib, "SMTP", Down)
    with pytest.raises(Unavailable) as e:
        senders.SmtpNotifier().send(["a@x.io"], [], "s", "b")
    assert "hunter2" not in str(e.value)


def test_smtp_needs_a_recipient(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "mail.x.io")
    with pytest.raises(Unsupported, match="recipients"):
        senders.SmtpNotifier().send([], [], "s", "b")


def _fake_boto(monkeypatch, record, fail=False):
    class Client:
        def send_email(self, **kw):
            if fail: raise RuntimeError("AccessDenied")
            record["ses"] = kw; return {"MessageId": "ses-1"}
        def publish(self, **kw):
            if fail: raise RuntimeError("AccessDenied")
            record["sns"] = kw; return {"MessageId": "sns-1"}

    class Boto:
        @staticmethod
        def client(name, region_name=None):
            record["region"] = region_name; return Client()

    import sys
    monkeypatch.setitem(sys.modules, "boto3", Boto)


def test_ses_and_sns_send(monkeypatch):
    rec = {}
    _fake_boto(monkeypatch, rec)
    monkeypatch.setenv("SES_FROM", "triage@x.io")
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:eu:1:t")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    assert senders.SesNotifier().send(["a@x.io"], ["b@x.io"], "s" * 300, "body") == "ses-1"
    assert rec["ses"]["Destination"] == {"ToAddresses": ["a@x.io"], "CcAddresses": ["b@x.io"]}
    assert len(rec["ses"]["Message"]["Subject"]["Data"]) == 200 and rec["region"] == "ap-south-1"
    assert senders.SnsNotifier().send([], [], "s" * 300, "body") == "sns-1"
    assert len(rec["sns"]["Subject"]) == 100


def test_aws_failure_is_unavailable(monkeypatch):
    _fake_boto(monkeypatch, {}, fail=True)
    monkeypatch.setenv("SES_FROM", "triage@x.io")
    with pytest.raises(Unavailable):
        senders.SesNotifier().send(["a@x.io"], [], "s", "b")


def test_escalation_uses_the_port(monkeypatch):
    from app import escalation
    monkeypatch.setenv("NOTICE_TRANSPORT", "sns")
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn")
    monkeypatch.setattr(escalation.store, "get_setting", lambda k, d=None: d)
    assert escalation.conf()["to"] == ["All confirmed subscribers of the SNS topic"]
    sent = {}
    class Fake:
        provider, capabilities = "fake", frozenset()
        def send(self, to, cc, subject, body): sent["args"] = (to, cc, subject, body); return "m1"
    monkeypatch.setattr(escalation.registry, "get_notifications", lambda: Fake())
    assert escalation._send(["a@x.io"], [], "s", "b") == "m1" and sent["args"][0] == ["a@x.io"]
    assert escalation.conf()["to"] == []          # not broadcast: the recipient lists are shown
