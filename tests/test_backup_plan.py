"""Encryption, customer retention rules, off-machine copies, warnings and restore. No database or network."""
import datetime as dt
import json
import time
from types import SimpleNamespace

import pytest

from app import backup, backup_crypto, bootstrap_store, restore


# ---- encryption ----
def test_encrypted_file_round_trips_and_hides_the_content(tmp_path):
    src, enc, out = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    data = b"PGDMP secret-value " * 200000                       # several chunks
    src.write_bytes(data)
    backup_crypto.encrypt_file(src, enc, "key")
    assert backup_crypto.is_encrypted(enc) and b"secret-value" not in enc.read_bytes()
    backup_crypto.decrypt_file(enc, out, "key")
    assert out.read_bytes() == data


def _sealed(tmp_path, size=3 << 20):
    src, enc = tmp_path / "a", tmp_path / "b"
    src.write_bytes(b"x" * size)
    backup_crypto.encrypt_file(src, enc, "key")
    return enc, tmp_path / "out"


def test_wrong_key_is_refused_and_leaves_nothing(tmp_path):
    enc, out = _sealed(tmp_path)
    with pytest.raises(ValueError, match="recovery key"):
        backup_crypto.decrypt_file(enc, out, "other")
    assert not out.exists()


def test_cut_off_file_is_refused(tmp_path):
    enc, out = _sealed(tmp_path)
    enc.write_bytes(enc.read_bytes()[:(1 << 20) + 200])          # ends cleanly after the first chunk's worth
    with pytest.raises(ValueError):
        backup_crypto.decrypt_file(enc, out, "key")
    data = enc.read_bytes()
    enc.write_bytes(data[:-5])
    with pytest.raises(ValueError):
        backup_crypto.decrypt_file(enc, out, "key")


def test_a_file_shortened_at_a_chunk_boundary_is_refused(tmp_path):
    enc, out = _sealed(tmp_path)
    data = enc.read_bytes()
    size = int.from_bytes(data[backup_crypto._HEADER:backup_crypto._HEADER + 4], "big")
    enc.write_bytes(data[:backup_crypto._HEADER + 4 + size])     # a complete chunk, but not the marked last one
    with pytest.raises(ValueError):
        backup_crypto.decrypt_file(enc, out, "key")


