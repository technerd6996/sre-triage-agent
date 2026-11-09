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
                  "unlock = unlock_ad_account\n"
                  "stop = stop_service\nenable = set_service_startup\ndisable = set_service_startup\n"
                  "log off = logoff_session\nlogoff = logoff_session\n"
                  "run the scheduled task = run_scheduled_job\nrun the scheduled job = run_scheduled_job\n"
                  "run the task = run_scheduled_job\nrun the job = run_scheduled_job\nrun task = run_scheduled_job\nrun job = run_scheduled_job\n"
                  "install = install_packages\nupgrade = install_packages\n"
                  "update packages = install_packages\nupdate package = install_packages\n"
                  "reboot = reboot_server\nshut down = shutdown_server\nshutdown = shutdown_server\npower off = shutdown_server\n"
                  "remove = remove_packages\nuninstall = remove_packages\n"
                  "install updates = install_windows_updates\ninstall update = install_windows_updates\n"
                  "install patches = install_windows_updates\ninstall patch = install_windows_updates")
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


def _alias(said: str) -> str:
    aliases = _parse(config.get("approver_chat.service_aliases"))
    service = aliases.get(said.lower()) or (said if re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", said) else "")
    if not service:
        raise AskBack(f"I do not know a service called '{said}'. Add it under Agent behaviour, Service names, "
                      "or say the exact service name.")
    return service


def _stop_args(message: str, verb: str, inst: dict) -> dict:
    from app.tools import chat_changes
    said = _service_from(message, verb)
    if not said:
        raise AskBack("Which service do you want to stop?")
    svc = _alias(said)
    if chat_changes.service_protected(svc):
        raise AskBack(f"'{svc}' is protected: stopping it could lock everyone out of the server, so the chat will not do it.")
    return {"args": {"target": inst["platform"], "service_name": svc}, "name": svc,
            "title": f"Stop the service `{svc}` on {inst['name']}?", "label": f"stop {svc} on {inst['name']}",
            "detail": ["Anything that depends on it stops working until it is started again."]}


def _startup_args(message: str, verb: str, inst: dict) -> dict:
    from app.tools import chat_changes
    said = _service_from(message, verb)
    if not said:
        raise AskBack(f"Which service do you want to {verb} at boot?")
    svc = _alias(said)
    if chat_changes.service_protected(svc):
        raise AskBack(f"'{svc}' is protected and its start-up setting is not changed from the chat.")
    on = verb == "enable"
    return {"args": {"target": inst["platform"], "service_name": svc, "mode": "enabled" if on else "disabled"}, "name": svc,
            "title": f"{'Enable' if on else 'Disable'} `{svc}` at boot on {inst['name']}?",
            "label": f"{verb} {svc} at boot on {inst['name']}",
            "detail": ["It keeps its current state now; only what happens at the next boot changes."]}


def _logoff_args(message: str, verb: str, inst: dict) -> dict:
    from app.tools import chat_changes
    m = re.search(r"\bsession\s*(?:id\s*)?#?([0-9]+|c[0-9]+)\b", message, re.I) or re.search(r"\bid\s*#?([0-9]{1,6})\b", message, re.I)
    if not m:
        raise AskBack(f"Which session? Ask 'who is logged in on {inst['name']}' for the IDs, then say 'log off session <id>'.")
    sid = m.group(1)
    try:
        who = chat_changes.find_session(inst["platform"], sid)
    except Exception as e:
        raise AskBack(f"I could not list the sessions on {inst['name']}: {str(e)[:120]}")
    if not who:
        raise AskBack(f"There is no session {sid} on {inst['name']} right now. Ask 'who is logged in on {inst['name']}' for the current list.")
    return {"args": {"target": inst["platform"], "session_id": sid}, "name": f"session {sid}",
            "title": f"Log off session {sid} ({who[1]}) on {inst['name']}?",
            "label": f"log off session {sid} ({who[1]}) on {inst['name']}",
            "detail": ["Unsaved work in that session is lost."]}


def _job_args(message: str, verb: str, inst: dict) -> dict:
    from app.tools import chat_changes
    rest = re.sub(r"^(the\s+)?(scheduled\s+)?(task|job)\s+", "", _after_verb(message, verb), flags=re.I)
    rest = re.sub(rf"\s+on\s+{re.escape(inst['name'])}$", "", rest, flags=re.I).strip().strip("\"'`")
    if not rest:
        raise AskBack(f"Which job? Ask 'list scheduled jobs on {inst['name']}' and use the name shown.")
    try:
        resolved = chat_changes.find_job(inst["platform"], rest)
    except ValueError as e:
        raise AskBack(str(e))
    return {"args": {"target": inst["platform"], "name": rest}, "name": resolved,
            "title": f"Run the scheduled job `{resolved}` now on {inst['name']}?",
            "label": f"run scheduled job {resolved} on {inst['name']}",
            "detail": ["The job starts and this chat does not wait for it to finish."]}


_PKG_FILLER = {"the", "package", "packages", "pkg", "pkgs", "please", "on", "at", "from", "for", "server", "with", "apt", "dnf", "yum",
               "to", "latest", "version", "and"}


def _install_args(message: str, verb: str, inst: dict) -> dict:
    from app import owner_contacts
    from app.tools import chat_changes
    mode = "install" if verb == "install" else "update"
    if inst["platform"] != "linux":
        raise AskBack("Installing packages from the chat is for Linux servers. Windows patches are a separate step that is not available yet.")
    rest = _SERVER_HINT.sub("", _after_verb(message, verb))
    names = [t for t in re.split(r"[\s,;]+", rest) if t and t.lower() not in _PKG_FILLER and t.lower() != inst["name"].lower()]
    try:
        names = chat_changes.validate_packages(names)
    except ValueError as e:
        raise AskBack(f"{e}. Say the package names, for example: {verb} nginx curl")
    try:
        plan = chat_changes.simulate_packages("linux", names, mode)
    except Exception as e:
        raise AskBack(f"I could not check what that would do: {str(e)[:150]}")
    if plan["error"]:
        raise AskBack(f"The package manager says: {plan['error'][:200]}")
    if plan["removes"]:
        raise AskBack("That would also remove: " + ", ".join(plan["removes"][:5]) + ". The chat will not do that.")
    if not plan["adds"]:
        raise AskBack(f"Nothing to do: {', '.join(names)} " + ("is already installed." if mode == "install" else "has no newer version.") +
                      f" ({plan['manager']})")
    oc = owner_contacts.contacts(inst["name"])
    can, why = owner_contacts.can_address()
    owners = ([("When it finishes, these owners are mailed: " + ", ".join(oc["emails"])) if can else
               f"Owner mail will NOT be sent: {why}. The change is still recorded on the ticket."] if oc["emails"]
              else [f"No owner email was found ({oc['error']}), so nobody will be mailed."])
    return {"args": {"target": "linux", "packages": names, "mode": mode}, "name": ", ".join(names),
            "title": f"{mode.capitalize()} {', '.join(names)} on {inst['name']} ({plan['manager']})?",
            "label": f"{mode} {', '.join(names)} on {inst['name']}",
            "detail": ["The dry run would change: " + "; ".join(plan["adds"][:8]) + (" ..." if len(plan["adds"]) > 8 else "")] + owners}


_MACHINE_WORDS = {"server", "the server", "machine", "the machine", "box", "the box", "vm", "the vm", "host", "the host",
                  "computer", "the computer", "pc", "it"}
_POWER_TOOLS = ("reboot_server", "shutdown_server")


def _means_machine(said: str, host: str) -> bool:
    """'the server', 'victim-test', 'server victim-test', 'victim-test server' all mean the whole machine, not a service."""
    s, h = said.lower().strip(), host.lower()
    if s in _MACHINE_WORDS or s == h:
        return True
    words = s.split()
    nouns = {"server", "machine", "box", "vm", "host", "computer", "pc"}
    return len(words) == 2 and ((words[0] in nouns and words[1] == h) or (words[1] in nouns and words[0] == h))


def _why(message: str) -> str:
    m = re.search(r"\b(?:because|reason|due to|since)\b\s*:?\s*(.+)$", message, re.I)
    return (m.group(1) if m else "requested in the chat").strip()[:80]


def _sessions_line(inst: dict) -> list:
    """A read-only look at who is logged on, so the engineer sees who a reboot would disconnect. Never fails the card."""
    try:
        from app.tools import chat_changes, chat_tools
        res = chat_tools.list_sessions.invoke({"target": inst["platform"]})
        who = sorted({u for _, u in chat_changes.parse_sessions(inst["platform"], res.get("lines", []))})
        return [f"{len(who)} user(s) are logged on now and will be disconnected: {', '.join(who)}"] if who else ["Nobody is logged on right now."]
    except Exception:                                              # noqa: BLE001
        return []


def _reboot_args(message: str, verb: str, inst: dict) -> dict:
    delay = int(config.get("chat.reboot_delay_seconds"))
    return {"args": {"target": inst["platform"], "delay_seconds": delay, "reason": _why(message)}, "name": inst["name"],
            "title": f"Restart the whole server {inst['name']} in {delay} seconds?",
            "label": f"restart server {inst['name']}",
            "detail": ["Everything on the server stops while it restarts."] + _sessions_line(inst)}


def _shutdown_args(message: str, verb: str, inst: dict) -> dict:
    delay = int(config.get("chat.reboot_delay_seconds"))
    return {"args": {"target": inst["platform"], "delay_seconds": delay, "reason": _why(message)}, "name": inst["name"],
            "title": f"Shut down the whole server {inst['name']} in {delay} seconds?",
            "label": f"shut down server {inst['name']}",
            "detail": ["It stays OFF until someone powers it on again (by hand or from the cloud console)."] + _sessions_line(inst)}


def _remove_args(message: str, verb: str, inst: dict) -> dict:
    from app.tools import chat_changes
    if inst["platform"] != "linux":
        raise AskBack("Removing packages from the chat is for Linux servers.")
    rest = _SERVER_HINT.sub("", _after_verb(message, verb))
    names = [t for t in re.split(r"[\s,;]+", rest) if t and t.lower() not in _PKG_FILLER and t.lower() != inst["name"].lower()]
    try:
        names = chat_changes.validate_packages(names)
        plan = chat_changes.simulate_removal("linux", names)
    except ValueError as e:
        raise AskBack(f"{e}. Say the package names, for example: remove telnet")
    except Exception as e:
        raise AskBack(f"I could not check what that would do: {str(e)[:150]}")
    if plan["error"]:
        raise AskBack(f"The package manager says: {plan['error'][:200]}")
    if plan["extra"]:
        raise AskBack("That would also remove: " + ", ".join(plan["extra"][:5]) + ". The chat will not do that.")
    if not plan["removes"]:
        raise AskBack(f"Nothing to do: {', '.join(names)} is not installed on {inst['name']}.")
    return {"args": {"target": "linux", "packages": names}, "name": ", ".join(names),
            "title": f"Remove {', '.join(names)} from {inst['name']} ({plan['manager']})?",
            "label": f"remove {', '.join(names)} from {inst['name']}",
            "detail": ["The dry run would remove: " + "; ".join(r for r in plan["removes"][:8])]}


def _winupdate_args(message: str, verb: str, inst: dict) -> dict:
    from app.tools import chat_changes, chat_tools
    if inst["platform"] != "windows":
        raise AskBack("Installing Windows updates is for Windows servers. For Linux packages, say: install <package names>.")
    kbs = re.findall(r"\bKB\d{6,8}\b", message, re.I)
    try:
        kbs = chat_changes.validate_kbs(kbs)
    except ValueError as e:
        raise AskBack(f"{e}. Ask 'pending windows updates on {inst['name']}' to see the numbers.")
    res = chat_tools.check_windows_updates.invoke({"target": "windows"})
    if res.get("error"):
        raise AskBack(f"I could not list the pending updates: {res['error'][:150]}")
    if res.get("search_error"):
        raise AskBack("Windows would not let me check which updates are pending, so I cannot confirm those numbers: "
                      + res["search_error"][:120])
    pending = {}
    for ln in res.get("lines", []):
        m = re.match(r"pending:\s*(\S+)\s*(.*)", ln)
        if m:
            for kb in re.findall(r"KB\d{6,8}", m.group(1)):
                pending[kb.upper()] = m.group(2)
    missing = [k for k in kbs if k not in pending]
    if missing:
        raise AskBack(f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not waiting to be installed on {inst['name']}.")
    return {"args": {"target": "windows", "kbs": kbs}, "name": ", ".join(kbs),
            "title": f"Install {', '.join(kbs)} on {inst['name']}?", "label": f"install windows update {', '.join(kbs)} on {inst['name']}",
            "detail": [f"{k}: {pending[k][:110]}" for k in kbs] +
                      ["It runs in the background as a one-time scheduled task; the result is written on the ticket.",
                       "No restart is done automatically. If one is needed, the result says so and you can ask for a reboot."]}


def _owner_card(action: dict, inst: dict, built: dict) -> dict:
    """The card for an action that needs the owners' email approval first: checks everything that can be checked now."""
    from app import owner_approval, owner_contacts
    if action["tool"] in _POWER_TOOLS:
        block = owner_approval.power_block(inst)
        if block:
            raise AskBack(block)
        last = owner_approval.recent_power_action(inst["name"])
        if last:
            raise AskBack(f"{inst['name']} was already restarted or shut down from the chat {int((time.time() - last) // 60)} minutes ago. "
                          f"The least gap is {config.get('chat.reboot_min_interval_minutes')} minutes (setting chat.reboot_min_interval_minutes).")
    can, why = owner_contacts.can_address()
    if not can:
        raise AskBack(f"This needs the owners' approval by email, but {why}.")
    oc = owner_contacts.contacts(inst["name"])
    if not oc["emails"]:
        raise AskBack(f"This needs the owners' approval, but I cannot find who to ask. {oc['error']}")
    action["owners"], action["stage"] = oc["emails"], "request"
    return {"kind": "owner_request", "token": _new_token(action), "tool": action["tool"], "title": built["title"],
            "name": action["name"], "host": inst["name"], "level": action["level"], "owners": oc["who"],
            "detail": built.get("detail", []), "expires_in": _ttl()}


def run_card(req: dict, approval: dict) -> dict:
    """After an owner's reply was accepted: the normal Confirm card, tied to the request and its ticket."""
    args = req["args_json"] if isinstance(req["args_json"], dict) else {}
    name = req["host"] if req["tool"] in _POWER_TOOLS else ", ".join(args.get("packages") or args.get("kbs") or []) or req["host"]
    action = {"kind": "action", "user": req["requester"], "tool": req["tool"], "verb": "", "args": args, "name": name,
              "title": req["label"], "label": req["label"], "host": req["host"], "host_mode": req["host_mode"], "level": req["level"],
              "message": req.get("message") or "", "expires": time.time() + _ttl(), "owner_after": True,
              "request_id": req["id"], "ticket": req["ticket"]}
    return {"kind": "action", "token": _new_token(action), "tool": req["tool"], "verb": "", "title": f"Go ahead with: {req['label']}?",
            "name": name, "host": req["host"], "host_mode": req["host_mode"], "level": req["level"], "expires_in": _ttl(),
            "detail": [f"Owner approval recorded from {approval.get('from') or 'the owner'}: \"{(approval.get('top') or '')[:160]}\"",
                       f"Request {req['id']}, ticket {req['ticket']}. Your Confirm is the last step."]}


def _tool(name: str):
    from app.tools import chat_changes, infra, infra_perf
    return {"restart_service": infra_perf.restart_service, "start_service": infra.start_service,
            "stop_service": chat_changes.stop_service, "set_service_startup": chat_changes.set_service_startup,
            "logoff_session": chat_changes.logoff_session, "run_scheduled_job": chat_changes.run_scheduled_job,
            "install_packages": chat_changes.install_packages, "remove_packages": chat_changes.remove_packages,
            "reboot_server": chat_changes.reboot_server, "shutdown_server": chat_changes.shutdown_server,
            "install_windows_updates": chat_changes.install_windows_updates,
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
    "stop_service": {"host": "named", "args": _stop_args},
    "set_service_startup": {"host": "named", "args": _startup_args},
    "logoff_session": {"host": "named", "args": _logoff_args},
    "run_scheduled_job": {"host": "named", "args": _job_args},
    "install_packages": {"host": "named", "args": _install_args, "owner_after": True},
    "reboot_server": {"host": "named", "args": _reboot_args, "owner": "before"},
    "shutdown_server": {"host": "named", "args": _shutdown_args, "owner": "before"},
    "remove_packages": {"host": "named", "args": _remove_args, "owner": "before"},
    "install_windows_updates": {"host": "named", "args": _winupdate_args, "owner": "before"},
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
    needs_owner = spec.get("owner") == "before"
    if level >= 2 and not needs_owner:
        raise AskBack(f"{tool_name} is high risk (L2) and needs the server owners' approval. Use the Agent Console for now.")

    inst = resolver(spec["host"])
    # "restart the server" means the whole machine, not a service called "server"
    if tool_name == "restart_service" and _means_machine(_service_from(message, verb), inst["name"]):
        tool_name, spec = "reboot_server", ACTIONS["reboot_server"]
        level, needs_owner = policy.risk_level(tool_name), True
    built = spec["args"](message, verb, inst)
    action = {"kind": "action", "user": user, "tool": tool_name, "verb": verb, "args": built["args"],
              "name": built["name"], "title": built["title"], "label": built["label"], "host": inst["name"],
              "host_mode": spec["host"], "level": level, "message": message[:300], "expires": time.time() + _ttl(),
              "owner_after": bool(spec.get("owner_after"))}
    if needs_owner:
        return _owner_card(action, inst, built)
    return {"kind": "action", "token": _new_token(action), "tool": tool_name, "verb": verb, "title": built["title"],
            "name": built["name"], "host": inst["name"], "host_mode": spec["host"], "level": level, "expires_in": _ttl(),
            "detail": built.get("detail", [])}


_ANY = object()


def redeem(token: str, user: str, stage=_ANY) -> dict:
    """The pending action for this token, removed so it works once. Raises ValueError with a message safe to show.
    `stage` says which step may use the token ('request' = asking the owners, None = the final Confirm). A token offered at the
    wrong step is refused WITHOUT being used up, so a mistaken click does not destroy the card."""
    with _LOCK:
        action = _PENDING.get(token or "")
        if not action:
            raise ValueError("That confirmation has already been used or is unknown. Ask again.")
        if action["expires"] < time.time():
            _PENDING.pop(token, None)
            raise ValueError("That confirmation expired. Ask again.")
        if action["user"] != user:
            raise ValueError("That confirmation belongs to someone else.")
        if stage is not _ANY and action.get("stage") != stage:
            raise ValueError("That card is the request for the owners' approval. Use its Email the owners button."
                             if action.get("stage") == "request" else "That card is not a request for the owners' approval.")
        _PENDING.pop(token, None)
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
    if tool == "stop_service":
        return f"Stopped {name} on {host}: {result.get('status_before')} -> {result.get('status')}."
    if tool == "set_service_startup":
        return f"{name} on {host}: start at boot is now {result.get('startup')}."
    if tool == "logoff_session":
        return f"On {host}: {result.get('summary')}."
    if tool == "run_scheduled_job":
        return f"Started {result.get('job')} on {host}: {result.get('state')}."
    if tool == "install_packages":
        return f"On {host}: {result.get('summary')}."
    if tool in ("reboot_server", "shutdown_server"):
        return f"{'Restart' if tool == 'reboot_server' else 'Shutdown'} of {host} is scheduled: {result.get('summary')}."
    if tool == "remove_packages":
        return f"On {host}: {result.get('summary')}."
    if tool == "install_windows_updates":
        return f"On {host}: {result.get('summary')}."
    if tool == "unlock_ad_account":
        return (f"Account {name} is still locked." if result.get("LockedOut") else f"Unlocked the account {name}.")
    return f"Done: {action['label']}."
