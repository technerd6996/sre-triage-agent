"""The packaged builds' entry point. No database or network."""
import io
import json

import psycopg
import pytest

from app import bootstrap_store, server


def test_secret_key_is_created_once(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_HOME", str(tmp_path))
    monkeypatch.delenv("TRIAGE_SECRET_KEY", raising=False)
    monkeypatch.setattr(bootstrap_store, "_lock", lambda p: None)
    assert server.ensure_secret_key() is True
    first = bootstrap_store.read()["TRIAGE_SECRET_KEY"]
    assert server.ensure_secret_key() is False
    assert bootstrap_store.read()["TRIAGE_SECRET_KEY"] == first


def test_existing_key_is_never_replaced(monkeypatch):
    monkeypatch.setenv("TRIAGE_SECRET_KEY", "already-set")
    monkeypatch.setattr(bootstrap_store, "save", lambda **k: pytest.fail("must not write"))
    assert server.ensure_secret_key() is False


def test_bind_defaults_to_this_machine_only(monkeypatch):
    monkeypatch.delenv("TRIAGE_HOST", raising=False)
    monkeypatch.delenv("TRIAGE_PORT", raising=False)
    assert server.bind_address() == ("127.0.0.1", 8000)
    monkeypatch.setenv("TRIAGE_HOST", "0.0.0.0")
    monkeypatch.setenv("TRIAGE_PORT", "9000")
    assert server.bind_address() == ("0.0.0.0", 9000)


def test_schema_file_is_where_the_build_expects_it():
    assert server.SCHEMA.is_file()


def test_gives_up_with_a_clear_message_and_no_address(monkeypatch):
    from app import db
    monkeypatch.setattr(db, "database_url", lambda: "postgres" + "ql://secret-user:secret-pass@x/db")

    def refuse(*a, **k):
        raise psycopg.OperationalError("connection refused for secret-user:secret-pass")
    monkeypatch.setattr(psycopg, "connect", refuse)
    with pytest.raises(SystemExit) as e:
        server.apply_schema(wait_seconds=2, sleep=lambda s: None)
    assert "did not become ready" in str(e.value) and "secret" not in str(e.value)


def test_installer_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_HOME", str(tmp_path))
    monkeypatch.setattr(bootstrap_store, "_lock", lambda p: None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TRIAGE_SECRET_KEY", raising=False)
    assert server.main(["bootstrap-has"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"DATABASE_URL": "postgres" + "ql://a", "TRIAGE_SECRET_KEY": "k"})))
    assert server.main(["bootstrap-merge"]) == 0
    assert server.main(["bootstrap-has"]) == 0


def test_service_account_gets_the_key_file(tmp_path, monkeypatch):
    if bootstrap_store.os.name != "nt":
        pytest.skip("Windows permissions")
    seen = {}
    monkeypatch.setenv("TRIAGE_SERVICE_ACCOUNT", r"NT SERVICE\TRIAGE1")
    monkeypatch.setattr(bootstrap_store.subprocess, "run", lambda cmd, **k: seen.update(cmd=cmd))
    bootstrap_store._lock(tmp_path / "f")
    assert r"NT SERVICE\TRIAGE1:(F)" in seen["cmd"]
