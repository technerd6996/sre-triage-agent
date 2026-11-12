"""Entry point of the packaged builds (Windows exe, Docker image). Does what scripts/start.ps1 does for a source checkout.

    triage1 serve                 make sure the secret key and schema exist, then serve
    triage1 init-db               apply db/schema.sql and exit (safe to repeat)
    triage1 bootstrap-merge       JSON object on stdin -> bootstrap.json (used by the installer)
    triage1 bootstrap-has         exit 0 when a database address is stored
    triage1 backup-now            one backup now (the installer runs this before an upgrade)
    triage1 restore <file>        put a backup back (administrators, on the server)
    triage1 version               print the version

Address: TRIAGE_HOST (default 127.0.0.1) and TRIAGE_PORT (default 8000). They are read here because the
settings live in the database and this runs before it is reachable.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from app import bootstrap_store

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEMA = BASE_DIR / "db" / "schema.sql"


def ensure_secret_key() -> bool:
    """A container has no installer to create the encryption key, so it is made once and kept in TRIAGE_HOME."""
    bootstrap_store.load()
    if os.getenv("TRIAGE_SECRET_KEY"):
        return False
    from cryptography.fernet import Fernet
    bootstrap_store.save(TRIAGE_SECRET_KEY=Fernet.generate_key().decode())
    return True


def apply_schema(wait_seconds: int = 60, sleep=time.sleep) -> None:
    """Waits for the database, which may still be starting with the server or in its own container."""
    import psycopg

    from app import db
    bootstrap_store.load()
    sql, last = SCHEMA.read_text(encoding="utf-8"), None
    for _ in range(max(1, wait_seconds)):
        try:
            with psycopg.connect(db.database_url(), connect_timeout=3, autocommit=True) as c:
                c.execute(sql)
            return
        except psycopg.OperationalError as e:
            last = e
            sleep(1)
    raise SystemExit(f"The database did not become ready in {wait_seconds} seconds: {type(last).__name__}")


def bind_address() -> tuple[str, int]:
    return os.getenv("TRIAGE_HOST") or "127.0.0.1", int(os.getenv("TRIAGE_PORT") or 8000)


def serve() -> None:
    ensure_secret_key()
    apply_schema()
    import uvicorn

    from app.main import app            # imported only now: importing it reads the settings from the database
    host, port = bind_address()
    uvicorn.run(app, host=host, port=port)


def _load_settings() -> None:
    bootstrap_store.load()
    from app import config
    config.hydrate_environ()


def backup_now(reason: str) -> int:
    _load_settings()
    from app import backup
    result = backup.run(reason)
    print(backup.describe(result), flush=True)
    return 0 if result["ok"] else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="triage1", description="TRIAGE-1 server")
    p.add_argument("command", choices=["serve", "init-db", "bootstrap-merge", "bootstrap-has", "backup-now", "restore", "version"])
    p.add_argument("file", nargs="?", help="restore: the triage-<date>.dump.enc file")
    p.add_argument("--yes", action="store_true", help="restore: do not ask for confirmation")
    p.add_argument("--no-safety-backup", action="store_true", help="restore: skip the backup of the current data")
    p.add_argument("--reason", default="manual", help="backup-now: why this backup was made")
    args = p.parse_args(argv)
    cmd = args.command
    if cmd == "version":
        from app.version import VERSION
        print(VERSION)
    elif cmd == "backup-now":
        return backup_now(args.reason)
    elif cmd == "restore":
        if not args.file:
            p.error("restore needs the backup file")
        _load_settings()
        from app import restore
        return restore.run(args.file, yes=args.yes, safety=not args.no_safety_backup)
    elif cmd == "serve":
        serve()
    elif cmd == "init-db":
        apply_schema()
        print("database ready")
    elif cmd == "bootstrap-merge":
        # Windows PowerShell can put a byte-order mark in front of text it pipes to a program.
        bootstrap_store.save(**json.loads(sys.stdin.read().lstrip("﻿").removeprefix("\u00ef\u00bb\u00bf")))
        print("bootstrap saved")
    elif cmd == "bootstrap-has":
        return 0 if bootstrap_store.read().get("DATABASE_URL") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
