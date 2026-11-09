"""Decides which server a ticket is about. The code decides, never the model."""
import re

from app.ports import registry
from app.tools import active


def _legacy(h) -> dict:
    """Same dict shape the agent used with the AWS code, so callers need no change yet."""
    return {"instance_id": h.id, "name": h.name, "platform": h.os, "state": h.state,
            "public_ip": h.address, "private_ip": h.private_address, "provider": h.provider}


def _key(s: str) -> frozenset:
    return frozenset(t for t in re.split(r"[-_.\s]+", (s or "").lower()) if t)


def _not_found(text: str) -> str:
    """'No known instance...' plus, when possible, the closest real name. Never auto-picks a server."""
    msg = "No known instance is named in the ticket."
    try:
        names = [h.name for h in registry.get_hosts().list_hosts() if h.name]
    except Exception:
        return msg
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}", text or "")
    close = []
    for n in names:
        for w in words:
            same_parts = _key(w) == _key(n) and len(_key(n)) > 1          # e.g. win-term-srv vs term-win-srv
            if same_parts or (len(w) >= 4 and (w.lower() in n.lower() or n.lower() in w.lower())):
                close.append(n)
                break
    if close:
        msg += " Did you mean " + " or ".join(close[:3]) + "?"
    if names:
        msg += " Known servers: " + ", ".join(names[:15]) + "."
    return msg


def resolve_server(ticket_text: str, ci_name: str = ""):
    """Returns (instance, reason). Also sets the active server so the infra tools use its address."""
    active.bind({})  # never carry a previous ticket's server over
    try:
        hosts = registry.get_hosts()
        # A server named in the message wins over the one the chat remembered ("cpu on zabbix-mon" must not run on the
        # server asked about a minute ago). The remembered name is the fallback when the message names none, or several.
        host = hosts.find_in_text(ticket_text)
        if not host and ci_name:
            host = hosts.find_by_name(ci_name) if hasattr(hosts, "find_by_name") else None
            host = host or hosts.find_in_text(ci_name)         # providers without find_by_name: match the name as text
    except Exception as e:
        return None, f"Host lookup failed: {str(e)[:150]}."
    if not host:
        return None, _not_found(ticket_text)
    active.bind({host.os: host.id})
    return _legacy(host), ""


def resolve_default_windows():
    """Fallback for AD user tickets that do not name a specific server: pick the running Windows instance."""
    try:
        win = [h for h in registry.get_hosts().list_hosts() if h.os == "windows" and h.state == "running"]
        if len(win) == 1:
            active.bind({**active.snapshot(), "windows": win[0].id})
            return _legacy(win[0])
    except Exception:
        pass
    return None