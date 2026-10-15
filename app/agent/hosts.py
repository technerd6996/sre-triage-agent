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