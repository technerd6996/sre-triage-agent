"""Read-only questions an approver can ask about a server in the chat (uptime, pending reboot, network, event errors, sessions,
failed services, scheduled jobs, Windows updates, Linux packages).

pick()   finds which check a question is about (first match wins; most specific words first).
args()   reads the extra inputs that check needs from the message (a host and port, a number of minutes, a package name).
Nothing here runs a command: the tools in app/tools/chat_tools.py do, and they validate everything again."""
import re

from app import config
from app.tools import chat_tools as T

# A question about services that exist or run on a server ("are there any sccm services", "is the spooler service running").
# "which services depend on X" is a topology question, not this one.
SERVICE_QUESTION = (r"^(?!.*\bdepend)(?=.*(\b(sccm|mecm|configmgr|ccmexec)\b|configuration manager"
                    r"|\bservices?\b.{0,30}\b(running|stopped|installed|present|exist\w*|status|state|in it|on it|there|listed)\b"
                    r"|\b(is|are|was|were|list|show|which|any|those|these)\b.{0,40}\bservices?\b))")

config.register("chat.service_search_aliases", "Service search words (what people say = names to look for)",
                "sccm = ccmexec, ccmsetup, cmrcservice, smstsmgr, SMS Agent Host, Configuration Manager\n"
                "mecm = ccmexec, ccmsetup, cmrcservice, smstsmgr, SMS Agent Host, Configuration Manager\n"
                "configmgr = ccmexec, ccmsetup, cmrcservice, smstsmgr, SMS Agent Host, Configuration Manager\n"
                "defender = WinDefend, Sense, Windows Defender\nsql = MSSQL, SQLAgent\nzabbix = zabbix",
                "text", "agent", multiline=True, max_len=4000,
                help="Lets a question like 'are there any sccm services on it' look for the real service names. Write: word = name, name, "
                     "name. A word that is not listed is searched for as typed.")

# (pattern on the lower-cased question, tool). First match wins.
_RULES = (
    (r"security (updates?|patch(es)?|fix(es)?)", T.list_security_updates),
    (r"upgradable|outdated packages|available upgrades|package updates|packages? (that )?(can|could) be (updated|upgraded)", T.list_upgradable),
    (r"(patch|update)s? (job|progress|result)|status of (the )?(patch|update) job|did the (patch|update)", T.check_patch_job),
    (r"windows updates?|\bhotfix|\bpatch(es|ing)?\b|(patch|update)s? status|status of (the )?(patch|update)s?\b|\b(pending|available|missing|waiting|outstanding|installed|needed) (updates?|patches)\b|\bupdates?\b.{0,25}\b(available|waiting|pending|missing|installed|needed|outstanding)\b|\blast (patch|update)\b|up[- ]to[- ]date|pending updates|missing updates|installed updates|\bkb\d{6,}",
     T.check_windows_updates),
    (r"\buptime\b|\bup ?time\b|boot( |-)?up|boot time|\bbooted\b|last (boot|reboot|restart)|when was .{0,40}(rebooted|restarted|booted)|how long .{0,40}\bup\b|is .{0,40}\bback\b|\b(has|have|did|was|is)\b.{0,40}\b(rebooted|restarted|booted)\b|\bup yet\b|\bis (it|[a-z0-9._-]+) up\b|\b(come|came) back\b",
     T.check_uptime),
    (r"pending (reboot|restart)|(reboot|restart) (is )?(pending|required|needed)|needs? (a )?(reboot|restart)|reboot required",
     T.check_pending_reboot),
    (r"\bping\b|\bdns\b|\bport\b|\bnslookup\b|\btelnet\b|can .{0,60}\breach\b|\breachab|connect(ivity)? to", T.check_network),
    (r"event (log|errors?)|\bjournal(ctl)?\b|error log|errors? in the (system )?log|recent errors|\bsyslog\b", T.get_event_errors),
    (r"\bsessions?\b|who is logged|logged (in|on)|\bquser\b|\brdp users\b", T.list_sessions),
    (r"(failed|stopped|not running|down) (services?|units)|services? (that )?(are|is) (failed|stopped|not running|down)|failed units",
     T.list_failed_services),
    (r"scheduled (tasks?|jobs?)|\bcron\b|\btimers?\b", T.list_scheduled_jobs),
    (SERVICE_QUESTION, T.find_services),
    (r"installed packages|list packages|\bpackages?\b|\brpm\b|\bdpkg\b|\bapt\b|\b(is|are)\s+[a-z0-9][a-z0-9+._-]*\s+installed\b"
     r"|\b(installed )?version of [a-z0-9][a-z0-9+._-]*", T.list_packages),
)

_HOWTO = re.compile(r"^(how (do|to|can)|what is a|explain)\b")


def pick(question: str):
    """The tool this question is about, or None."""
    q = (question or "").lower()
    if _HOWTO.match(q.strip()):
        return None
    for pattern, tool in _RULES:
        if re.search(pattern, q):
            return tool
    return None


def for_platform(tool, platform: str):
    """Windows has no package manager listing; Linux has no Windows Update. Map to the closest real check."""
    if platform == "windows" and tool in (T.list_upgradable, T.list_security_updates):
        return T.check_windows_updates
    if platform == "linux" and tool is T.check_windows_updates:
        return T.list_upgradable
    return tool


