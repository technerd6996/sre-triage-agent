"""Entry point of the packaged builds (Windows exe, Docker image). Does what scripts/start.ps1 does for a source checkout.

    triage1 serve                 make sure the secret key and schema exist, then serve
    triage1 init-db               apply db/schema.sql and exit (safe to repeat)
    triage1 bootstrap-merge       JSON object on stdin -> bootstrap.json (used by the installer)
    triage1 bootstrap-has         exit 0 when a database address is stored

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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="triage1", description="TRIAGE-1 server")
    p.add_argument("command", choices=["serve", "init-db", "bootstrap-merge", "bootstrap-has"])
    cmd = p.parse_args(argv).command
    if cmd == "serve":
        serve()
    elif cmd == "init-db":
        apply_schema()
        print("database ready")
    elif cmd == "bootstrap-merge":
        bootstrap_store.save(**json.load(sys.stdin))
        print("bootstrap saved")
    elif cmd == "bootstrap-has":
        return 0 if bootstrap_store.read().get("DATABASE_URL") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
