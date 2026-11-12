"""Scheduled backups, made by the app itself: a pg_dump of the database plus a copy of bootstrap.json.

Why inside the app: a Windows scheduled task needs an elevated session to register. The app is already running, so it
does the backup at the chosen hour with no extra set-up.

Losing bootstrap.json makes every saved credential unreadable (it holds the encryption key), so the two files are always
written together. Both are encrypted with a recovery key by default. The key is created automatically so the first
backup is already encrypted, and the admin panel warns until someone confirms they saved it: a recovery key that exists
only on this server does not survive losing this server.

run()      makes one backup now, applies the retention rules, copies it off the machine and records the result.
due()      pure decision: is a backup due at this moment.
health()   what is wrong with backups right now, for the admin Overview and the warning email.
start()    starts the background checker (called once at app start)."""
import datetime as dt
import glob
import hashlib
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app import backup_crypto, bootstrap_store, config, store

log = logging.getLogger("uvicorn.error")

config.register("backup.enabled", "Back up automatically every day", True, "bool", "backup",
                help="A database dump and a copy of bootstrap.json (the encryption key). Without that file the saved "
                     "credentials cannot be read, so both are always kept together.")
config.register("backup.hour", "Hour of the day to back up (0 to 23, server time)", 2, "int", "backup", lo=0, hi=23,
                help="If the app was not running at that hour, the backup is made when it is next running and the last "
                     "one is more than 36 hours old.")
config.register("backup.encrypt", "Encrypt backups", True, "bool", "backup",
                help="On by default. Turning this off leaves the database and its encryption key readable to anyone who can open the backup folder.")
config.register("backup.passphrase", "Backup recovery key", "", "secret", "backup",
                help="Created automatically. Replace it to use your own passphrase. Backups already made still need the key they were made with.")
config.register("backup.keep_days", "Keep every backup for (days)", 14, "int", "backup", lo=1, hi=3650)
config.register("backup.keep_weeks", "Also keep one backup a week for (weeks)", 0, "int", "backup", lo=0, hi=520,
                help="0 means none beyond the daily ones.")
config.register("backup.keep_months", "Also keep one backup a month for (months)", 0, "int", "backup", lo=0, hi=240,
                help="0 means none beyond the daily and weekly ones. The newest backup is never deleted.")
config.register("backup.folder", "Backup folder", "", "text", "backup", max_len=400,
                help="Empty means the 'backups' folder next to bootstrap.json.")
config.register("backup.copy_folder", "Copy each backup to (second disk or network share)", "", "text", "backup", max_len=400,
                help="Empty means no copy. A backup on the same disk does not survive losing that disk. For a network share, "
                     "give this server's computer account write access to it.")
config.register("backup.alert_hours", "Warn when the last good backup is older than (hours)", 36, "int", "backup", lo=1, hi=8760)
config.register("backup.alert_email", "Email backup warnings to", "", "text", "backup", max_len=400,
                help="Empty means show the warning in the admin panel only. At most one email a day.")
config.register("backup.pg_dump", "Path to pg_dump", "", "text", "backup", max_len=400,
                help="Empty means find it automatically (the bundled copy, PATH, then the PostgreSQL folder under Program Files).")

_LOCK = threading.Lock()
_started = False
_FILE = re.compile(r"^(?:triage|bootstrap)-(\d{8}-\d{4,6})\.(?:dump|json)(?:\.enc)?$")
_BASE = Path(__file__).resolve().parent.parent


def folder() -> Path:
    custom = (config.get("backup.folder") or "").strip()
    return Path(custom) if custom else bootstrap_store.home() / "backups"


def find_tool(name: str) -> str:
    """pg_dump or pg_restore: the configured folder, the copy bundled with the installer, PATH, then Program Files."""
    custom = (config.get("backup.pg_dump") or "").strip()
    exe = name + (".exe" if os.name == "nt" else "")
    if custom:
        p = Path(custom).with_name(exe)
        if not p.is_file():
            raise RuntimeError(f"{name} was not found at {p}")
        return str(p)
    bundled = _BASE / "pgsql" / "bin" / exe
    if bundled.is_file():
        return str(bundled)
    hit = shutil.which(name)
    if hit:
        return hit
    found = sorted(glob.glob(rf"C:\Program Files\PostgreSQL\*\bin\{exe}"))
    if found:
        return found[-1]
    raise RuntimeError(f"{name} was not found. Install the PostgreSQL client tools or set its path in the Backups settings.")


def find_pg_dump() -> str:
    return find_tool("pg_dump")


# ---- recovery key ----
def _key_in_env() -> str:
    return os.getenv("TRIAGE_BACKUP_KEY", "")


def _stored_key_exists() -> bool:
    """Asks the database directly. A cached or failed read must never look like 'no key', or a key would be replaced."""
    rows = store.supabase.table("settings").select("key").eq("key", config.SEC + "backup.passphrase").execute().data
    return bool(rows)


