"""One-time import of an existing .env: classify every line, preview it, then save.
Nothing here returns or logs a secret value."""
import io

from dotenv import dotenv_values

from app import config

BOOTSTRAP = {"DATABASE_URL", "TRIAGE_SECRET_KEY"}      # must stay outside the database
HOST = {"WIN_HOST", "WIN_USER", "WIN_PASSWORD", "WIN_WINRM_TRANSPORT", "LINUX_USER", "LINUX_KEY_PATH"}  # PAM, P3
RETIRED = {"TRIAGE_USERS", "CRITICAL_SERVICES"}
_BY_ENV = {s.env: s for s in config.SETTINGS.values() if s.env}


class ImportBlocked(Exception):
    pass


def parse(text: str) -> dict:
    """Name -> value. Handles export, quotes, comments; lines with an empty value are treated as not set."""
    raw = dotenv_values(stream=io.StringIO(text), interpolate=False)
    return {k: v for k, v in raw.items() if k and v is not None and v.strip() != ""}


def classify(name: str) -> str:
    if name in BOOTSTRAP:
        return "bootstrap"
    if name in HOST:
        return "host"
    if name in RETIRED or name.startswith("WF_"):
        return "retired"
    s = _BY_ENV.get(name)
    if s:
        return "secret" if s.secret else "setting"
    return "unmanaged"


def preview(text: str) -> dict:
    ready, items, keep = config.secrets_ready(), [], []
    for name, value in parse(text).items():
        cat = classify(name)
        row = {"name": name, "category": cat}
        if cat in ("bootstrap", "host", "unmanaged"):
            keep.append(name)
        s = _BY_ENV.get(name)
        if s and cat in ("setting", "secret"):
            row.update(key=s.key, label=s.label, restart=not s.live)
            try:
                clean = config.validate(s.key, value)
            except ValueError as e:
                row.update(status="invalid", error=str(e))
            else:
                if s.secret:
                    row.update(status="replace" if config.is_set(s.key) else "new", masked=True, blocked=not ready)
                else:
                    cur, src = config.current(s.key)
                    status = "new" if src != "saved" else ("same" if cur == clean else "differs")
                    row.update(status=status, value=clean)
        items.append(row)
    counts = {}
    for i in items:
        counts[i["category"]] = counts.get(i["category"], 0) + 1
    return {"items": items, "counts": counts, "secrets_ready": ready, "keep_in_env": keep}


def apply(text: str, by: str) -> dict:
    from app import store
    p = preview(text)
    bad = [{"name": i["name"], "error": i["error"]} for i in p["items"] if i.get("status") == "invalid"]
    if bad:
        return {"ok": False, "errors": bad}                    # nothing is saved unless every line is valid
    if any(i["category"] == "secret" for i in p["items"]) and not p["secrets_ready"]:
        raise ImportBlocked("This file contains secrets but TRIAGE_SECRET_KEY is not set. "
                            "Generate a key, add it to the environment, restart, and import again.")
    values, saved, same, restart = parse(text), 0, 0, []
    for i in p["items"]:
        if i["category"] not in ("setting", "secret"):
            continue
        if i["status"] == "same":
            same += 1
            continue
        config.save(i["key"], values[i["name"]], by)           # audited per key (secrets: key name only)
        saved += 1
        if not config.SETTINGS[i["key"]].live:
            restart.append(i["key"])
    try:
        store.audit(None, None, by, "env_imported", f"{saved} saved, {same} unchanged")
    except Exception:
        pass
    return {"ok": True, "saved": saved, "unchanged": same, "restart_needed": restart, "keep_in_env": p["keep_in_env"]}