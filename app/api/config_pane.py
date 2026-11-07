"""Configuration pane API: the sections the pane shows, and the Test button for each.
Forms come from the settings registry (app/config.py) joined with the active adapter's manifest."""
from __future__ import annotations

import os
import smtplib
import time

import requests
from fastapi import APIRouter, HTTPException, Request

from app import config, store
from app.ports import registry

router = APIRouter(prefix="/api/admin/config")

# id == the Setting.group it shows
SECTIONS = (
    {"id": "ticketing", "title": "Ticketing", "port": "ticketing", "testable": True, "panel": "connections"},
    {"id": "identity", "title": "Identity (AD)", "port": "identity", "testable": True, "panel": "connections"},
    {"id": "knowledge", "title": "Knowledge source", "port": None, "testable": True, "panel": "connections"},
    {"id": "notifications", "title": "Notifications", "port": None, "testable": True, "panel": "connections"},
    {"id": "credentials", "title": "Credentials (PAM)", "port": "credentials", "testable": False, "panel": "connections"},
    {"id": "model", "title": "Model runtime", "port": None, "testable": True, "panel": "behaviour"},
    {"id": "agent", "title": "Agent behaviour", "port": None, "testable": False, "panel": "behaviour"},
    {"id": "infra", "title": "Servers and thresholds", "port": None, "testable": False, "panel": "behaviour"},
    {"id": "limits", "title": "Limits and timings", "port": None, "testable": False, "panel": "behaviour"},
    {"id": "security", "title": "Security and access", "port": None, "testable": False, "panel": "behaviour"},
    {"id": "policy", "title": "Safety policy (approvals)", "port": None, "testable": False, "panel": "behaviour"},
    {"id": "prompts", "title": "Prompts and messages", "port": None, "testable": False, "panel": "behaviour"},
)
_DEFAULT_PROVIDER = {"ticketing": "servicenow"}
_LABEL = {s.env: s.label for s in config.SETTINGS.values() if s.env}


def _provider_info(port: str | None):
    if not port:
        return None
    active = registry.active_provider(port, _DEFAULT_PROVIDER.get(port, ""))
    manifests = registry.available(port)
    return {
        "active": active,
        "providers": [{"id": m.provider, "title": m.title, "capabilities": sorted(m.capabilities)}
                      for m in manifests],
        "required": sorted(f"{port}.{f.key}" for m in manifests if m.provider == active
                           for f in m.fields if f.required),
    }


def _filled(item: dict) -> bool:
    if item["kind"] == "secret":
        return bool(item.get("is_set")) or item.get("source") == "env"
    return item.get("value") not in (None, "")

def _pending_restart(key: str) -> bool:
    """True when a saved change has not reached the running app yet."""
    s = config.SETTINGS[key]
    if s.live or not s.env:
        return False
    running = os.environ.get(s.env, "")
    try:
        if s.secret:
            return config.is_set(key) and config.get_secret(key) != running
        value, source = config.current(key)
    except Exception:
        return False
    if source == "saved":
        return str(value) != running
    return source == "default" and bool(running) and str(value) != running   # a reset not yet applied

_BOOT = (("DATABASE_URL", "Database connection", "addr"),
         ("TRIAGE_SECRET_KEY", "Encryption key", "secret"))


def _bootstrap_items() -> list[dict]:
    """What the running app loaded from the .env. Never includes a credential."""
    from urllib.parse import urlparse
    out = []
    for env, label, kind in _BOOT:
        v = os.getenv(env, "").strip()
        if not v:
            shown = "Not set"
        elif kind == "secret":
            shown = "Set"
        else:
            p = urlparse(v)
            shown = (f"{p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else "") + (p.path or "")
                     if p.hostname else "Set (format not recognised)")
        out.append({"env": env, "label": label, "shown": shown, "set": bool(v)})
    return out

@router.get("/sections")
def sections():
    out = []
    for s in SECTIONS:
        info = _provider_info(s["port"])
        required = set(info["required"]) if info else set()
        fields = config.describe(s["id"])
        for f in fields:
            f["required"] = f["key"] in required
            f["pending_restart"] = _pending_restart(f["key"])
        out.append({"id": s["id"], "title": s["title"], "testable": s["testable"], "panel": s["panel"],
                    "provider": info, "fields": fields,
                    "missing": [f["label"] for f in fields if f["required"] and not _filled(f)]})
    return {"sections": out, "secrets_ready": config.secrets_ready(), "bootstrap": _bootstrap_items()}


# ---- Test buttons. They use the values the app is running on, like /api/ready does. ----
def _names(env_names) -> str:
    return ", ".join(_LABEL.get(n, n) for n in env_names)


def _t_ticketing() -> str:
    registry.get_ticketing().ping()
    msg = "agent account signed in"
    if os.getenv("SNOW_CHECKER_USER"):
        registry.get_ticketing(role="reviewer").ping()
        msg += ", checker account signed in"
    return msg


def _t_model() -> str:
    url = os.getenv("COLAB_API_URL", "").strip()
    if not url:
        raise RuntimeError("Model API URL is not set")
        
    # Use the same split logic we applied to the other health checks
    base = url.split("/v1")[0] if "/v1" in url else url.rsplit("/", 1)[0]
    
    r = requests.get(f"{base}/health", timeout=4)
    if r.status_code != 200:
        raise RuntimeError(f"Model server answered HTTP {r.status_code}")
    return "model server online"


def _t_knowledge() -> str:
    uri = os.getenv("NEO4J_URI", "").strip()
    if not uri:
        raise RuntimeError("Neo4j URI is not set")
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(uri, auth=(os.getenv("NEO4J_USER", ""), os.getenv("NEO4J_PASS", "")),
                               connection_timeout=5)
    try:
        drv.verify_connectivity()
    finally:
        drv.close()
    return "Neo4j reachable"


def _t_notifications() -> str:
    return registry.get_notifications().check()          # blank transport means smtp; no message is sent


def _t_bootstrap() -> str:
    store.recent_audit(1)                                   # same check /api/ready uses
    if not config.secrets_ready():
        return "database reachable. Encryption key is not set, so secrets cannot be stored"
    config._fernet()                                        # raises if the key is malformed
    return "database reachable, encryption key valid"

def _t_identity() -> str:
    ident = registry.get_identity()
    # Search for a common keyword to verify the bind and search base are valid
    groups = ident.search_groups("Admin")
    return f"AD bound successfully. Search test returned {len(groups)} groups."


_TESTS = {"ticketing": _t_ticketing, "model": _t_model, "knowledge": _t_knowledge,
          "notifications": _t_notifications, "bootstrap": _t_bootstrap, "identity": _t_identity}


def _run_test(section: str) -> dict:
    fn = _TESTS.get(section)
    if not fn:
        raise HTTPException(404, "This section has no test")
    t = time.time()
    try:
        ok, detail = True, fn()
    except Exception as e:                                   # noqa: BLE001
        ok, detail = False, (str(e) or type(e).__name__)[:200]
    return {"ok": ok, "detail": detail, "ms": int((time.time() - t) * 1000)}


@router.post("/test/{section}")
def test_section(section: str, request: Request):
    res = _run_test(section)
    try:
        store.audit(None, None, getattr(request.state, "user", "?"), "config_tested",
                    f"{section}: {'ok' if res['ok'] else 'failed'}")
    except Exception:
        pass
    return res