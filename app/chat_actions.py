"""Changes an approver asks for in the chat ("Can we restart the spooler service?", "unlock jsmith").

Two steps, so the browser can never change what runs:
  propose()  reads the message, works out ONE closed action (a tool from the admin verb table, its arguments, a host),
             and returns a card to show. Nothing is changed, no credential is fetched.
  redeem()   exchanges the card's single-use token for that exact action, once, for the same person, before it expires.
The model is not involved: the phrase-to-tool table and the service-name aliases are admin settings.

Adding an action: write an entry in ACTIONS below (how to read its arguments, what to say) and a line in the
'approver_chat.verbs' setting. The ticket, PAM, host lock and audit steps in api/approver_chat.py do not change."""
import re
import secrets
import threading
import time

from app import config
from app.agent import policy

_DEFAULT_VERBS = ("restart = restart_service\nstart = start_service\n"
                  "clear temp = clear_temp_files\nclean temp = clear_temp_files\nfree up = clear_temp_files\n"
                  "clean old logs = clean_old_logs\nclean logs = clean_old_logs\n"
                  "unlock = unlock_ad_account")
_DEFAULT_ALIASES = ("print spooler = Spooler\nspooler = Spooler\nwindows update = wuauserv\niis = W3SVC\n"
                    "remote desktop = TermService\nrdp = TermService\ndns = DNS\ndhcp = DHCPServer")


def _check_table(text: str) -> None:
    _parse(text)


def _parse(text: str) -> dict:
    out = {}
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"line {n}: write it as  phrase = value")
        k, v = (x.strip() for x in line.split("=", 1))
        if not k or not v:
            raise ValueError(f"line {n}: both sides of '=' are needed")
        out[k.lower()] = v
    return out


config.register("approver_chat.verbs", "Chat actions (phrase = tool, one per line)", _DEFAULT_VERBS, "text", "agent",
                multiline=True, max_len=2000, check=_check_table,
                help="What an approver can ask for in the chat, for example 'restart = restart_service'. The tool must be one "
                     "the chat knows how to read arguments for (restart_service, start_service, clear_temp_files, "
                     "clean_old_logs, unlock_ad_account). The tool's risk level still comes from the safety policy table.")
config.register("approver_chat.service_aliases", "Service names (what people say = real service name)", _DEFAULT_ALIASES,
                "text", "agent", multiline=True, max_len=4000, check=_check_table,
                help="Lets 'print spooler' mean the Windows service 'Spooler'. A single word that is not listed is used as typed.")
config.register("approver_chat.action_token_minutes", "Minutes a chat action card stays valid", 5, "int", "agent",
                lo=1, hi=60, help="After this the engineer has to ask again. Each card works once.")

_PENDING: dict = {}  # token -> pending action
_LOCK = threading.Lock()
_SERVER_HINT = re.compile(r"\s*\(server:[^)]*\)\s*$", re.I)
_FILLER = {"the", "ad", "active", "directory", "account", "user", "for", "of", "please", "a", "an", "on", "in", "at"}


class AskBack(Exception):
    """The message is not enough to act on. The text is shown to the engineer as the next question."""


_TICKET = re.compile(r"\bINC\d{5,}\b", re.I)


def close_intent(message: str) -> bool:
    """'close INC0010689', 'INC0010689 - close the incident', 'resolve INC...'."""
    return bool(re.search(r"\b(close|resolve)\b", message or "", re.I) and _TICKET.search(message or ""))


def ticket_in(message: str) -> str:
    m = _TICKET.search(message or "")
    return m.group(0).upper() if m else ""


def _new_token(entry: dict) -> str:
    token = secrets.token_urlsafe(24)
    with _LOCK:
        now = time.time()
        for k in [k for k, v in _PENDING.items() if v["expires"] < now]:
            _PENDING.pop(k, None)
        _PENDING[token] = entry
    return token


def _ttl() -> int:
    return int(config.get("approver_chat.action_token_minutes") or 5) * 60


def propose_close(user: str, number: str, notes: str) -> dict:
    """Card to resolve one ticket with these close notes. Same single-use token rules as an action card."""
    entry = {"kind": "close", "user": user, "ticket": number, "notes": notes[:4000], "expires": time.time() + _ttl()}
    return {"kind": "close", "token": _new_token(entry), "ticket": number, "notes": notes[:4000], "level": 0,
            "expires_in": _ttl()}