class NeedsMore(Exception):
    """The question is not enough to run the check; the text is the follow-up question."""


_NOT_A_HOST = {"the", "a", "an", "it", "this", "that", "them", "me", "us", "from", "port", "to", "on", "server", "host", "address"}
_IPV4 = r"\b\d{1,3}(?:\.\d{1,3}){3}\b"
_FQDN = r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+\b"


def source_hint(question: str) -> str:
    """'ping db01 from web01' or 'can web01 reach db01' -> 'web01': the server the check should run FROM."""
    q = (question or "").lower()
    for pattern in (r"\bfrom\s+([a-z0-9][a-z0-9._-]*)",
                    r"\b(?:can|could|does|is|will)\s+([a-z0-9][a-z0-9._-]*)\s+(?:able to\s+)?(?:reach|connect|ping|resolve|talk)"):
        m = re.search(pattern, q)
        if m and m.group(1) not in _NOT_A_HOST:
            return m.group(1)
    return ""


def network_target(question: str, server_names: list) -> tuple:
    """(host, port) to test from the server. The server the check runs on is not the target."""
    q = (question or "").lower()
    skip = {s.lower() for s in server_names}
    port = 0
    pm = re.search(r"\bport\s+(\d{1,5})\b", q) or re.search(r"[a-z0-9.\]-]:(\d{1,5})\b", q)
    if pm:
        port = int(pm.group(1))
    host = ""
    for pattern in (_IPV4, _FQDN):
        for m in re.finditer(pattern, q):
            if m.group(0) not in skip:
                host = m.group(0)
                break
        if host:
            break
    if not host:
        m = re.search(r"\b(?:ping|reach|resolve|lookup|nslookup|connect to|connectivity to|telnet)\s+(?:to\s+)?([a-z0-9][a-z0-9._-]*)", q)
        if m and m.group(1) not in skip and m.group(1) not in _NOT_A_HOST:
            host = m.group(1)
    if not host:
        raise NeedsMore("Which host or address should the server check? For example: ping 10.0.0.5 from term-win-srv, "
                        "or can term-win-srv reach db01 on port 1433.")
    return host, port


_NOT_A_SERVICE = {"the", "a", "an", "those", "these", "that", "this", "any", "all", "which", "what", "running", "stopped", "failed", "windows",
                  "system", "of", "are", "is", "was", "were", "there", "other", "some", "other", "installed", "same", "listed", "its", "it"}


def _aliases() -> dict:
    out = {}
    for line in (config.get("chat.service_search_aliases") or "").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = (x.strip() for x in line.split("=", 1))
            if k and v:
                out[k.lower()] = v
    return out


def service_names(question: str) -> str:
    """The service names to look for, as a comma separated text. Raises NeedsMore when the question names none."""
    q = (question or "").lower()
    for word, names in sorted(_aliases().items(), key=lambda kv: -len(kv[0])):
        if re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", q):
            return names
    for pattern in (r"services? (?:named|called|like|matching|containing)\s+['\"]?([a-z0-9][a-z0-9 _.@-]{0,40}?)['\"]?(?:\s|$|\?)",
                    r"\b([a-z0-9][a-z0-9_.@-]*)\s+services?\b",
                    r"\bservices?\s+([a-z0-9][a-z0-9_.@-]*)\b"):
        for m in re.finditer(pattern, q):
            word = m.group(1).strip()
            if word not in _NOT_A_SERVICE and not re.fullmatch(r"(running|stopped|installed|present|exists?|there|on|in|up|down|status|state|list|do|does)", word):
                return _aliases().get(word, word)
    raise NeedsMore("Which service? For example: is the spooler service running on term-win-srv, or are there any sccm services on it.")


def minutes_in(question: str) -> int:
    m = re.search(r"(\d+)\s*(min(?:ute)?s?|h(?:ours?)?|d(?:ays?)?)\b", (question or "").lower())
    if not m:
        return 60
    n, unit = int(m.group(1)), m.group(2)
    return n * (60 if unit.startswith("h") else 1440 if unit.startswith("d") else 1)


def package_filter(question: str) -> str:
    q = (question or "").lower()
    for pattern in (r"packages? (?:named|matching|like|containing|called)\s+([a-z0-9][a-z0-9+._-]*)",
                    r"\b(?:is|are)\s+([a-z0-9][a-z0-9+._-]*)\s+installed",
                    r"(?:version of|installed version of)\s+([a-z0-9][a-z0-9+._-]*)"):
        m = re.search(pattern, q)
        if m and m.group(1) not in ("the", "a", "any", "all"):
            return m.group(1)
    return ""


def args(tool, question: str, platform: str, server_name: str) -> dict:
    """Arguments for this check beyond the platform. May raise NeedsMore."""
    out = {"target": platform}
    if tool is T.check_network:
        host, port = network_target(question, [server_name])
        out.update(host=host, port=port)
    elif tool is T.get_event_errors:
        out.update(minutes=minutes_in(question))
    elif tool is T.find_services:
        out.update(names=service_names(question))
    elif tool is T.list_packages:
        flt = package_filter(question)
        if flt:
            out.update(name=flt)
    return out
