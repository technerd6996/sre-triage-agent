"""Per-host lock: at most one run may act on a given server at a time.
In-process only. The agent runs as one app process, so this is enough."""
import logging
import threading
import time
from contextlib import contextmanager

log = logging.getLogger(__name__)

_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()
# Safety net: records who holds each lock and when, so a leaked lock (acquired, never released --
# the exact bug that hung every run against a host after the first) shows up in logs within
# minutes instead of silently serializing every later run for up to `timeout` seconds.
_HELD: dict[str, dict] = {}            # host_id -> {"since": float, "run_id": str | None}
_WARN_AFTER_SECONDS = 120              # a real triage action should never hold the lock this long

def _lock_for(host_id: str) -> threading.Lock:
    with _GUARD:
        return _LOCKS.setdefault(host_id, threading.Lock())

@contextmanager
def host_lock(host_id: str, timeout: float = 600, run_id: str | None = None):
    lock = _lock_for(host_id)
    if not lock.acquire(timeout=timeout):
        raise TimeoutError(f"Host {host_id} is busy with another run. Try again later.")
    with _GUARD:
        _HELD[host_id] = {"since": time.time(), "run_id": run_id}
    try:
        yield
    finally:
        lock.release()
        with _GUARD:
            _HELD.pop(host_id, None)

def stale_locks(after_seconds: float = _WARN_AFTER_SECONDS) -> list[dict]:
    """Locks currently held longer than `after_seconds`. Empty in the normal case."""
    now = time.time()
    with _GUARD:
        return [{"host_id": h, "run_id": info["run_id"], "held_seconds": round(now - info["since"])}
                for h, info in _HELD.items() if now - info["since"] > after_seconds]

def _watchdog(interval: float = 60):
    while True:
        time.sleep(interval)
        try:
            for stale in stale_locks():
                log.warning(
                    "Host lock held unusually long: host=%s run=%s held_for=%ss "
                    "(this is the exact shape of a leaked lock -- if it never clears, every "
                    "other run against this host will hang with no steps until the 600s timeout)",
                    stale["host_id"], stale["run_id"], stale["held_seconds"],
                )
        except Exception:
            log.exception("host_lock watchdog failed")

def start_watchdog():
    """Call once at app startup. Safe to call more than once; only the first call starts it."""
    with _GUARD:
        if getattr(start_watchdog, "_started", False):
            return
        start_watchdog._started = True
    threading.Thread(target=_watchdog, daemon=True, name="host-lock-watchdog").start()