# ---------------------------------------------------------------- reading arguments from the message
def _after_verb(text: str, verb: str) -> str:
    t = _SERVER_HINT.sub("", text.strip()).strip().rstrip("?.! ")
    m = re.search(rf"\b{re.escape(verb)}\b\s*(.*)$", t, re.I)
    return m.group(1).strip() if m else ""


def _service_from(text: str, verb: str) -> str:
    """'restart the print spooler service on x' -> 'print spooler'."""
    rest = re.sub(r"^the\s+", "", _after_verb(text, verb), flags=re.I)
    name = re.split(r"\s+(?:on|in|at|for)\s+", rest, maxsplit=1, flags=re.I)[0]
    return re.sub(r"\s+service$", "", name.strip(), flags=re.I).strip()


def _service_args(message: str, verb: str, inst: dict) -> dict:
    said = _service_from(message, verb)
    if not said:
        raise AskBack(f"Which service do you want to {verb}?")
    aliases = _parse(config.get("approver_chat.service_aliases"))
    service = aliases.get(said.lower()) or (said if re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", said) else "")
    if not service:
        raise AskBack(f"I do not know a service called '{said}'. Add it under Agent behaviour, Service names, "
                      "or say the exact service name.")
    return {"args": {"target": inst["platform"], "service_name": service}, "name": service,
            "title": f"{verb.capitalize()} the service `{service}` on {inst['name']}?", "label": f"{verb} {service} on {inst['name']}"}


def _temp_args(message: str, verb: str, inst: dict) -> dict:
    return {"args": {"target": inst["platform"]}, "name": "temp files",
            "title": f"Delete temp files older than 1 day on {inst['name']} to free up space?",
            "label": f"clear temp files on {inst['name']}"}


def _logs_args(message: str, verb: str, inst: dict) -> dict:
    from app.tools import infra_perf
    text = _SERVER_HINT.sub("", message)
    m = re.search(r"[A-Za-z]:\\[^\s\"']+", text) if inst["platform"] == "windows" else re.search(r"(?<![\w.])/[A-Za-z0-9._/-]+", text)
    if not m:
        raise AskBack("Which folder? Give the full path, for example C:\\inetpub\\logs\\LogFiles (it must be one of the "
                      "folders allowed for log cleaning).")
    days_m = re.search(r"(\d{1,3})\s*(?:days?|d)\b", text, re.I)
    days = int(days_m.group(1)) if days_m else 14
    try:
        path = infra_perf._allowed_path(inst["platform"], m.group(0).rstrip(".,;"), "LOG_CLEAN_PATHS", "/var/log")
    except ValueError as e:
        raise AskBack(f"I cannot clean that folder: {e}")
    return {"args": {"target": inst["platform"], "path": path, "older_than_days": days}, "name": path,
            "title": f"Delete log files older than {days} days in `{path}` on {inst['name']}?",
            "label": f"clean logs in {path} (older than {days} days) on {inst['name']}"}


def _unlock_args(message: str, verb: str, inst: dict) -> dict:
    words = [w for w in re.split(r"\s+", _after_verb(message, verb)) if w and w.lower().strip("'s") not in _FILLER]
    user = words[0].rstrip(".,;'s") if words else ""
    if not user:
        raise AskBack("Which account do you want to unlock? Say the username, for example: unlock jsmith")
    if not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", user):
        raise AskBack(f"'{user}' does not look like a username.")
    return {"args": {"username": user}, "name": user,
            "title": f"Unlock the Active Directory account `{user}`? (runs through {inst['name']})",
            "label": f"unlock AD account {user} (via {inst['name']})"}


def _tool(name: str):
    from app.tools import infra, infra_perf
    return {"restart_service": infra_perf.restart_service, "start_service": infra.start_service,
            "clear_temp_files": infra.clear_temp_files, "clean_old_logs": infra_perf.clean_old_logs,
            "unlock_ad_account": infra.unlock_ad_account}.get(name)


# host: "named" = the server the engineer named (or the one this chat was just asking about);
#       "windows" = the one running Windows server, the same host AD tickets use (AD commands run through it)
ACTIONS = {
    "restart_service": {"host": "named", "args": _service_args},
    "start_service": {"host": "named", "args": _service_args},
    "clear_temp_files": {"host": "named", "args": _temp_args},
    "clean_old_logs": {"host": "named", "args": _logs_args},
    "unlock_ad_account": {"host": "windows", "args": _unlock_args},
}


def propose(message: str, user: str, resolve) -> dict:
    """The card for one action, or AskBack. `resolve` is a server dict, or a function host_mode -> server dict that
    raises AskBack when it cannot find one (the API supplies it, so servers are only looked up when needed)."""
    resolver = (lambda mode: resolve) if isinstance(resolve, dict) else resolve
    verbs = _parse(config.get("approver_chat.verbs"))
    low = message.lower()
    # longest phrase first, so 'clean old logs' wins over a shorter phrase that is part of it
    verb = next((v for v in sorted(verbs, key=len, reverse=True) if re.search(rf"\b{re.escape(v)}\b", low)), None)
    if not verb:
        raise AskBack("I can only do these from the chat: " + ", ".join(verbs) + ". Say which one and on which server.")
    tool_name = verbs[verb]
    spec = ACTIONS.get(tool_name)
    if spec is None or _tool(tool_name) is None:
        raise AskBack(f"'{verb}' is set up for {tool_name}, which the chat cannot run yet.")
    level = policy.risk_level(tool_name)
    if level >= 3:
        raise AskBack(f"{tool_name} is critical risk (L3) and is never run from the chat.")
    if level >= 2:
        raise AskBack(f"{tool_name} is high risk (L2) and needs the server owners' approval. Use the Agent Console for now.")

    inst = resolver(spec["host"])
    built = spec["args"](message, verb, inst)
    action = {"kind": "action", "user": user, "tool": tool_name, "verb": verb, "args": built["args"],
              "name": built["name"], "title": built["title"], "label": built["label"], "host": inst["name"],
              "host_mode": spec["host"], "level": level, "message": message[:300], "expires": time.time() + _ttl()}
    return {"kind": "action", "token": _new_token(action), "tool": tool_name, "verb": verb, "title": built["title"],
            "name": built["name"], "host": inst["name"], "host_mode": spec["host"], "level": level, "expires_in": _ttl()}


def redeem(token: str, user: str) -> dict:
    """The pending action for this token, removed so it works once. Raises ValueError with a message safe to show."""
    with _LOCK:
        action = _PENDING.pop(token or "", None)
    if not action:
        raise ValueError("That confirmation has already been used or is unknown. Ask again.")
    if action["expires"] < time.time():
        raise ValueError("That confirmation expired. Ask again.")
    if action["user"] != user:
        raise ValueError("That confirmation belongs to someone else.")
    return action


def tool_for(action: dict):
    return _tool(action["tool"])


def args_for(action: dict) -> dict:
    return dict(action["args"])


def summary(action: dict, result: dict) -> str:
    """One plain sentence about what happened."""
    host, name, tool = action["host"], action["name"], action["tool"]
    if result.get("error"):
        return f"Could not {action['label']}: {str(result['error'])[:200]}"
    if tool in ("restart_service", "start_service"):
        before, now = result.get("status_before"), result.get("status") or result.get("state")
        tail = f": {before} -> {now}" if before and now else (f": now {now}" if now else "")
        return f"{action['verb'].capitalize()}ed {name} on {host}{tail}."
    if tool == "clear_temp_files":
        got = (f"freed {result['freed_mb']} MB" if "freed_mb" in result
               else f"removed {result.get('files_deleted', 0)} files")
        return f"Cleared temp files on {host}: {got}."
    if tool == "clean_old_logs":
        got = (f"freed {result['freed_mb']} MB" if "freed_mb" in result
               else f"removed {result.get('files_deleted', 0)} files")
        used = (f" Disk used {result['used_pct_before']}% -> {result['used_pct_after']}%."
                if result.get("used_pct_before") is not None and result.get("used_pct_after") is not None else "")
        return f"Cleaned old logs in {name} on {host}: {got}.{used}"
    if tool == "unlock_ad_account":
        return (f"Account {name} is still locked." if result.get("LockedOut") else f"Unlocked the account {name}.")
    return f"Done: {action['label']}."
