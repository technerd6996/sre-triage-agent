"""Settings service: one place to read every tunable value.

Order: saved value (settings table, `cfg.<key>`) -> older settings row (`legacy_key`) -> environment
variable -> default. With nothing saved, behaviour is exactly what the .env gave before.
Secrets are not handled here yet (they arrive with the credentials provider).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger("uvicorn.error")

PREFIX = "cfg."
SEC = "sec."                     # encrypted secrets live under this prefix
_TTL = 5.0                      # seconds a snapshot of the settings table is reused


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    env: str = ""
    kind: str = "text"          # text | int | float | bool | select
    default: object = ""
    group: str = "agent"
    live: bool = True           # False = needs a restart to apply
    options: tuple = ()
    lo: float | None = None
    hi: float | None = None
    legacy_key: str = ""        # an older settings row this value used to live in
    help: str = ""
    bridge: bool = False        # code still reads it with os.getenv: a saved value is copied into the environment

    @property
    def secret(self) -> bool:
        return self.kind == "secret"


SETTINGS = {s.key: s for s in (
    Setting("model.name", "Model name sent to the server", "LLM_MODEL_NAME", "text", "Qwen/Qwen3-8B-AWQ",
            group="model", help="Must match the model the server loaded. Saved on every call so models can be compared."),
    Setting("runs.max_concurrent", "Tickets running at once", "RUNS_MAX_CONCURRENT", "int", 2,
            group="model", lo=2, hi=8,
            help="How many tickets the agent works at the same time. Each needs a different server."),
    Setting("model.temperature", "Temperature", "LLM_TEMPERATURE", "float", 0.4, group="model", lo=0, hi=2),
    Setting("model.max_tokens", "Max tokens per reply", "LLM_MAX_TOKENS", "int", 1024, group="model", lo=64, hi=8192,
            help="Caps a runaway reply. A reply cut off here fails JSON parsing and is counted as wasted tokens."),
    Setting("model.timeout_seconds", "Request timeout (seconds)", "LLM_TIMEOUT_SECONDS", "int", 180, group="model", lo=10, hi=1800),
    Setting("model.slots", "Concurrent model requests", "LLM_SLOTS", "int", 1, group="model", lo=1, hi=8,
            help="1 for a one-at-a-time endpoint. With vLLM, 2 to 4 lets the server batch requests."),
    Setting("budget.daily_tokens", "Daily token budget (0 = off)", "BUDGET_DAILY_TOKENS", "int", 0,
            group="model", lo=0, hi=1000000000,
            help="Warning threshold for all model tokens in one day. Warns only; does not stop runs yet."),
    Setting("budget.warn_pct", "Warn at percent of budget", "BUDGET_WARN_PCT", "int", 80,
            group="model", lo=1, hi=100),
    Setting("identity.refresh_minutes", "AD group refresh interval (minutes)", "IDENTITY_REFRESH_MINUTES", "int", 15,
            group="identity", lo=1, hi=1440,
            help="How often a signed-in user's AD groups are re-checked. Removed access ends within this time."),
    Setting("agent.approval_timeout_min", "Approval timeout (minutes)", "APPROVAL_TIMEOUT_MIN", "int", 30,
            lo=1, hi=240, legacy_key="approval_timeout_min",
            help="No answer in this time: the request is rejected and the ticket is escalated."),
    Setting("agent.run_stall_timeout_min", "Run stall timeout (minutes)", "RUN_STALL_TIMEOUT_MIN", "int", 10,
            lo=1, hi=240, help="No progress for this long: the run is marked failed and the slot is released."),
    Setting("agent.p1_time_budget_min", "P1 time budget (minutes)", "P1_TIME_BUDGET_MIN", "int", 15,
            lo=1, hi=480, help="A run longer than this proposes a P1 declaration."),
    Setting("agent.verify_soak_seconds", "Checker soak time (seconds)", "VERIFY_SOAK_SECONDS", "float", 60.0,
            lo=0, hi=600, help="Gap between the two checks that catch a service that dies again."),
    Setting("agent.checker_mode", "Checker mode", "CHECKER_MODE", "select", "shadow",
            options=("off", "shadow", "on"),
            help="off: no check. shadow: verify and write a note only. on: close or reopen the ticket."),
    Setting("agent.never_auto_services", "Never auto-restore (comma separated)", "NEVER_AUTO_SERVICES", "text", "",
            help="Services that always go to a human. Empty means no exclusions."),
    Setting("agent.auto_max_fails", "Auto-mode circuit breaker (failed runs)", "AUTO_MAX_FAILS", "int", 3,
            lo=1, hi=20, help="This many failed auto runs in a row switch auto mode off."),
    Setting("agent.poll_seconds", "Queue poll interval (seconds)", "POLL_SECONDS", "int", 15,
            lo=5, hi=3600),
    Setting("credentials.max_per_run", "Credential checkouts allowed per run", "PAM_MAX_PER_RUN", "int", 40,
            group="credentials", lo=1, hi=500, help="More than this in one run is refused and audited."),
    Setting("credentials.max_per_hour", "Credential checkouts allowed per hour", "PAM_MAX_PER_HOUR", "int", 200,
            group="credentials", lo=1, hi=5000, help="Across all runs. A burst above this is refused and audited."),
    Setting("credentials.inquiry_max_per_hour", "Inquiry credential checkouts allowed per hour", "PAM_INQUIRY_MAX_PER_HOUR", "int", 60,
            group="credentials", lo=1, hi=2000, help="Across all approver inquiries. A burst above this is refused and audited."),
    Setting("credentials.inquiry_max_per_user_per_hour", "Inquiry credential checkouts allowed per user per hour", "PAM_INQUIRY_MAX_PER_USER_PER_HOUR", "int", 20,
            group="credentials", lo=1, hi=500, help="Per approver. More than this in an hour is refused and audited."),
)}


def _add(key, label, env, kind="text", default="", group="ticketing", **kw):
    """Settings the rest of the code still reads with os.getenv; they apply after a restart."""
    # Set live=True for ticketing fields so changes apply immediately without restarts
    is_live = True if group == "ticketing" else False
    SETTINGS[key] = Setting(key, label, env, kind, default, group, live=is_live, bridge=True, **kw)

_add("agent.llm_checker", "LLM checker (shadow)", "LLM_CHECKER", "bool", True, group="agent")
_add("ticketing.instance", "ServiceNow instance URL", "SNOW_INSTANCE", help="For example https://company.service-now.com")
_add("ticketing.caller", "Ticket caller", "SNOW_CALLER", default="sre_triage_agent")
_add("ticketing.assignment_group", "Assignment group", "SNOW_ASSIGNMENT_GROUP", default="L1-SRE-Agent")
_add("ticketing.assignee", "Assignee", "SNOW_ASSIGNEE", default="svc_triage_01")
_add("ticketing.close_code", "Close code", "SNOW_CLOSE_CODE", default="Solution provided")
_add("ticketing.escalation_group", "Escalation group", "SNOW_ESCALATION_GROUP")
_add("ticketing.channel", "Contact channel", "SNOW_CHANNEL", default="virtual_agent")
_add("ticketing.rate_max", "Tickets per user per window", "TICKET_RATE_MAX", "int", 5, lo=1, hi=1000)
_add("ticketing.user", "ServiceNow user (agent)", "SNOW_USER", "secret")
_add("ticketing.password", "ServiceNow password (agent)", "SNOW_PASSWORD", "secret")
_add("ticketing.checker_user", "ServiceNow user (checker)", "SNOW_CHECKER_USER", "secret")
_add("ticketing.checker_password", "ServiceNow password (checker)", "SNOW_CHECKER_PASSWORD", "secret")
_add("identity.server", "LDAP Server", "IDENTITY_SERVER", group="identity")
_add("identity.domain", "Domain (e.g., triage.local)", "IDENTITY_DOMAIN", group="identity")
_add("identity.search_base", "Search Base (e.g., DC=triage,DC=local)", "IDENTITY_SEARCH_BASE", group="identity")
_add("identity.bind_user", "Service Account DN", "IDENTITY_BIND_USER", group="identity")
_add("identity.bind_password", "Service Account Password", "IDENTITY_BIND_PASSWORD", "secret", group="identity")
_add("model.colab_api_url", "Model API URL", "COLAB_API_URL", group="model")
_add("agent.max_tokens_per_run", "Token budget per run", "AGENT_MAX_TOKENS_PER_RUN", "int", 60000,
     group="model", lo=5000, hi=500000, help="A run above this is stopped and escalated.")
_add("knowledge.neo4j_uri", "Neo4j URI", "NEO4J_URI", group="knowledge")
_add("knowledge.neo4j_user", "Neo4j user", "NEO4J_USER", group="knowledge")
_add("knowledge.neo4j_pass", "Neo4j password", "NEO4J_PASS", "secret", group="knowledge")
_add("knowledge.skills_source", "Skills source", "SKILLS_SOURCE", "select", "file", "knowledge", options=("file", "kb"))
_add("knowledge.skills_kb_name", "Skills KB name", "SKILLS_KB_NAME", default="TRIAGE-1 Playbooks", group="knowledge")
_add("knowledge.skills_ttl", "Skills cache (seconds)", "SKILLS_TTL_SECONDS", "int", 300, "knowledge", lo=10, hi=86400)
_add("notifications.transport", "Notice transport", "NOTICE_TRANSPORT", group="notifications")
_add("notifications.aws_region", "AWS region", "AWS_REGION", group="notifications")
_add("notifications.aws_profile", "AWS profile", "AWS_PROFILE", group="notifications")
_add("notifications.ses_from", "SES sender", "SES_FROM", group="notifications")
_add("notifications.sns_topic_arn", "SNS topic ARN", "SNS_TOPIC_ARN", group="notifications")
_add("notifications.aws_access_key_id", "AWS access key ID", "AWS_ACCESS_KEY_ID", "secret", group="notifications")
_add("notifications.aws_secret_access_key", "AWS secret access key", "AWS_SECRET_ACCESS_KEY", "secret", group="notifications")
_add("notifications.smtp_host", "SMTP host", "SMTP_HOST", group="notifications")
_add("notifications.smtp_port", "SMTP port", "SMTP_PORT", group="notifications")
_add("notifications.smtp_user", "SMTP user", "SMTP_USER", group="notifications")
_add("notifications.smtp_from", "SMTP sender", "SMTP_FROM", group="notifications")
_add("notifications.smtp_password", "SMTP password", "SMTP_PASSWORD", "secret", group="notifications")


def _cast(s: Setting, v):
    if s.kind in ("int", "float"):
        what = "a whole number" if s.kind == "int" else "a number"
        if isinstance(v, bool):
            raise ValueError(f"{s.label} must be {what}")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{s.label} must be {what}") from None
        if s.kind == "int":
            if f != int(f):
                raise ValueError(f"{s.label} must be {what}")
            n = int(f)
        else:
            n = f
        if s.lo is not None and n < s.lo:
            raise ValueError(f"{s.label} must be at least {s.lo:g}")
        if s.hi is not None and n > s.hi:
            raise ValueError(f"{s.label} must be at most {s.hi:g}")
        return n
    if s.kind == "bool":
        t = str(v).strip().lower()
        if t in ("1", "true", "yes", "on"):
            return True
        if t in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"{s.label} must be on or off")
    if s.kind == "select":
        t = str(v).strip().lower()
        if t not in s.options:
            raise ValueError(f"{s.label} must be one of: {', '.join(s.options)}")
        return t
    if s.kind == "secret":
        t = str(v).strip("\r\n")
        if not t:
            raise ValueError(f"{s.label} cannot be empty")
        return t
    return str(v).strip()


_cache = {"at": 0.0, "rows": {}}


def _rows() -> dict:
    """One query for the whole settings table, reused for a few seconds."""
    if time.time() - _cache["at"] < _TTL:
        return _cache["rows"]
    try:
        from app import store                       # lazy: store needs the bootstrap env when imported
        data = store.supabase.table("settings").select("key,value_json").execute().data
        _cache["rows"] = {r["key"]: r["value_json"] for r in data}
    except Exception as e:                          # keep the last good snapshot
        log.warning("config: could not read settings (%s); using the last known values", str(e)[:150])
    _cache["at"] = time.time()
    return _cache["rows"]


def _resolve(s: Setting):
    rows = _rows()
    for key in (PREFIX + s.key, s.legacy_key):
        if key and key in rows:
            try:
                return _cast(s, rows[key]), "saved"
            except ValueError as e:
                log.warning("config: ignoring saved %s: %s", key, e)
    raw = os.getenv(s.env) if s.env else None
    if raw not in (None, ""):
        try:
            return _cast(s, raw), "env"
        except ValueError as e:
            log.warning("config: ignoring %s from the environment: %s", s.env, e)
    return s.default, "default"


def get(key: str):
    s = SETTINGS[key]
    if s.secret:
        raise ValueError(f"{s.label} is a secret; read it with get_secret()")
    return _resolve(s)[0]


def current(key: str):
    """(value, source) for a normal setting."""
    return _resolve(SETTINGS[key])


def validate(key: str, value):
    """Returns the cleaned value or raises ValueError with a message fit to show the admin."""
    return _cast(SETTINGS[key], value)


def save(key: str, value, by: str = "system"):
    s = SETTINGS[key]
    if s.secret:
        return save_secret(key, value, by)
    new = _cast(s, value)
    old = _resolve(s)[0]
    from app import store
    store.supabase.table("settings").upsert({"key": PREFIX + key, "value_json": new, "updated_by": by,
                                             "updated_at": time.time()}).execute()
    _cache["at"] = 0.0
    try:
        store.audit(None, None, by, "config_changed", f"{key}: {old!r} -> {new!r}")
    except Exception as e:
        log.warning("config: audit failed: %s", str(e)[:150])
    return new


def reset(key: str, by: str = "system") -> None:
    """Drops the saved value, so the .env value (or the default) applies again."""
    s = SETTINGS[key]
    from app import store
    for k in (PREFIX + key, SEC + key, s.legacy_key):
        if k:
            store.supabase.table("settings").delete().eq("key", k).execute()
    _cache["at"] = 0.0
    try:
        store.audit(None, None, by, "config_reset", key)
    except Exception as e:
        log.warning("config: audit failed: %s", str(e)[:150])


def describe(group: str | None = None) -> list[dict]:
    """What the Configuration pane will render: value, where it came from, and its limits."""
    out = []
    for s in SETTINGS.values():
        if group and s.group != group:
            continue
        item = {"key": s.key, "label": s.label, "kind": s.kind, "group": s.group, "live": s.live,
                "options": list(s.options), "min": s.lo, "max": s.hi, "help": s.help, "default": s.default}
        if s.secret:                                    # never returned: only whether it is set
            meta = _rows().get(SEC + s.key) or {}
            item.update(value=None, is_set=bool(meta), version=meta.get("ver"), updated=meta.get("at"),
                        source="saved" if meta else ("env" if s.env and os.getenv(s.env) else "unset"))
        else:
            item["value"], item["source"] = _resolve(s)
        out.append(item)
    return out

# ---- secrets: encrypted with TRIAGE_SECRET_KEY, stored as `sec.<key>`, never returned by any API ----
def secrets_ready() -> bool:
    return bool(os.getenv("TRIAGE_SECRET_KEY", "").strip())


def _fernet():
    key = os.getenv("TRIAGE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("TRIAGE_SECRET_KEY is not set, so secrets cannot be stored or read. Generate a key with: "
                           'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def is_set(key: str) -> bool:
    return bool(_rows().get(SEC + key))


def save_secret(key: str, value, by: str = "system") -> None:
    s = SETTINGS[key]
    token = _fernet().encrypt(_cast(s, value).encode()).decode()
    from app import store
    old = _rows().get(SEC + key) or {}
    store.supabase.table("settings").upsert({
        "key": SEC + key, "value_json": {"v": token, "ver": int(old.get("ver", 0)) + 1, "at": time.time()},
        "updated_by": by, "updated_at": time.time()}).execute()
    _cache["at"] = 0.0
    try:
        store.audit(None, None, by, "secret_changed", key)            # the value is never logged
    except Exception as e:
        log.warning("config: audit failed: %s", str(e)[:150])


def get_secret(key: str) -> str:
    """Saved secret (decrypted), else the environment variable, else empty."""
    s = SETTINGS[key]
    row = _rows().get(SEC + key)
    if row:
        return _fernet().decrypt(row["v"].encode()).decode()
    return os.getenv(s.env, "") if s.env else ""


def hydrate_environ() -> int:
    """Copies saved settings and secrets into os.environ so code that still uses os.getenv sees them.
    A saved value wins over the .env, as in get(). A failure never stops the app from starting."""
    n = 0
    for s in SETTINGS.values():
        if not (s.bridge and s.env):
            continue
        try:
            if s.secret:
                row = _rows().get(SEC + s.key)
                if not row:
                    continue
                value = _fernet().decrypt(row["v"].encode()).decode()
            else:
                value, source = _resolve(s)
                if source != "saved":
                    continue
        except Exception as e:
            log.warning("config: could not load %s into the environment (%s)", s.key, type(e).__name__)
            continue
        os.environ[s.env] = str(value)
        n += 1
    if n:
        log.info("config: %d saved setting(s) loaded into the environment", n)
    return n