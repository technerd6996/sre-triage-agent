from collections import deque

import pytest

from app import pam_service
from app.ports.errors import NotFound, Unavailable


class Fake:
    provider = "fake"
    capabilities = frozenset({"fetch"})

    def __init__(self, data=None, exc=None):
        self.data, self.exc = data, exc

    def fetch(self, *keys):
        if self.exc:
            raise self.exc
        return keys[0], self.data


@pytest.fixture
def svc(monkeypatch):
    rows = []
    monkeypatch.setattr(pam_service, "_audit", lambda run, ticket, action, detail: rows.append((action, detail)))
    monkeypatch.setattr(pam_service, "_run_ticket", lambda r: "INC1")
    monkeypatch.setattr(pam_service.usage, "agent_context", lambda: ("r1", "triage"))
    monkeypatch.setattr(pam_service, "_recent", deque())
    pam_service._per_run.clear()

    def use(fake):
        monkeypatch.setattr(pam_service.registry, "get_credentials", lambda: fake)
    return rows, use


def test_checkout_returns_the_secret_and_audits_without_it(svc):
    rows, use = svc
    use(Fake({"username": "u", "password": "s3cret"}))
    assert pam_service.checkout("i-1", "10.0.0.5")["password"] == "s3cret"
    assert rows == [("pam_checkout", "host=i-1 key=i-1 result=ok provider=fake")]
    assert "s3cret" not in str(rows)


def test_no_run_is_refused(svc, monkeypatch):
    rows, use = svc
    use(Fake({}))
    monkeypatch.setattr(pam_service.usage, "agent_context", lambda: (None, "triage"))
    with pytest.raises(RuntimeError, match="no active agent run"):
        pam_service.checkout("i-1")
    assert rows[0][0] == "pam_denied"


def test_not_found_message_names_the_last_key(svc):
    rows, use = svc
    use(Fake(exc=NotFound("x")))
    with pytest.raises(RuntimeError, match="No credential available for 10.0.0.5"):
        pam_service.checkout("i-1", "10.0.0.5")
    assert "result=not_found" in rows[0][1]


def test_unavailable_store(svc):
    rows, use = svc
    use(Fake(exc=Unavailable("ClientError: AccessDenied")))
    with pytest.raises(RuntimeError, match="PAM is unavailable"):
        pam_service.checkout("i-1")
    assert "result=unavailable" in rows[0][1]


def test_per_run_limit(svc, monkeypatch):
    rows, use = svc
    use(Fake({"username": "u"}))
    monkeypatch.setenv("PAM_MAX_PER_RUN", "2")
    pam_service.checkout("i-1")
    pam_service.checkout("i-1")
    with pytest.raises(RuntimeError, match="per-run limit"):
        pam_service.checkout("i-1")
    assert rows[-1][0] == "pam_rate_limited"


def test_hourly_limit(svc, monkeypatch):
    rows, use = svc
    use(Fake({"username": "u"}))
    monkeypatch.setenv("PAM_MAX_PER_HOUR", "1")
    pam_service.checkout("i-1")
    with pytest.raises(RuntimeError, match="hourly limit"):
        pam_service.checkout("i-1")