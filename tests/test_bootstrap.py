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