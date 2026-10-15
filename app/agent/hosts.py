"""Decides which server a ticket is about. The code decides, never the model."""
from app.tools import aws


def resolve_server(ticket_text: str):
    """Returns (instance, reason). Also sets the active server so the infra tools use its IP."""
    aws.ACTIVE.clear()  # never carry a previous ticket's server over
    try:
        inst = aws.find_in_text(ticket_text)
    except Exception as e:
        return None, f"AWS lookup failed: {str(e)[:150]}."
    if not inst:
        return None, "No known instance is named in the ticket."
    aws.ACTIVE[inst["platform"]] = inst["instance_id"]
    return inst, ""


def resolve_default_windows():
    """Fallback for AD user tickets that do not name a specific server: pick the running Windows instance."""
    try:
        win = [i for i in aws.list_instances() if i["platform"] == "windows" and i["state"] == "running"]
        if len(win) == 1:
            inst = win[0]
            aws.ACTIVE["windows"] = inst["instance_id"]
            return inst
    except Exception:
        pass
    return None