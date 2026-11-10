"""Adapter registry. Adapters are discovered from app/adapters/* at startup."""
from __future__ import annotations

import importlib
import os
import pkgutil
from typing import Callable

from app.ports.errors import Unsupported
from app.ports.manifest import Manifest

_ADAPTERS: dict[tuple[str, str], tuple[Manifest, Callable]] = {}
_DISCOVERED = False


def register(manifest: Manifest, factory: Callable) -> None:
    _ADAPTERS[(manifest.port, manifest.provider)] = (manifest, factory)


def discover() -> None:
    """Import every package under app/adapters so each can call register()."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    import app.adapters as pkg
    for m in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"app.adapters.{m.name}")
    _DISCOVERED = True


def available(port: str) -> list[Manifest]:
    discover()
    return [m for (p, _), (m, _) in _ADAPTERS.items() if p == port]


def get(port: str, provider: str, **kw):
    discover()
    try:
        factory = _ADAPTERS[(port, provider)][1]
    except KeyError:
        raise Unsupported(f"No '{provider}' adapter installed for port '{port}'")
    return factory(**kw)


# Until the config layer exists (P1) the active provider comes from the environment.
def active_provider(port: str, default: str) -> str:
    if port == "ticketing":                 # the licence key names the one ticketing tool this install may use
        try:
            from app import licence
            tool = licence.current().get("itsm")
        except Exception:                   # noqa: BLE001
            tool = ""
        if tool:
            return tool
    if port == "credentials":
        try:
            from app import config
            chosen = config.get("credentials.provider")
        except Exception:                   # noqa: BLE001
            chosen = ""
        if chosen:
            return chosen
    return os.getenv(f"{port.upper()}_PROVIDER", default)


def is_servicenow() -> bool:
    """False when the install's ticketing tool is not ServiceNow: its knowledge base, CMDB and owner lookups are then off."""
    return active_provider("ticketing", "servicenow") == "servicenow"


def get_ticketing(role: str = "agent"):
    """role="reviewer" returns an adapter that acts as the independent checker identity."""
    return get("ticketing", active_provider("ticketing", "servicenow"), role=role)

def get_active(port: str, default: str, **kw):
    return get(port, active_provider(port, default), **kw)


def get_hosts():
    return get_active("hosts", "aws")


def get_context():
    return get_active("context", "servicenow")


def get_credentials():
    return get_active("credentials", "awssm")


def get_identity():
    return get_active("identity", "ad")

def get_notifications():
    """The transport comes from the Notice transport setting (NOTICE_TRANSPORT); blank means smtp."""
    return get("notifications", (os.getenv("NOTICE_TRANSPORT") or "smtp").strip().lower())