def test_changed_byte_is_refused(tmp_path):
    enc, out = _sealed(tmp_path)
    data = bytearray(enc.read_bytes())
    data[len(data) // 2] ^= 1
    enc.write_bytes(data)
    with pytest.raises(ValueError):
        backup_crypto.decrypt_file(enc, out, "key")


# ---- retention ----
def _ts(y, m, d):
    return dt.datetime(y, m, d, 2, 0).timestamp()


NOW = _ts(2026, 11, 10)


def _days(n):
    return {f"d{i}": NOW - i * 86400 for i in range(n)}


def test_retention_days_only():
    keep = backup.keep_set(_days(30), NOW, 7, 0, 0)
    assert keep == {f"d{i}" for i in range(8)}


def test_retention_weekly_and_monthly_add_older_backups():
    times = _days(120)
    daily = backup.keep_set(times, NOW, 7, 0, 0)
    weekly = backup.keep_set(times, NOW, 7, 8, 0)
    both = backup.keep_set(times, NOW, 7, 8, 4)
    assert len(daily) < len(weekly) < len(both)
    assert len(weekly - daily) <= 8 and max(int(n[1:]) for n in weekly) < 7 * 9


def test_newest_backup_is_never_deleted():
    assert backup.keep_set({"old": NOW - 900 * 86400}, NOW, 1, 0, 0) == {"old"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    home = tmp_path / "triage"
    home.mkdir()
    (home / "bootstrap.json").write_text('{"TRIAGE_SECRET_KEY": "k"}', encoding="utf-8")
    saved, audits, settings = {}, [], {"backup.folder": "", "backup.pg_dump": "", "backup.copy_folder": "", "backup.keep_days": 14, "backup.keep_weeks": 0,
                                       "backup.keep_months": 0, "backup.encrypt": True, "backup.enabled": True, "backup.alert_hours": 36, "backup.alert_email": ""}
    monkeypatch.setenv("TRIAGE_HOME", str(home))
    monkeypatch.setenv("TRIAGE_BACKUP_KEY", "the-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql" + "://" + "triage" + ":" + "pw" + "@localhost:5433/triage")
    monkeypatch.setattr(backup.store, "set_setting", lambda k, v, by="": saved.__setitem__(k, v))
    monkeypatch.setattr(backup.store, "get_setting", lambda k, d=None: saved.get(k, d))
    monkeypatch.setattr(backup.store, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(backup, "find_tool", lambda name: name)
    monkeypatch.setattr(bootstrap_store, "_lock", lambda p: None)
    monkeypatch.setattr(backup.config, "get", lambda k: settings[k])
    calls = []

    def fake_run(cmd, env=None, **kw):
        calls.append(cmd)
        if "-f" in cmd:
            open(cmd[cmd.index("-f") + 1], "wb").write(b"PGDMP" * 50)
        return SimpleNamespace(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    return SimpleNamespace(home=home, saved=saved, audits=audits, settings=settings, calls=calls, tmp=tmp_path, mp=monkeypatch)


def _names(p):
    return sorted(x.name for x in p.iterdir())


def test_backups_are_encrypted_by_default_and_no_plain_copy_remains(env):
    r = backup.run()
    names = _names(env.home / "backups")
    assert r["ok"] and r["encrypted"] and all(n.endswith(".enc") for n in names) and len(names) == 2
    for n in names:
        assert backup_crypto.is_encrypted(env.home / "backups" / n)


def test_encryption_can_be_switched_off_and_is_then_reported(env):
    env.settings["backup.encrypt"] = False
    r = backup.run()
    assert r["ok"] and not any(n.endswith(".enc") for n in _names(env.home / "backups"))
    assert "NOT encrypted" in backup.describe(r) and any("not encrypted" in p for p in backup.health()["problems"])


def test_copy_goes_to_the_second_location(env):
    env.settings["backup.copy_folder"] = str(env.tmp / "share")
    r = backup.run()
    assert r["copied_to"] == str(env.tmp / "share") and _names(env.tmp / "share") == _names(env.home / "backups")
    assert not [n for n in _names(env.tmp / "share") if n.endswith(".part")]


def test_a_failed_copy_keeps_the_backup_and_raises_a_warning(env, monkeypatch):
    env.settings["backup.copy_folder"] = str(env.tmp / "share")

    def boom(*a):
        raise OSError("share is offline")
    monkeypatch.setattr(backup.shutil, "copyfile", lambda a, b: boom() if "share" in str(b) else open(b, "wb").write(open(a, "rb").read()))
    r = backup.run()
    assert r["ok"] and "offline" in r["copy_error"] and "FAILED" in backup.describe(r)
    assert any("off-machine" in p for p in backup.health()["problems"]) and env.audits[-1][3] == "backup_copy_failed"


def test_copy_folder_cannot_be_the_backup_folder(env):
    env.settings["backup.copy_folder"] = str(env.home / "backups")
    assert "same" in backup.run()["copy_error"]


def test_retention_also_applies_to_the_copy_and_leaves_other_files(env):
    import os
    share = env.tmp / "share"
    share.mkdir()
    env.settings["backup.copy_folder"] = str(share)
    old = time.time() - 40 * 86400
    for name in ("triage-20260101-020000.dump.enc", "bootstrap-20260101-020000.json.enc", "notes.txt"):
        (share / name).write_text("x")
        os.utime(share / name, (old, old))
    backup.run()
    names = _names(share)
    assert "notes.txt" in names and "triage-20260101-020000.dump.enc" not in names


def test_health_warns_when_backups_are_too_old_or_failed(env):
    env.saved["backup_last"] = {"ok": True, "at": 1}
    env.saved["backup_last_ok_at"] = time.time() - 40 * 3600
    assert any("hours old" in p for p in backup.health()["problems"])
    env.saved["backup_last"] = {"ok": False, "at": 2, "error": "disk full"}
    assert any("disk full" in p for p in backup.health()["problems"])


def test_health_warns_until_the_recovery_key_is_saved(env, monkeypatch):
    monkeypatch.delenv("TRIAGE_BACKUP_KEY")
    monkeypatch.setattr(backup, "recovery_key", lambda create=True: "stored-key")
    env.saved["backup_last"] = {"ok": True, "at": time.time()}
    env.saved["backup_last_ok_at"] = time.time()
    assert any("recovery key" in p for p in backup.health()["problems"])
    backup.confirm_key("admin@x")
    assert backup.health()["problems"] == [] and env.audits[-1][3] == "backup_key_confirmed"
    monkeypatch.setattr(backup, "recovery_key", lambda create=True: "a-new-key")     # a replaced key has to be saved again
    assert any("recovery key" in p for p in backup.health()["problems"])


def test_a_failed_read_never_replaces_the_recovery_key(env, monkeypatch):
    monkeypatch.delenv("TRIAGE_BACKUP_KEY")

    def down():
        raise RuntimeError("database unreachable")
    monkeypatch.setattr(backup, "_stored_key_exists", down)
    monkeypatch.setattr(backup.config, "save_secret", lambda *a: pytest.fail("must not create a key"))
    with pytest.raises(RuntimeError):
        backup.recovery_key()


def test_the_recovery_key_is_created_once(env, monkeypatch):
    monkeypatch.delenv("TRIAGE_BACKUP_KEY")
    stored = {}
    monkeypatch.setattr(backup, "_stored_key_exists", lambda: bool(stored))
    monkeypatch.setattr(backup.config, "save_secret", lambda k, v, by: stored.__setitem__(k, v))
    monkeypatch.setattr(backup.config, "get_secret", lambda k: stored[k])
    first = backup.recovery_key()
    assert len(first) > 40 and backup.recovery_key() == first


def test_alert_email_at_most_once_a_day(env, monkeypatch):
    from app import owner_contacts
    sent = []
    monkeypatch.setattr(owner_contacts, "send", lambda to, cc, s, b: (sent.append(to), (True, "id"))[1])
    env.settings["backup.alert_email"] = "ops@x.test; two@x.test"
    now = time.time()
    backup._alert(["p"], now)
    backup._alert(["p"], now + 3600)
    assert sent == [["ops@x.test", "two@x.test"]]
    backup._alert(["p"], now + 90000)
    assert len(sent) == 2


def test_the_recovery_key_is_super_admin_only_and_status_is_not():
    from app import rbac
    assert rbac.ROLES["super_admin"] >= {"users.manage"} and "users.manage" not in rbac.ROLES["admin"]
    text = open(rbac.__file__, encoding="utf-8").read()
    assert 'recovery-key(/confirm)?", "users.manage"' in text and 'backup/status", "monitor.view"' in text


# ---- restore ----
@pytest.fixture
def made(env, monkeypatch):
    monkeypatch.setattr(restore, "_service_exists", lambda: False)
    monkeypatch.setattr(restore, "_app_is_listening", lambda: False)
    monkeypatch.setattr(restore.backup, "find_tool", lambda name: name)
    from app import server
    monkeypatch.setattr(server, "apply_schema", lambda *a, **k: env.calls.append(["schema"]))
    assert backup.run()["ok"]
    env.calls.clear()
    env.file = next(p for p in (env.home / "backups").iterdir() if p.name.startswith("triage-"))
    return env


def _tools(env):
    return [c[0] for c in env.calls]


def test_restore_runs_in_one_transaction_after_a_safety_backup(made):
    code = restore.run(str(made.file), yes=True)
    assert code == 0 and _tools(made) == ["pg_dump", "pg_restore", "schema"]
    cmd = made.calls[1]
    assert "--single-transaction" in cmd and "--exit-on-error" in cmd and "--clean" in cmd and "pw" not in " ".join(cmd)
    assert not [p for p in made.home.iterdir() if p.name.startswith("restore-")]         # no decrypted copy stays


def test_restore_needs_the_word_restore(made):
    assert restore.run(str(made.file), ask=lambda t: "yes") == 1 and made.calls == []


def test_restore_with_a_wrong_key_changes_nothing(made, monkeypatch):
    monkeypatch.setenv("TRIAGE_BACKUP_KEY", "wrong")
    assert restore.run(str(made.file), yes=True) == 1 and made.calls == []


def test_restore_stops_if_the_safety_backup_fails(made, monkeypatch):
    monkeypatch.setattr(restore.backup, "run", lambda r: {"ok": False, "error": "disk full"})
    assert restore.run(str(made.file), yes=True) == 1 and made.calls == []


def test_restore_refuses_while_the_app_is_running(made, monkeypatch):
    monkeypatch.setattr(restore, "_app_is_listening", lambda: True)
    assert restore.run(str(made.file), yes=True) == 1 and made.calls == []


def test_a_failed_restore_says_the_database_was_left_alone(made, monkeypatch, capsys):
    monkeypatch.setattr(restore.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="boom", stdout=""))
    assert restore.run(str(made.file), yes=True, safety=False) == 1
    assert "left as it was" in capsys.readouterr().out


def test_restore_puts_the_backup_encryption_key_in_place(made):
    (made.home / "bootstrap.json").write_text(json.dumps({"TRIAGE_SECRET_KEY": "different", "DATABASE_URL": "keep-me"}), encoding="utf-8")
    assert restore.run(str(made.file), yes=True, safety=False) == 0
    stored = bootstrap_store.read()
    assert stored["TRIAGE_SECRET_KEY"] == "k" and stored["DATABASE_URL"] == "keep-me"


def test_service_is_started_again_even_when_restore_fails(made, monkeypatch):
    actions = []
    monkeypatch.setattr(restore, "_service_exists", lambda: True)
    monkeypatch.setattr(restore, "_service", actions.append)
    monkeypatch.setattr(restore.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="boom", stdout=""))
    restore.run(str(made.file), yes=True, safety=False)
    assert actions == ["stop", "start"]


def test_only_backup_files_are_accepted(made, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("x")
    assert restore.run(str(p), yes=True) == 2
