"""Scheduled backups, made by the app itself: a pg_dump of the database plus a copy of bootstrap.json.

Why inside the app: a Windows scheduled task needs an elevated session to register, which is why the nightly backup was
never switched on. The app is already running, so it does the backup at the chosen hour with no extra set-up.

Losing bootstrap.json makes every saved credential unreadable (it holds the encryption key), so the two files are
always written together. The backup folder therefore holds the key AND the encrypted credentials: it is locked to the
owner and Administrators, and it must be copied OFF this machine to somewhere equally protected (see `backup.folder`).

run()     makes one backup now and records the result (setting `backup_last`, audit row).
due()     pure decision: is a backup due at this moment.
start()   starts the background checker (called once at app start)."""
import glob
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app import bootstrap_store, config, store

log = logging.getLogger("uvicorn.error")

config.register("backup.enabled", "Back up automatically every day", True, "bool", "backup",
                help="A database dump and a copy of bootstrap.json (the encryption key). Without that file the saved "
                     "credentials cannot be read, so both are always kept together.")
config.register("backup.hour", "Hour of the day to back up (0 to 23, server time)", 2, "int", "backup", lo=0, hi=23,
                help="If the app was not running at that hour, the backup is made when it is next running and the last "
                     "one is more than 36 hours old.")
config.register("backup.keep_days", "Days to keep backups", 14, "int", "backup", lo=1, hi=3650)
config.register("backup.folder", "Backup folder", "", "text", "backup", max_len=400,
                help="Empty means the 'backups' folder next to bootstrap.json. Point it at another disk or a network share: "
                     "a backup on the same disk does not survive losing that disk.")
config.register("backup.pg_dump", "Path to pg_dump", "", "text", "backup", max_len=400,
                help="Empty means find it automatically (PATH, then the PostgreSQL folder under Program Files).")

_LOCK = threading.Lock()
_started = False


def folder() -> Path:
    custom = (config.get("backup.folder") or "").strip()
    return Path(custom) if custom else bootstrap_store.home() / "backups"


def find_pg_dump() -> str:
    custom = (config.get("backup.pg_dump") or "").strip()
    if custom:
        if not Path(custom).is_file():
            raise RuntimeError(f"pg_dump was not found at {custom}")
        return custom
    hit = shutil.which("pg_dump")
    if hit:
        return hit
    found = sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin\pg_dump.exe"))
    if found:
        return found[-1]
    raise RuntimeError("pg_dump was not found. Install the PostgreSQL client tools or set its path in the Backups settings.")


def _prune(dest: Path, keep_days: int, now: float) -> int:
    """Deletes this app's own backup files older than keep_days. Other files in the folder are never touched."""
    removed = 0
    for pattern in ("triage-*.dump", "bootstrap-*.json"):
        for p in dest.glob(pattern):
            try:
                if now - p.stat().st_mtime > keep_days * 86400:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def _record(result: dict) -> None:
    try:
        store.set_setting("backup_last", result, "backup")
        if result["ok"]:
            store.set_setting("backup_last_ok_at", result["at"], "backup")     # survives a later failed attempt
        store.audit(None, None, "system", "backup_done" if result["ok"] else "backup_failed",
                    (result.get("file") or result.get("error") or "")[:300])
    except Exception as e:                                   # noqa: BLE001
        log.warning("backup result could not be recorded: %s", str(e)[:150])


def run() -> dict:
    """One backup now. Returns {"ok", "at", "file", "mb", "removed"} or {"ok": False, "at", "error"}. Never raises."""
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "at": time.time(), "error": "a backup is already running"}
    now = time.time()
    dump = None
    try:
        url = os.getenv("DATABASE_URL", "")
        boot = bootstrap_store.path()
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        u = urlsplit(url)
        pg = find_pg_dump()
        dest = folder()
        dest.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        dump = dest / f"triage-{stamp}.dump"
        env = {**os.environ, "PGPASSWORD": unquote(u.password or "")}      # never on the command line
        cmd = [pg, "-h", u.hostname or "localhost", "-p", str(u.port or 5432), "-U", unquote(u.username or ""),
               "-d", (u.path or "/").lstrip("/"), "-Fc", "-f", str(dump)]
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)
        if done.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {(done.stderr or '').strip()[:200] or 'exit code ' + str(done.returncode)}")
        if not dump.is_file() or dump.stat().st_size == 0:
            raise RuntimeError("pg_dump wrote an empty file")
        copied = False
        if boot.is_file():                                   # absent when the two values come from the environment
            keyfile = dest / f"bootstrap-{stamp}.json"
            shutil.copyfile(boot, keyfile)               # a fresh date, so clean-up removes it together with its dump
            copied = True
            for p in (dump, keyfile):
                try:
                    bootstrap_store._lock(p)                 # owner and Administrators only
                except Exception as e:                       # noqa: BLE001
                    log.warning("backup file could not be locked down (%s): %s", p.name, str(e)[:120])
        removed = _prune(dest, int(config.get("backup.keep_days")), now)
        result = {"ok": True, "at": now, "file": str(dump), "mb": round(dump.stat().st_size / 1048576, 2),
                  "key_copied": copied, "removed": removed}
    except Exception as e:                                   # noqa: BLE001
        if dump is not None:
            try:
                dump.unlink(missing_ok=True)                 # a half-written dump must not look like a backup
            except OSError:
                pass
        result = {"ok": False, "at": now, "error": (str(e) or type(e).__name__)[:300]}
        log.error("backup failed: %s", result["error"])
    finally:
        _LOCK.release()
    _record(result)
    return result


def last() -> dict:
    return store.get_setting("backup_last", None) or {}


def due(now: float, last_ok_at, enabled: bool, hour: int) -> bool:
    """A backup is due when: it is the chosen hour and none was made in the last 20 hours, or the last good one is more
    than 36 hours old (the app was off at the chosen hour), or there has never been one."""
    if not enabled:
        return False
    if not last_ok_at:
        return True
    age = now - float(last_ok_at)
    if age > 36 * 3600:
        return True
    return time.localtime(now).tm_hour == int(hour) and age > 20 * 3600


def describe(result: dict = None) -> str:
    """One sentence for the admin panel."""
    r = last() if result is None else result
    if not r:
        return "no backup has been made yet"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("at", 0)))
    if not r.get("ok"):
        return f"the last backup FAILED at {when}: {r.get('error', 'unknown error')}"
    key = "with the key file" if r.get("key_copied") else "WITHOUT the key file (it is not on disk; back up TRIAGE_SECRET_KEY yourself)"
    return f"backup written at {when}: {r.get('file')} ({r.get('mb')} MB), {key}"


def _loop() -> None:
    from app import telemetry
    telemetry.mute()                                         # not app traffic
    time.sleep(90)                                           # let the app finish starting
    while True:
        try:
            prev = last()
            last_ok = store.get_setting("backup_last_ok_at", None)
            # after a failure, wait an hour before trying again instead of every five minutes
            recently_failed = prev and not prev.get("ok") and time.time() - float(prev.get("at", 0)) < 3600
            if not recently_failed and due(time.time(), last_ok, bool(config.get("backup.enabled")), int(config.get("backup.hour"))):
                run()
        except Exception as e:                               # noqa: BLE001
            log.warning("backup check failed: %s", str(e)[:150])
        time.sleep(300)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="backup").start()
