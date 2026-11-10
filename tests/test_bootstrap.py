from app import auth, bootstrap
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_password_policy():
    c = bootstrap.check_new_password
    assert c("short1!", "a@b.co")
    assert c(bootstrap.DEFAULT_ADMIN_PASSWORD, "a@b.co")
    assert c("same-password-123", "a@b.co", current="same-password-123")
    assert c("a@b.co" * 3, "a@b.co" * 3)
    assert c("a-strong-passphrase-2026", "a@b.co", current="old-password") is None


def test_paths_allowed_while_a_password_change_is_pending():
    ok = auth.allowed_while_password_change_pending
    assert ok("/api/auth/change-password") and ok("/api/auth/me") and ok("/api/auth/logout")
    assert ok("/static/change-password.html")
    assert not ok("/api/agent/runs") and not ok("/admin") and not ok("/")

def test_setup_code(monkeypatch):
    monkeypatch.setitem(bootstrap._state, "code", None)
    code = bootstrap.setup_code()
    assert bootstrap.check_setup_code(code) and bootstrap.check_setup_code(f"  {code} ")
    assert not bootstrap.check_setup_code("wrong") and not bootstrap.check_setup_code("")


def test_installed_state_is_sticky(monkeypatch):
    monkeypatch.setitem(bootstrap._state, "installed", True)
    assert bootstrap.is_installed() is True          # no database read once installed


def _client(monkeypatch, installed: bool) -> TestClient:
    monkeypatch.setattr(bootstrap, "is_installed", lambda: installed)
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)

    @app.get("/setup")
    def setup_page():
        return "setup"

    @app.get("/api/setup/status")
    def setup_status():
        return {"ok": True}

    @app.get("/api/x")
    def api_x():
        return {"ok": True}

    @app.get("/other")
    def other():
        return "other"

    return TestClient(app, follow_redirects=False)


def test_before_setup_everything_but_setup_is_closed(monkeypatch):
    c = _client(monkeypatch, installed=False)
    assert c.get("/setup").status_code == 200 and c.get("/api/setup/status").status_code == 200
    r = c.get("/api/x")
    assert r.status_code == 503 and r.json()["code"] == "setup_required"
    r = c.get("/other")
    assert r.status_code == 302 and r.headers["location"] == "/setup"


def test_after_setup_the_setup_routes_are_closed(monkeypatch):
    c = _client(monkeypatch, installed=True)
    assert c.get("/api/setup/status").status_code == 404
    r = c.get("/setup")
    assert r.status_code == 302 and r.headers["location"] == "/static/login.html"
    assert c.get("/api/x").status_code == 401         # normal auth applies again

# ---------------------------------------------------------------- the first admin's password is chosen during setup
class _Users:
    """A tiny stand-in for the users table: remembers inserts, answers the two lookups seed_default_admin makes."""

    def __init__(self, existing_admin=False):
        self.rows, self.existing_admin, self._q = [], existing_admin, None

    def table(self, name):
        self._q = {"in": False}
        return self

    def select(self, *a): return self
    def in_(self, *a): self._q["in"] = True; return self
    def eq(self, *a): return self
    def limit(self, *a): return self
    def insert(self, row): self.rows.append(row); return self

    def execute(self):
        from types import SimpleNamespace
        return SimpleNamespace(data=[{"user_id": "x"}] if (self._q["in"] and self.existing_admin) else [])


def _seed_env(monkeypatch, existing_admin=False):
    db = _Users(existing_admin)
    monkeypatch.setattr(bootstrap.store, "supabase", db)
    monkeypatch.setattr(bootstrap.store, "audit", lambda *a, **k: None)
    return db


def test_the_first_admin_gets_the_chosen_password_and_no_forced_change(monkeypatch):
    db = _seed_env(monkeypatch)
    assert bootstrap.seed_default_admin("a-strong-passphrase-2026") is True
    row = db.rows[0]
    assert row["must_change_password"] is False and bootstrap._pwd.verify("a-strong-passphrase-2026", row["password_hash"])
    assert not bootstrap._pwd.verify(bootstrap.DEFAULT_ADMIN_PASSWORD, row["password_hash"])


import pytest  # noqa: E402


@pytest.mark.parametrize("bad", ["", "short1!", bootstrap.DEFAULT_ADMIN_PASSWORD, bootstrap.DEFAULT_ADMIN_ID])
def test_setup_refuses_a_missing_weak_or_old_default_password(monkeypatch, bad):
    db = _seed_env(monkeypatch)
    with pytest.raises(ValueError):
        bootstrap.seed_default_admin(bad)
    assert db.rows == []                                   # no account was created


def test_no_account_is_created_when_an_admin_already_exists(monkeypatch):
    db = _seed_env(monkeypatch, existing_admin=True)
    assert bootstrap.seed_default_admin("") is False and db.rows == []


def test_the_setup_reply_never_carries_a_password():
    import inspect
    assert "default_password" not in inspect.getsource(bootstrap.complete_setup)
