"""Plan enforcement: a signed licence key, per-plan feature switches, and counting of servers, approver seats and
automatic tickets.

The key is signed by the vendor with an Ed25519 private key that never leaves the vendor. This app holds only the public
key (PUBLIC_KEY below) and checks keys offline: no internet is needed, and a key cannot be edited or made up without the
private key. Honest limit: someone who can change this program's code can remove the check. The signature stops forged
and altered keys, not a modified program.

    key      TRIAGE1-<base64url(payload json)>.<base64url(signature)>
    payload  {"id", "customer", "plan", "servers", "seats", "tickets_month", "features": [...], "issued", "expires"}
             a limit of 0 means unlimited; "expires" 0 means it never expires

Rules (the customer's choice, 2026-11-10): soft limits. Being over a limit, or expired, starts a grace period
(GRACE_DAYS). After it, only NEW AUTOMATIC tickets stop. People can always sign in, chat and approve. A feature that is
not in the plan is refused at once with a plain message; that is a switch, not a limit.
Without a key the app runs as UNLICENSED: the Starter limits and no paid features.

    current()        the verified licence (or the unlicensed one)
    require(name)    raises NotInPlan when the plan lacks a feature
    status()         plan, limits, usage, problems, grace and whether automatic tickets are stopped
    auto_allowed()   may a new automatic ticket be queued now
    count_auto()     call once for every automatic ticket queued"""
import base64
import json
import logging
import threading
import time

from app import config, store

log = logging.getLogger("uvicorn.error")

# The vendor's public key (raw Ed25519, base64). The matching private key is NOT in this project.
PUBLIC_KEY = "30q+Lbp8neS1omlmigTi+IWEdpq0qL/KhcTGylcW4Gw="
PREFIX = "TRIAGE1-"
GRACE_DAYS = 14
WARN_AT = 0.8

FEATURES = {
    "chat_changes": "changes from the approver chat (stop or start a service, log off a session, packages)",
    "owner_approval": "owner approval by email (reboot, shutdown, package removal, Windows update install)",
    "telemetry_export": "telemetry export and alerts",
}
# The plan table. A key carries its own numbers, so a custom deal needs no code change; these are the defaults per plan
# and what `scripts/licence_tool.py issue --plan` fills in.
PLANS = {
    "Starter": {"servers": 25, "seats": 2, "tickets_month": 200, "features": []},
    "Team": {"servers": 100, "seats": 10, "tickets_month": 1500, "features": ["chat_changes"]},
    "Commander": {"servers": 0, "seats": 50, "tickets_month": 10000,
                  "features": ["chat_changes", "owner_approval", "telemetry_export"]},
}
# Each key is issued for exactly one ticketing tool; that tool is the only one this install can use.
ITSM = {"servicenow": "ServiceNow", "jira": "Jira Service Management", "bmc": "BMC Helix / Remedy",
        "manageengine": "ManageEngine ServiceDesk Plus", "freshservice": "Freshservice"}
UNLICENSED = {"id": "", "customer": "", "plan": "Unlicensed", **PLANS["Starter"], "issued": 0, "expires": 0, "valid": False}


class NotInPlan(Exception):
    """The plan does not include this feature. The text is safe to show to the person."""


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parse(key: str) -> dict:
    """The payload of a correctly signed key. Raises ValueError with a message for the admin."""
    key = "".join((key or "").split())
    if not key.startswith(PREFIX) or key.count(".") != 1:
        raise ValueError("That is not a TRIAGE-1 licence key (it starts with TRIAGE1- and has two parts).")
    body, sig = key[len(PREFIX):].split(".")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        raw, signature = _b64d(body), _b64d(sig)
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY)).verify(signature, raw)
        except InvalidSignature:
            raise ValueError("The licence key is not valid: its signature does not match. It was altered or not issued for this product.")
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise
    except Exception:
        raise ValueError("The licence key could not be read. Paste it again, complete and unchanged.")
    out = {"id": str(data.get("id", "")), "customer": str(data.get("customer", "")), "plan": str(data.get("plan", "")),
           "issued": float(data.get("issued") or 0), "expires": float(data.get("expires") or 0), "valid": True}
    for k in ("servers", "seats", "tickets_month"):
        out[k] = max(0, int(data.get(k) or 0))
    out["features"] = [f for f in (data.get("features") or []) if f in FEATURES]
    out["itsm"] = str(data.get("itsm") or "")
    if out["itsm"] and out["itsm"] not in ITSM:
        raise ValueError("The licence key names a ticketing tool this version does not support. Update TRIAGE-1 or ask for a new key.")
    return out


