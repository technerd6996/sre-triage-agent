"""SQLite persistence for runs, run events and the audit trail.
Swap the connection for Postgres later without touching callers."""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.getenv("TRIAGE_DB", "triage_runs.db")
_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript("""
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, ticket TEXT, status TEXT, skill TEXT, server TEXT,
    thread_id TEXT, created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT, seq INTEGER, ts REAL, body TEXT,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, ticket TEXT,
    actor TEXT, action TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);
""")
_conn.commit()


def _exec(sql: str, params: tuple = ()):
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def save_run(run: dict) -> None:
    _exec(
        "INSERT INTO runs (id, ticket, status, skill, server, thread_id, created, updated) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "status=excluded.status, skill=excluded.skill, server=excluded.server, "
        "thread_id=excluded.thread_id, updated=excluded.updated",
        (run["id"], run["ticket"], run["status"], run.get("skill"), run.get("server"),
         run.get("thread_id"), run.get("created", time.time()), time.time()),
    )


def add_event(run_id: str, seq: int, ev: dict) -> None:
    _exec("INSERT OR REPLACE INTO events (run_id, seq, ts, body) VALUES (?,?,?,?)",
          (run_id, seq, time.time(), json.dumps(ev, default=str)))


def audit(run_id: str | None, ticket: str | None, actor: str, action: str, detail: str = "") -> None:
    _exec("INSERT INTO audit (ts, run_id, ticket, actor, action, detail) VALUES (?,?,?,?,?,?)",
          (time.time(), run_id, ticket, actor, action, detail[:1000]))


def recent_audit(limit: int = 100) -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def load_runs(limit: int = 20) -> list[dict]:
    """Most recent runs with their events, oldest first (same order the API keeps in memory).
    Runs that were mid-flight when the server stopped cannot be resumed (the agent checkpoint
    is in memory), so they are marked 'interrupted'."""
    with _lock:
        rows = _conn.execute("SELECT * FROM runs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in reversed(rows):
            evs = [json.loads(e["body"]) for e in _conn.execute(
                "SELECT body FROM events WHERE run_id=? ORDER BY seq", (r["id"],)).fetchall()]
            run = {"id": r["id"], "ticket": r["ticket"], "status": r["status"], "skill": r["skill"],
                   "server": r["server"], "thread_id": r["thread_id"], "created": r["created"],
                   "events": evs, "pending": None}
            out.append(run)
    for run in out:
        if run["status"] in ("running", "awaiting_approval"):
            run["status"] = "interrupted"
            run["events"].append({"type": "error",
                                  "message": "Server restarted while this run was in progress. Re-run the ticket."})
            add_event(run["id"], len(run["events"]) - 1, run["events"][-1])
            save_run(run)
            audit(run["id"], run["ticket"], "system", "run_interrupted", "server restart")
    return out