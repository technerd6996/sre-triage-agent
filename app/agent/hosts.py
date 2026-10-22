"""Decides which server a ticket is about. The code decides, never the model."""
from app.ports import registry
from app.tools import active


def _legacy(h) -> dict:
    """Same dict shape the agent used with the AWS code, so callers need no change yet."""
    return {"instance_id": h.id, "name": h.name, "platform": h.os, "state": h.state,
            "public_ip": h.address, "private_ip": h.private_address, "provider": h.provider}


def resolve_server(ticket_text: str):
    """Returns (instance, reason). Also sets the active server so the infra tools use its address."""
    active.bind({})  # never carry a previous ticket's server over
    try:
        host = registry.get_hosts().find_in_text(ticket_text)
    except Exception as e:
        return None, f"Host lookup failed: {str(e)[:150]}."
    if not host:
        return None, "No known instance is named in the ticket."
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