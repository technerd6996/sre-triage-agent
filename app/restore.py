"""Restores a backup: triage1 restore <dump file>. Run by an administrator on the server, never from the web.

A web page cannot reliably replace the database the app is using, and a stolen admin session must not be able to wipe it.
pg_restore runs in one transaction, so a restore that fails leaves the database as it was."""
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app import backup, backup_crypto, bootstrap_store, store

SERVICE = "TRIAGE1"
_NAME = re.compile(r"^triage-(\d{8}-\d{4,6})\.dump(\.enc)?$")


def _say(text: str) -> None:
    print(text, flush=True)


def _service_exists() -> bool:
    return os.name == "nt" and subprocess.run(["sc.exe", "query", SERVICE], capture_output=True).returncode == 0


def _service(action: str) -> None:
    subprocess.run(["sc.exe", action, SERVICE], capture_output=True)
    want = "STOPPED" if action == "stop" else "RUNNING"
    for _ in range(40):
        if want in subprocess.run(["sc.exe", "query", SERVICE], capture_output=True, text=True).stdout:
            return
        time.sleep(1)
    raise RuntimeError(f"The {SERVICE} service did not reach {want}.")


def _app_is_listening() -> bool:
    from app.server import bind_address
    host, port = bind_address()
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1" if host == "0.0.0.0" else host, port)) == 0


def _open_file(folder: Path, name: str) -> Path:
    p = folder / name
    p.touch()
    bootstrap_store._lock(p)                                 # locked before any decrypted byte is written
    return p


def _unseal(src: Path, folder: Path, name: str, secret: str) -> Path:
    if not backup_crypto.is_encrypted(src):
        return src
    if not secret:
        raise ValueError("This backup is encrypted and needs its recovery key.")
    out = _open_file(folder, name)
    backup_crypto.decrypt_file(src, out, secret)
    return out


def run(file: str, yes: bool = False, safety: bool = True, ask=input, ask_secret=getpass.getpass) -> int:
    src = Path(file)
    m = _NAME.match(src.name)
    if not m or not src.is_file():
        _say("Give the path of a backup file named triage-<date>.dump or triage-<date>.dump.enc.")
        return 2
    url = os.getenv("DATABASE_URL", "")
    if not url:
        _say("No database address is set on this server. Finish the installation first.")
        return 2
    u = urlsplit(url)
    target = f"{(u.path or '/').lstrip('/')} on {u.hostname}"
    sibling = next((p for p in (src.with_name(f"bootstrap-{m.group(1)}.json.enc"), src.with_name(f"bootstrap-{m.group(1)}.json")) if p.is_file()), None)

    bootstrap_store.home().mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="restore-", dir=bootstrap_store.home()))
    stopped = False
    try:
        secret = ""
        if backup_crypto.is_encrypted(src):
            secret = os.getenv("TRIAGE_BACKUP_KEY", "") or ask_secret("Backup recovery key: ")
        dump = _unseal(src, work, "restore.dump", secret)
        with open(dump, "rb") as f:
            if f.read(5) != b"PGDMP":
                _say("This file is not a database backup.")
                return 2
        new_key = ""
        if sibling:
            keyfile = _unseal(sibling, work, "restore.json", secret)
            new_key = json.loads(keyfile.read_text(encoding="utf-8")).get("TRIAGE_SECRET_KEY", "")
        else:
            _say("WARNING: the key file from this backup was not found next to it. Saved credentials will only be readable if this server already has the same encryption key.")

        _say(f"This replaces ALL data in the database {target} with the backup from {m.group(1)}.")
        if not yes and ask("Type RESTORE to continue: ").strip() != "RESTORE":
            _say("Nothing was changed.")
            return 1

        if _service_exists():
            _say("Stopping the TRIAGE-1 service...")
            _service("stop")
            stopped = True
        if _app_is_listening():
            _say("TRIAGE-1 is still running on this machine. Stop it, then run this again. Nothing was changed.")
            return 1

        if safety:
            _say("Backing up the current data first...")
            r = backup.run("before restore")
            if not r["ok"]:
                _say(f"The safety backup failed: {r['error']}\nNothing was changed. To restore anyway, add --no-safety-backup.")
                return 1
            _say(f"Safety backup: {r['file']}")

        _say("Restoring...")
        env = {**os.environ, "PGPASSWORD": unquote(u.password or "")}
        cmd = [backup.find_tool("pg_restore"), "-h", u.hostname or "localhost", "-p", str(u.port or 5432), "-U", unquote(u.username or ""),
               "-d", (u.path or "/").lstrip("/"), "--clean", "--if-exists", "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction", str(dump)]
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=7200)
        if done.returncode != 0:
            _say(f"The restore failed and the database was left as it was: {(done.stderr or '').strip()[:400]}")
            return 1
        if new_key and new_key != bootstrap_store.read().get("TRIAGE_SECRET_KEY"):
            bootstrap_store.save(TRIAGE_SECRET_KEY=new_key)      # the database address of this server is kept
            _say("The encryption key from the backup was put in place.")
        from app import server
        server.apply_schema()                                    # brings an older backup up to this version's tables
        try:
            store.audit(None, None, "system", "backup_restored", src.name[:200])
        except Exception:                                        # noqa: BLE001  the restore itself succeeded
            pass
        _say("Restore complete.")
        return 0
    except ValueError as e:
        _say(str(e))
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)                  # decrypted copies never stay on disk
        if stopped:
            _say("Starting the TRIAGE-1 service...")
            _service("start")
