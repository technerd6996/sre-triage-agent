"""The two values that cannot live in the database: DATABASE_URL (needed to reach it) and TRIAGE_SECRET_KEY
(the key that encrypts every saved credential). There is no .env file. Both are generated on first start and kept
in one file outside the project, readable only by the owner and Administrators:

    Windows : C:\\ProgramData\\triage\\bootstrap.json      (override the folder with TRIAGE_HOME)
    other   : ~/.triage/bootstrap.json

Environment variables of the same name win over the file (useful for containers and CI).
Everything else (ServiceNow, WinRM, AWS, LDAP ...) is entered on the setup page and stored in the database.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

NAMES = ("DATABASE_URL", "TRIAGE_SECRET_KEY")


def home() -> Path:
    if os.getenv("TRIAGE_HOME"):
        return Path(os.environ["TRIAGE_HOME"])
    if os.name == "nt":
        return Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "triage"
    return Path.home() / ".triage"


def path() -> Path:
    return home() / "bootstrap.json"


def read() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        return {k: str(v) for k, v in data.items() if k in NAMES and v}
    except (OSError, ValueError):
        return {}


def load() -> None:
    """Puts the stored values into os.environ, never overriding a variable that is already set."""
    for k, v in read().items():
        os.environ.setdefault(k, v)


def _lock(p: Path) -> None:
    """Owner and Administrators only."""
    if os.name == "nt":
        # A Windows service runs as its own account, which the installer names here.
        user = os.getenv("TRIAGE_SERVICE_ACCOUNT") or os.getenv("USERNAME", "")
        cmd =["icacls", str(p), "/inheritance:r", "/grant:r", "*S-1-5-32-544:(F)", "*S-1-5-18:(F)"]
        if user:
            cmd.append(f"{user}:(F)")
        subprocess.run(cmd, check=True, capture_output=True)
    else:
        os.chmod(p, 0o600)


def save(**values: str) -> None:
    """Merges values into the file (creating the folder), then locks the file down."""
    bad = set(values) - set(NAMES)
    if bad:
        raise ValueError(f"not a bootstrap value: {sorted(bad)}")
    home().mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(home(), 0o700)
    data = {**read(), **{k: v for k, v in values.items() if v}}
    p = path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    _lock(tmp)
    os.replace(tmp, p)
    for k, v in values.items():
        os.environ[k] = v


if __name__ == "__main__":
    # python -m app.bootstrap_store merge   <- JSON object on stdin; used by scripts/start.ps1
    if len(sys.argv) == 2 and sys.argv[1] == "merge":
        save(**json.load(sys.stdin))
        print("bootstrap saved to", path())
    elif len(sys.argv) == 2 and sys.argv[1] == "has":
        sys.exit(0 if read().get("DATABASE_URL") else 1)
    else:
        sys.exit("usage: python -m app.bootstrap_store merge|has")
