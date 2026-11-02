"""First-run helpers: the shipped Default Super Admin, the password rules, and the installed state."""
import logging
import secrets
import time

from passlib.context import CryptContext

from app import store

log = logging.getLogger("uvicorn.error")

DEFAULT_ADMIN_ID = "superadmin@triage.local"
DEFAULT_ADMIN_PASSWORD = "ChangeMe-Triage1!"      # shipped default: usable once, the first login must replace it
MIN_PASSWORD = 12

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def check_new_password(new: str, user_id: str, current: str = "") -> str | None:
    """A message describing the problem, or None when the password is acceptable."""
    if len(new) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters"
    if new == DEFAULT_ADMIN_PASSWORD:
        return "Choose a password different from the shipped default"
    if current and new == current:
        return "The new password must differ from the current one"
    if new.lower() == (user_id or "").lower():
        return "The password cannot be the same as the user name"
    return None


def seed_default_admin(force: bool = False) -> bool:
    """Creates the Default Super Admin when the system has no active admin (or force=True).
    Returns True if it was created."""
    db = store.supabase
    if not force and db.table("users").select("user_id").in_("role", ["admin", "super_admin"]).eq("status", "active").limit(1).execute().data:
        return False
    if db.table("users").select("user_id").eq("user_id", DEFAULT_ADMIN_ID).execute().data:
        return False
    db.table("users").insert({
        "user_id": DEFAULT_ADMIN_ID, "name": "Default Super Admin", "role": "super_admin", "type": "human",
        "status": "active", "password_hash": _pwd.hash(DEFAULT_ADMIN_PASSWORD),
        "must_change_password": True}).execute()
    store.audit(None, None, "system", "default_admin_created", DEFAULT_ADMIN_ID)
    return True


def default_admin_unchanged() -> bool:
    """True while the default account exists, is active, and still has the shipped password."""
    try:
        rows = (store.supabase.table("users").select("status,must_change_password")
                .eq("user_id", DEFAULT_ADMIN_ID).execute().data)
    except Exception:
        return False
    return bool(rows) and rows[0]["status"] == "active" and bool(rows[0].get("must_change_password"))

# ---- installed state and first-run setup ----
REQUIRED_TABLES = ("approvals", "audit", "events", "jobs", "llm_calls", "notices", "rag_events",
                   "runs", "sessions", "settings", "sre_runbooks", "users", "verifications")
_state = {"installed": False, "checked": 0.0, "last": True, "code": None}


def is_installed() -> bool:
    """True once setup is complete. A deployment that predates /setup (it has an active admin) counts as installed."""
    if _state["installed"]:
        return True
    now = time.time()
    if now - _state["checked"] < 5:
        return _state["last"]
    _state["checked"] = now
    try:
        db = store.supabase
        flag = db.table("settings").select("value_json").eq("key", "installed").execute().data
        if flag and flag[0]["value_json"] is True:
            _state["installed"] = True
        elif db.table("users").select("user_id").in_("role", ["admin", "super_admin"]).eq("status", "active").limit(1).execute().data:
            mark_installed("existing-install")
        _state["last"] = _state["installed"]
    except Exception as e:
        log.warning("setup check could not read the database: %s", str(e)[:150])
        _state["last"] = True            # a database problem must not trap everyone in /setup
    return _state["last"]


def mark_installed(by: str) -> None:
    store.supabase.table("settings").upsert({"key": "installed", "value_json": True, "updated_by": by,
                                             "updated_at": time.time()}).execute()
    _state["installed"] = True


def setup_code() -> str:
    if not _state["code"]:
        _state["code"] = secrets.token_urlsafe(9)
    return _state["code"]


def check_setup_code(given: str) -> bool:
    return secrets.compare_digest((given or "").strip(), setup_code())


def announce_setup_if_needed() -> None:
    if not is_installed():
        log.warning("SETUP REQUIRED: open /setup and enter this setup code: %s", setup_code())


def database_check() -> dict:
    missing = []
    for t in REQUIRED_TABLES:
        try:
            store.supabase.table(t).select("*").limit(1).execute()
        except Exception:
            missing.append(t)
    return {"ok": not missing, "missing": missing, "checked": len(REQUIRED_TABLES)}


def complete_setup() -> dict:
    """Re-reads the database itself (never trusts cached state), seeds the default admin, closes /setup."""
    flag = store.supabase.table("settings").select("value_json").eq("key", "installed").execute().data
    if flag and flag[0]["value_json"] is True:
        raise ValueError("Setup is already complete.")
    chk = database_check()
    if not chk["ok"]:
        raise ValueError("These tables are missing: " + ", ".join(chk["missing"]))
    created = seed_default_admin()
    store.audit(None, None, "system", "setup_completed", f"default admin created={created}")
    mark_installed("setup")
    out = {"ok": True, "created": created, "username": DEFAULT_ADMIN_ID if created else None}
    if created:
        out["default_password"] = DEFAULT_ADMIN_PASSWORD      # shown once, to whoever holds the setup code
    return out