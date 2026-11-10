"""The history scanner names the kind of secret and never prints its value."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("scan_history", Path(__file__).resolve().parent.parent / "scripts" / "scan_history.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

KEY = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIabc\n"
DB = "postgresql" + "://" + "app" + ":" + "s3cretpw" + "@db:5432/x"


def test_kinds_are_named_for_real_secrets():
    assert scan.kinds_in(KEY, "Creds/zabb.pem") == ["private key file (PEM)"]
    assert "database address with a password" in scan.kinds_in("DATABASE_URL=" + DB, "notes.env")
    assert "password assignment" in scan.kinds_in("SNOW_PASSWORD=Sup3rSecretValue", ".env.example")
    assert "password assignment" in scan.kinds_in("DEFAULT_ADMIN_PASSWORD  = Sup3rSecretValue", "Creds/Triage_1.txt")


def test_code_and_placeholders_are_not_reported():
    assert scan.kinds_in('DATABASE_URL = "postgresql' + '://triage:$appPw@127.0.0.1:5432/triage"', "scripts/start.ps1") == []
    assert scan.kinds_in("def login(user: str, password: str) -> bool:", "app/x.py") == []
    assert scan.kinds_in("token = request.cookies.get('triage_session')", "old.bak") == []


def test_the_report_never_contains_the_value(capsys, monkeypatch):
    monkeypatch.setattr(scan, "scan_history", lambda: [{"where": "git history", "path": "Creds/x.txt", "kind": "password assignment",
                                                        "commits": 2, "first_commit": "aaaaaaa", "last_commit": "bbbbbbb", "still_tracked": False}])
    monkeypatch.setattr(scan, "scan_folder", lambda: [])
    monkeypatch.setattr(scan.sys, "argv", ["scan_history.py"])
    assert scan.main() == 1
    out = capsys.readouterr().out
    assert "Creds/x.txt: password assignment" in out and "Sup3rSecretValue" not in out
