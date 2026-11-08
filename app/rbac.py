"""Role engine: additive roles, one capability per route, deny by default."""
from __future__ import annotations

import logging
import re

log = logging.getLogger("uvicorn.error")

ALL_CAPS = frozenset({
    "chat.create",     # chat page, create tickets
    "console.view",    # Agent console and L1 queue (read)
    "console.act",     # approve or reject, run runbooks
    "admin.open",      # open the admin page
    "monitor.view",    # metrics and monitoring dashboards
    "audit.view",      # audit log
    "config.manage",   # Controls and the Configuration pane
    "users.manage",    # users, roles (later AD mapping)
})

ROLES = {
    "auditors":    frozenset({"admin.open", "monitor.view", "audit.view"}),
    "users":       frozenset({"chat.create"}),
    "approvers":   frozenset({"chat.create", "console.view", "console.act", "admin.open", "monitor.view"}),
    "admin":       frozenset({"admin.open", "monitor.view", "audit.view", "config.manage"}),
    "super_admin": ALL_CAPS,
}
ROLE_LABELS = {"auditors": "Auditor", "users": "User", "approvers": "Approver", "admin": "Admin",
               "super_admin": "Super Admin"}
_LEGACY = {"viewer": "users", "approver": "approvers"}      # rows not yet migrated still work
ANY = "authenticated"                                       # any signed-in user, no role needed


def role_names(value) -> list[str]:
    """A user's roles. One role today; a comma list works too, so roles are additive."""
    out = []
    for r in str(value or "").split(","):
        r = _LEGACY.get(r.strip(), r.strip())
        if r in ROLES and r not in out:
            out.append(r)
    return out


def caps_for(value) -> frozenset:
    caps: set = set()
    for r in role_names(value):
        caps |= ROLES[r]
    return frozenset(caps)


def label_for(value) -> str:
    return ", ".join(ROLE_LABELS[r] for r in role_names(value)) or "No role"


def home_for(value) -> str:
    if set(role_names(value)) & {"super_admin", "admin", "auditors"}:
        return "/admin"
    return "/" if "chat.create" in caps_for(value) else "/static/login.html"


# ---- Route rules. First match wins. A route that matches nothing is denied. ----
_RULES: list = []


def _r(methods: str, pattern: str, need) -> None:
    m = None if methods == "*" else frozenset(methods.split("|"))
    _RULES.append((m, re.compile(pattern), (need,) if isinstance(need, str) else tuple(need)))


_G = "GET|HEAD"
# any signed-in user
_r("*", r"/api/auth/(me|logout|change-password)", ANY)
_r(_G, r"/api/(ready|chat/status)", ANY)
# pages
_r(_G, r"/", "chat.create")
_r(_G, r"/admin", "admin.open")
_r(_G, r"/agent", "console.view")
# chat and tickets
_r("POST", r"/api/(chat|title)", "chat.create")
_r(_G, r"/api/servers", "chat.create")
_r("*", r"/api/issues(/.*)?", "chat.create")
_r("*", r"/api/approver-chat(/.*)?", "console.act")       # ad-hoc runs: approvers only, never inherited by users
# agent
_r(_G, r"/api/agent/(live|runs/[^/]+)", ("chat.create", "console.view"))
_r(_G, r"/api/agent/(status|llm-stats|skills-status)", "monitor.view")
_r(_G, r"/api/agent/auto", ("console.view", "config.manage"))
_r("*", r"/api/agent/auto", "config.manage")
_r(_G, r"/api/agent/audit", "audit.view")
_r(_G, r"/api/agent/(queue|runs|approvals)", "console.view")
_r("POST", r"/api/agent/(run|runs/[^/]+/(decision|p1))", "console.act")
# admin API
_r(_G, r"/api/admin/(summary|bot-stats|rag-stats|api-health|runs|jobs)", "monitor.view")
_r(_G, r"/api/admin/audit(\.csv)?", "audit.view")
_r("POST", r"/api/admin/users/[^/]+/revoke", "users.manage")
_r("*", r"/api/admin/users(/[^/]+)?", "users.manage")
_r("*", r"/api/users", "users.manage")                     # ServiceNow user search on the Users page
# AD group -> role mapping lives on the Users page. It grants roles (up to super_admin), so it must
# need users.manage; under the config.manage catch-all below an Admin could promote themself.
_r("*", r"/api/admin/(ad-mappings(/[^/]+)?|ad-groups/search)/?", "users.manage")
_r("*", r"/api/admin(/.*)?", "config.manage")              # settings, controls, config pane, cancel and rerun


def rule_for(method: str, path: str):
    """The capabilities that open this route (any one is enough), or None when no rule exists."""
    method = method.upper()
    for methods, pat, need in _RULES:
        if (methods is None or method in methods) and pat.fullmatch(path):
            return need
    return None


def allows(role_value, need) -> bool:
    if need == (ANY,):
        return True
    return bool(caps_for(role_value) & set(need))


def is_page(path: str) -> bool:
    return path in ("/", "/admin", "/agent")


# ---- Startup check ----
_SKIP = ("/api/health", "/api/auth/login", "/api/auth/banner", "/setup", "/api/setup", "/static")


def unruled(app) -> list[str]:
    out = []
    for r in app.routes:
        path, methods = getattr(r, "path", None), getattr(r, "methods", None)
        if not path or not methods or path.startswith(_SKIP):
            continue
        sample = re.sub(r"\{[^}]+\}", "x", path)
        out += [f"{m} {path}" for m in methods - {"HEAD", "OPTIONS"} if rule_for(m, sample) is None]
    return sorted(out)


def warn_unruled(app) -> None:
    missing = unruled(app)
    if missing:
        log.warning("RBAC: %d route(s) have no rule and will be denied: %s", len(missing), "; ".join(missing))