def _check_key(value: str) -> None:
    if (value or "").strip():
        parse(value)


config.register("licence.key", "Licence key", "", "text", "licence", multiline=True, max_len=4000, check=_check_key,
                help="Paste the key you were given. It is checked on this server, with no internet needed. Empty means "
                     "unlicensed: the Starter limits and no paid features.")

_cache = {"key": None, "lic": None}


def current() -> dict:
    """The licence in force. A key that no longer verifies counts as unlicensed (and says why in status())."""
    key = (config.get("licence.key") or "").strip()
    if _cache["key"] == key and _cache["lic"] is not None:
        return _cache["lic"]
    if not key:
        lic = dict(UNLICENSED)
    else:
        try:
            lic = parse(key)
        except ValueError as e:
            lic = {**UNLICENSED, "error": str(e)}
    _cache.update(key=key, lic=lic)
    return lic


def has(feature: str) -> bool:
    return feature in current().get("features", [])


def require(feature: str) -> None:
    if not has(feature):
        lic = current()
        raise NotInPlan(f"Your plan ({lic['plan']}) does not include {FEATURES.get(feature, feature)}. "
                        "Ask your administrator about upgrading the licence.")


# ---------------------------------------------------------------- usage
_USAGE_LOCK = threading.Lock()


def _month(now: float = None) -> str:
    return time.strftime("%Y-%m", time.localtime(now or time.time()))


def auto_tickets_this_month() -> int:
    u = store.get_setting("licence_usage", None) or {}
    return int(u.get("auto_tickets", 0)) if u.get("month") == _month() else 0


def count_auto(n: int = 1) -> None:
    """One more automatic ticket this month. Never raises: counting must not stop the work."""
    try:
        with _USAGE_LOCK:
            store.set_setting("licence_usage", {"month": _month(), "auto_tickets": auto_tickets_this_month() + n}, "licence")
    except Exception as e:                                   # noqa: BLE001
        log.warning("licence usage could not be counted: %s", str(e)[:120])


def _count_servers() -> int:
    from app.ports import registry
    return len(registry.get_hosts().list_hosts())


def _count_seats() -> int:
    """Active people who can act in the approver chat and on approvals (the console.act capability)."""
    from app import rbac
    rows = store.supabase.table("users").select("role,status,type").eq("status", "active").execute().data or []
    return sum(1 for r in rows if r.get("type", "human") != "service"
               and any("console.act" in rbac.ROLES[n] for n in rbac.role_names(r.get("role"))))


def usage() -> dict:
    """Counts now. A count that cannot be read is None (and is not treated as over the limit)."""
    out = {}
    for name, fn in (("servers", _count_servers), ("seats", _count_seats), ("tickets_month", auto_tickets_this_month)):
        try:
            out[name] = fn()
        except Exception as e:                               # noqa: BLE001
            out[name] = None
            log.warning("licence count of %s failed: %s", name, str(e)[:120])
    return out


LABELS = {"servers": "servers", "seats": "approver seats", "tickets_month": "automatic tickets this month"}


