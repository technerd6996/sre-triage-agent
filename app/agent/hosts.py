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


def _unmanaged(hosts, ticket_text: str, ci_name: str = "") -> str:
    """'Server X is not managed by TRIAGE-1 App: ...' when the ticket names a real server that lacks the
    managed-server tag in the CMDB; '' otherwise (or when the provider cannot tell)."""
    find = getattr(hosts, "find_unmanaged_in_text", None)
    if not find:
        return ""
    try:
        h = find(ticket_text) or (find(ci_name) if ci_name else None)
    except Exception:                       # noqa: BLE001 - only improves the message
        return ""
    if not h:
        return ""
    from app.tools import cmdb_tags
    return cmdb_tags.not_managed_message(h.name or h.id) + "."


def is_not_managed(why: str) -> bool:
    from app.tools import cmdb_tags
    return cmdb_tags.NOT_MANAGED in (why or "")


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
        unmanaged = _unmanaged(hosts, ticket_text, ci_name)
        if unmanaged:
            return None, unmanaged
        return None, _not_found(ticket_text)
    active.bind({host.os: host.id})
    return _legacy(host), ""


_PLATFORM_WORDS = {
    "windows": ("windows", "win32", "powershell"),
    "linux": ("linux", "ubuntu", "debian", "rhel", "red hat", "centos", "amazon linux", "systemd", "systemctl"),
}


def _platform_from_text(text: str) -> str:
    """'windows' or 'linux' when the ticket names exactly one of them as a whole word, else ''."""
    low = (text or "").lower()
    hits = {p for p, words in _PLATFORM_WORDS.items()
            if any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low) for w in words)}
    return hits.pop() if len(hits) == 1 else ""


def _platform_from_cmdb(text: str, ci_name: str = "") -> tuple:
    """(os, CI name) from the ServiceNow CMDB record of the server the ticket names, or ('', '')."""
    try:
        from app.adapters.cmdbhosts.hosts import CmdbHosts
        cmdb = CmdbHosts(managed_only=False)           # a hint only: the OS of any CI, tagged or not
        h = (cmdb.find_by_name(ci_name) if ci_name else None) or cmdb.find_in_text(text)
    except Exception:                       # noqa: BLE001 - only a hint: no CMDB means no hint
        return "", ""
    return (h.os, h.name) if h and h.os else ("", "")


def guess_platform(ticket_text: str, ci_name: str = "") -> tuple:
    """When the server itself cannot be found: (platform, where it came from), so the playbook is for the right OS.
    The CMDB record wins over words in the ticket. Only picks a playbook: never selects or touches a server."""
    os_name, ci = _platform_from_cmdb(ticket_text, ci_name)
    if os_name:
        return os_name, f"from the CMDB record {ci}"
    os_name = _platform_from_text(ticket_text)
    return (os_name, "from the ticket text") if os_name else ("", "")


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