def recovery_key(create: bool = True) -> str:
    env = _key_in_env()
    if env:
        return env
    if _stored_key_exists():
        return config.get_secret("backup.passphrase")
    if not create:
        return ""
    key = "-".join(secrets.token_hex(4).upper() for _ in range(8))
    config.save_secret("backup.passphrase", key, "system")
    return key


def _fingerprint(key: str) -> str:
    return hashlib.sha256(("triage-backup:" + key).encode()).hexdigest()[:24]


def key_confirmed() -> bool:
    if _key_in_env():
        return True
    key = recovery_key(create=False)
    return bool(key) and store.get_setting("backup_key_confirmed", "") == _fingerprint(key)


def reveal_key(by: str) -> str:
    if _key_in_env():
        raise ValueError("The recovery key is set in this server's environment, not stored by the app.")
    key = recovery_key()
    store.audit(None, None, by, "backup_key_shown", "recovery key displayed")
    return key


def confirm_key(by: str) -> None:
    store.set_setting("backup_key_confirmed", _fingerprint(recovery_key()), by)
    store.audit(None, None, by, "backup_key_confirmed", "recovery key saved by the customer")


# ---- retention ----
def keep_set(times: dict, now: float, days: int, weeks: int, months: int) -> set:
    """Which backups stay. {name: time}: everything within `days`, the newest of each of the last `weeks` weeks and
    `months` months, and always the newest one."""
    newest_first = sorted(times, key=times.get, reverse=True)
    keep = {n for n in newest_first if now - times[n] <= days * 86400}
    if newest_first:
        keep.add(newest_first[0])
    for limit, bucket in ((weeks, lambda d: tuple(d.isocalendar())[:2]), (months, lambda d: (d.year, d.month))):
        seen = []
        for n in newest_first:
            b = bucket(dt.datetime.fromtimestamp(times[n]))
            if b not in seen:
                if len(seen) >= limit:
                    break
                seen.append(b)
                keep.add(n)
    return keep


def _prune(dest: Path, now: float) -> int:
    """Applies the retention rules to this app's own backup files. Other files in the folder are never touched."""
    groups = {}
    for p in dest.iterdir():
        m = _FILE.match(p.name)
        if m and p.is_file():
            groups.setdefault(m.group(1), []).append(p)
    times = {s: max(p.stat().st_mtime for p in files) for s, files in groups.items()}
    keep = keep_set(times, now, int(config.get("backup.keep_days")), int(config.get("backup.keep_weeks")), int(config.get("backup.keep_months")))
    removed = 0
    for stamp, files in groups.items():
        if stamp not in keep:
            for p in files:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def _lock_quietly(p: Path) -> None:
    try:
        bootstrap_store._lock(p)                             # owner and Administrators only
    except Exception as e:                                   # noqa: BLE001
        log.warning("backup file could not be locked down (%s): %s", p.name, str(e)[:120])


def _copy_off(files: list, dest: Path, now: float) -> str:
    target = Path((config.get("backup.copy_folder") or "").strip())
    if target.resolve() == dest.resolve():
        raise RuntimeError("the copy folder is the same as the backup folder")
    target.mkdir(parents=True, exist_ok=True)
    for f in files:
        part = target / (f.name + ".part")
        shutil.copyfile(f, part)
        _lock_quietly(part)
        os.replace(part, target / f.name)                    # a half-copied file must not look like a backup
    _prune(target, now)
    return str(target)


def _record(result: dict) -> None:
    try:
        store.set_setting("backup_last", result, "backup")
        if result["ok"]:
            store.set_setting("backup_last_ok_at", result["at"], "backup")     # survives a later failed attempt
        store.audit(None, None, "system", "backup_done" if result["ok"] else "backup_failed",
                    (result.get("file") or result.get("error") or "")[:300])
        if result.get("copy_error"):
            store.audit(None, None, "system", "backup_copy_failed", result["copy_error"][:300])
    except Exception as e:                                   # noqa: BLE001
        log.warning("backup result could not be recorded: %s", str(e)[:150])