def evaluate(lic: dict, used: dict, over_since, now: float) -> dict:
    """Pure decision. Returns problems (over a limit or expired), warnings (80% or more), the grace state and whether
    new automatic tickets are stopped."""
    problems, warnings = [], []
    if lic.get("error"):
        warnings.append(lic["error"])
    if lic.get("valid") and lic.get("expires") and now > lic["expires"]:
        problems.append("the licence expired on " + time.strftime("%Y-%m-%d", time.localtime(lic["expires"])))
    elif lic.get("valid") and lic.get("expires") and lic["expires"] - now < 30 * 86400:
        warnings.append("the licence expires on " + time.strftime("%Y-%m-%d", time.localtime(lic["expires"])))
    for k, label in LABELS.items():
        limit, n = lic.get(k, 0), used.get(k)
        if not limit or n is None:
            continue
        # tickets: the limit itself is still allowed (200 of 200); servers and seats likewise
        if n > limit or (k == "tickets_month" and n >= limit):
            problems.append(f"{n} {label}, the plan allows {limit}")
        elif n >= limit * WARN_AT:
            warnings.append(f"{n} of {limit} {label}")
    grace_ends = (float(over_since) + GRACE_DAYS * 86400) if (problems and over_since) else None
    blocked = bool(problems) and grace_ends is not None and now > grace_ends
    return {"problems": problems, "warnings": warnings, "grace_ends": grace_ends, "auto_blocked": blocked}


_status_cache = {"at": 0.0, "value": None}


def status(force: bool = False) -> dict:
    """Everything the admin panel shows. Cached for a minute; also starts or clears the grace period."""
    now = time.time()
    if not force and _status_cache["value"] and now - _status_cache["at"] < 60 and _cache["key"] == (config.get("licence.key") or "").strip():
        return _status_cache["value"]
    lic, used = current(), usage()
    over_since = store.get_setting("licence_over_since", None)
    ev = evaluate(lic, used, over_since, now)
    try:
        if ev["problems"] and not over_since:
            over_since = now
            store.set_setting("licence_over_since", now, "licence")
            store.audit(None, None, "system", "licence_grace_started", "; ".join(ev["problems"])[:300])
            ev = evaluate(lic, used, over_since, now)
        elif not ev["problems"] and over_since:
            store.set_setting("licence_over_since", None, "licence")
            store.audit(None, None, "system", "licence_back_in_limits", "")
    except Exception as e:                                   # noqa: BLE001
        log.warning("licence grace state could not be saved: %s", str(e)[:120])
    value = {"itsm": ITSM.get(lic.get("itsm") or "", ""), "plan": lic["plan"], "customer": lic.get("customer", ""), "id": lic.get("id", ""), "licensed": bool(lic.get("valid")),
             "expires": lic.get("expires") or None,
             "limits": {k: lic.get(k, 0) for k in LABELS}, "usage": used,
             "features": {f: f in lic.get("features", []) for f in FEATURES}, **ev}
    _status_cache.update(at=now, value=value)
    return value


def auto_allowed() -> bool:
    """May a new automatic ticket be queued. A failure to work it out never stops the work."""
    try:
        return not status()["auto_blocked"]
    except Exception as e:                                   # noqa: BLE001
        log.warning("licence check failed, allowing the ticket: %s", str(e)[:120])
        return True


def describe(s: dict = None) -> str:
    """One paragraph for the Check licence button."""
    s = s or status(force=True)
    lim = lambda k: "unlimited" if not s["limits"][k] else str(s["limits"][k])          # noqa: E731
    use = lambda k: "?" if s["usage"][k] is None else str(s["usage"][k])                # noqa: E731
    parts = [f"{s['plan']}" + (f" for {s['customer']}" if s["customer"] else "") + (" (no key entered)" if not s["licensed"] else "")
             + (f", ticketing tool: {s['itsm']}" if s.get("itsm") else ""),
             "; ".join(f"{LABELS[k]} {use(k)} of {lim(k)}" for k in LABELS),
             "features: " + (", ".join(f for f, on in s["features"].items() if on) or "none")]
    if s["problems"]:
        when = time.strftime("%Y-%m-%d", time.localtime(s["grace_ends"])) if s["grace_ends"] else "?"
        parts.append(("NEW AUTOMATIC TICKETS ARE STOPPED: " if s["auto_blocked"] else f"over the plan, automatic tickets stop after {when}: ")
                     + "; ".join(s["problems"]))
    if s["warnings"]:
        parts.append("note: " + "; ".join(s["warnings"]))
    return ". ".join(parts)
