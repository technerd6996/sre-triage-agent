"""Who to tell about a server: owner email addresses read from the ServiceNow CMDB, and the mail itself.

The server CI is read for its owner fields. A reference to a user or a group gives that record's email; a plain text field
(default u_owner_email, for a shared mailbox) can hold one or more addresses. Field names are admin settings, so a different
CMDB layout needs no code change. Nothing here raises: a lookup that fails returns an empty list with the reason."""
import logging
import os
import re

from app import config
from app.ports import registry

log = logging.getLogger("triage.owners")

config.register("owners.email_field", "CMDB text field holding owner email addresses", "u_owner_email", "text", "notifications",
                help="A text field on the server CI with one or more addresses (a shared mailbox), separated by comma, semicolon or space. "
                     "Leave empty if you only use the standard owner fields.")
config.register("owners.user_fields", "CMDB user fields whose email is used", "owned_by,managed_by", "text", "notifications",
                help="Reference-to-user fields on the server CI. Each user's Email on the user record is mailed.")
config.register("owners.group_fields", "CMDB group fields whose email is used", "support_group", "text", "notifications",
                help="Reference-to-group fields on the server CI. The group's Email on the group record is mailed.")

config.register("owners.mail_transport", "Mail transport for owner approval and owner notices", "same", "select", "notifications",
                options=("same", "smtp", "ses"),
                help="'same' uses the Notice transport above. Choose smtp or ses to mail owners WITHOUT changing how P1 alerts are sent "
                     "(SNS cannot address individual owners, so it cannot be used for owner mail).")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _request(*a, **kw):
    from app.adapters.servicenow.client import _request as snow_request     # lazy: keeps this module cheap to import
    return snow_request(*a, **kw)


def _fields(setting: str) -> list:
    return [f.strip() for f in str(config.get(setting) or "").split(",") if f.strip() and re.fullmatch(r"[A-Za-z0-9_]+", f.strip())]


def emails_in(text: str) -> list:
    return list(dict.fromkeys(m.lower() for m in _EMAIL.findall(text or "")))


def _ref(v) -> str:
    return (v.get("value") if isinstance(v, dict) else v) or ""


def contacts(host_name: str) -> dict:
    """{'emails': [...], 'who': ['Ann (owned_by)', ...], 'error': ''} for the server CI. Never raises."""
    out = {"emails": [], "who": [], "error": ""}
    try:
        user_f, group_f, text_f = _fields("owners.user_fields"), _fields("owners.group_fields"), _fields("owners.email_field")
        wanted = user_f + group_f + text_f
        if not wanted:
            out["error"] = "no owner fields are configured (Admin > Notifications)"
            return out
        name = (host_name or "").replace("^", " ")
        merged = {}
        # a field defined on a subclass is not returned by the parent table, so read the server table and cmdb_ci
        for table in dict.fromkeys((os.getenv("CMDB_HOST_TABLE", "cmdb_ci_server"), "cmdb_ci")):
            rows = _request("GET", table, params={"sysparm_query": f"name={name}", "sysparm_limit": 1,
                                                  "sysparm_fields": ",".join(["sys_id"] + wanted),
                                                  "sysparm_display_value": "false"})
            for k, v in (rows[0] if rows else {}).items():
                if v not in ("", None, {}) and not merged.get(k):
                    merged[k] = v
        if not merged:
            out["error"] = f"no CMDB record named {host_name}"
            return out
        found = []                                                # (email, who)
        for f in text_f:
            for e in emails_in(str(merged.get(f) or "")):
                found.append((e, f"{f}"))
        for table, fields in (("sys_user", user_f), ("sys_user_group", group_f)):
            ids = {f: _ref(merged.get(f)) for f in fields if _ref(merged.get(f))}
            if not ids:
                continue
            rows = _request("GET", table, params={"sysparm_query": "sys_idIN" + ",".join(ids.values()),
                                                  "sysparm_fields": "sys_id,email,name", "sysparm_limit": 20})
            by_id = {r["sys_id"]: r for r in rows}
            for f, sid in ids.items():
                r = by_id.get(sid)
                for e in emails_in((r or {}).get("email", "")):
                    found.append((e, f"{(r or {}).get('name') or f} ({f})"))
        seen = set()
        for e, who in found:
            if e not in seen:
                seen.add(e)
                out["emails"].append(e)
                out["who"].append(f"{e} - {who}")
        if not out["emails"]:
            out["error"] = (f"{host_name} has no owner email in the CMDB (checked: {', '.join(wanted)}). "
                            "Add an email to the owner's user record or fill in the owner email field.")
    except Exception as e:                                         # noqa: BLE001  a CMDB problem must not break the action
        out["error"] = f"owner lookup failed: {str(e)[:150]}"
    return out


def _notifier():
    """The mail sender for owner mail: the dedicated owner transport when one is chosen, else the main Notice transport."""
    choice = str(config.get("owners.mail_transport") or "same").strip().lower()
    return registry.get("notifications", choice) if choice in ("smtp", "ses") else registry.get_notifications()


def can_address() -> tuple:
    """(True, '') when the active mail transport can send to named people; else (False, why not). Never raises.
    SNS publishes to one fixed topic and ignores To and Cc, so the CMDB owners would never receive the message."""
    try:
        from app.ports.notifications import CAP_RECIPIENTS
        n = _notifier()
        if CAP_RECIPIENTS not in n.capabilities:
            return False, (f"the mail transport is {n.provider.upper()}, which sends to a fixed list and cannot address the owners; "
                           "set 'Mail transport for owner approval' to smtp or ses (Admin > Notifications)")
        return True, ""
    except Exception as e:                                         # noqa: BLE001
        return False, f"mail is not set up: {str(e)[:120]}"


def send(to: list, cc: list, subject: str, body: str) -> tuple:
    """(True, message id) or (False, reason). Never raises, and never pretends: a transport that cannot address people is a failure."""
    ok, why = can_address()
    if not ok:
        return False, why
    try:
        return True, _notifier().send(to, cc, subject, body)
    except Exception as e:                                         # noqa: BLE001
        return False, str(e)[:200]