def run(reason: str = "") -> dict:
    """One backup now. Returns {"ok", "at", "file", "mb", "removed", ...} or {"ok": False, "at", "error"}. Never raises."""
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "at": time.time(), "error": "a backup is already running"}
    now = time.time()
    temp = []
    try:
        url = os.getenv("DATABASE_URL", "")
        boot = bootstrap_store.path()
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        u = urlsplit(url)
        pg = find_pg_dump()
        encrypt = bool(config.get("backup.encrypt"))
        secret = recovery_key() if encrypt else ""
        dest = folder()
        dest.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        dump = dest / f"triage-{stamp}.dump"
        temp.append(dump)
        env = {**os.environ, "PGPASSWORD": unquote(u.password or "")}      # never on the command line
        cmd = [pg, "-h", u.hostname or "localhost", "-p", str(u.port or 5432), "-U", unquote(u.username or ""),
               "-d", (u.path or "/").lstrip("/"), "-Fc", "-f", str(dump)]
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)
        if done.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {(done.stderr or '').strip()[:200] or 'exit code ' + str(done.returncode)}")
        if not dump.is_file() or dump.stat().st_size == 0:
            raise RuntimeError("pg_dump wrote an empty file")
        files = [dump]
        copied = False
        if boot.is_file():                                   # absent when the two values come from the environment
            keyfile = dest / f"bootstrap-{stamp}.json"
            temp.append(keyfile)
            shutil.copyfile(boot, keyfile)                   # a fresh date, so clean-up removes it together with its dump
            files.append(keyfile)
            copied = True
        for p in files:
            _lock_quietly(p)
        if encrypt:
            sealed = []
            for p in files:
                enc = p.with_name(p.name + ".enc")
                temp.append(enc)
                backup_crypto.encrypt_file(p, enc, secret)
                _lock_quietly(enc)
                p.unlink()
                sealed.append(enc)
            files = sealed
        result = {"ok": True, "at": now, "file": str(files[0]), "mb": round(files[0].stat().st_size / 1048576, 2),
                  "key_copied": copied, "encrypted": encrypt, "reason": reason}
        temp.clear()
        result["removed"] = _prune(dest, now)
        if (config.get("backup.copy_folder") or "").strip():
            try:
                result["copied_to"] = _copy_off(files, dest, now)
            except Exception as e:                           # noqa: BLE001  the local backup is still good
                result["copy_error"] = (str(e) or type(e).__name__)[:300]
                log.error("backup copy failed: %s", result["copy_error"])
    except Exception as e:                                   # noqa: BLE001
        for p in temp:
            try:
                p.unlink(missing_ok=True)                    # a half-written file must not look like a backup
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
    text = f"backup written at {when}: {r.get('file')} ({r.get('mb')} MB), {key}, {'encrypted' if r.get('encrypted') else 'NOT encrypted'}"
    if r.get("copied_to"):
        text += f", copied to {r['copied_to']}"
    if r.get("copy_error"):
        text += f". The copy FAILED: {r['copy_error']}"
    return text


def health(now: float = None) -> dict:
    """What needs attention. Never raises: a problem reading the state is itself reported."""
    now = time.time() if now is None else now
    problems = []
    try:
        r = last()
        last_ok = store.get_setting("backup_last_ok_at", None)
        hours = int(config.get("backup.alert_hours"))
        if not config.get("backup.enabled"):
            problems.append("automatic backups are switched off")
        elif last_ok and now - float(last_ok) > hours * 3600:
            problems.append(f"the last good backup is {int((now - float(last_ok)) / 3600)} hours old")
        elif not last_ok and r:
            problems.append("no backup has succeeded yet")
        if r and not r.get("ok"):
            problems.append(f"the last backup failed: {r.get('error', 'unknown error')}")
        if r.get("copy_error"):
            problems.append(f"the off-machine copy failed: {r['copy_error']}")
        if not config.get("backup.encrypt"):
            problems.append("backups are not encrypted")
        elif r.get("ok") and not key_confirmed():
            problems.append("the backup recovery key has not been saved. Without it, backups cannot be restored after losing this server")
        return {"problems": problems, "last_ok_at": last_ok, "summary": describe(r), "key_confirmed": key_confirmed(),
                "copy_set": bool((config.get("backup.copy_folder") or "").strip())}
    except Exception as e:                                   # noqa: BLE001
        return {"problems": [f"backup state could not be read: {str(e)[:150]}"], "last_ok_at": None, "summary": "", "key_confirmed": False, "copy_set": False}


def _alert(problems: list, now: float) -> None:
    """At most one email a day. A warning that cannot be sent is logged, and the admin panel still shows it."""
    to = [a for a in re.split(r"[,;\s]+", (config.get("backup.alert_email") or "").strip()) if a]
    if not problems or not to or now - float(store.get_setting("backup_alert_at", 0) or 0) < 86400:
        return
    from app import owner_contacts
    ok, detail = owner_contacts.send(to, [], "[TRIAGE-1] Backups need attention", "TRIAGE-1 backups need attention:\n\n- " + "\n- ".join(problems))
    if ok:
        store.set_setting("backup_alert_at", now, "backup")
    else:
        log.warning("backup warning email was not sent: %s", str(detail)[:150])


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
            _alert(health()["problems"], time.time())
        except Exception as e:                               # noqa: BLE001
            log.warning("backup check failed: %s", str(e)[:150])
        time.sleep(300)


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="backup").start()
