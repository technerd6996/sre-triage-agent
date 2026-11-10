"""Credential checkout rules. Used inside the tool layer only; the model never sees what this returns.

Every checkout needs an agent run, counts against a per-run and a per-hour limit, and writes an audit
row (host, which key matched, result, provider). Secret values are never written anywhere."""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque

from app import config, telemetry
from app.llm import usage
from app.ports import registry
from app.ports.errors import NotFound, PortError

log = logging.getLogger("triage.pam")

_lock = threading.Lock()
_recent: deque = deque()                  # timestamps of every checkout in the last hour
_per_run: OrderedDict = OrderedDict()     # run id -> checkouts so far (newest 200 runs kept)


def _audit(run_id, ticket, action: str, detail: str) -> None:
    try:
        from app import store                 # lazy: importing store opens the database client
        store.audit(run_id, ticket, "agent", action, detail)
    except Exception as e:                    # noqa: BLE001  a database blip must not stop a run
        log.error("PAM audit row not written (%s): %s %s", type(e).__name__, action, detail)


def _run_ticket(run_id):
    try:
        from app.api.agent import RUNS        # lazy: avoids a circular import
        return (RUNS.get(run_id) or {}).get("ticket")
    except Exception:                         # noqa: BLE001
        return None


def _allow(run_id: str):
    """None when this checkout may go ahead, else the reason it may not."""
    now = time.time()
    with _lock:
        while _recent and now - _recent[0] > 3600:
            _recent.popleft()
        if len(_recent) >= config.get("credentials.max_per_hour"):
            return "hourly limit reached"
        if _per_run.get(run_id, 0) >= config.get("credentials.max_per_run"):
            return "per-run limit reached"
        _recent.append(now)
        _per_run[run_id] = _per_run.get(run_id, 0) + 1
        _per_run.move_to_end(run_id)
        while len(_per_run) > 200:
            _per_run.popitem(last=False)
    return None


@telemetry.traced("credential_store", "checkout")
def checkout(*keys: str) -> dict:
    """The credential stored for the first key that has one. Pass the stable host id first,
    then the address as a fallback. Raises RuntimeError with a message that is safe to show."""
    keys = tuple(k for k in keys if k)
    target = keys[0] if keys else "unknown"
    run_id = usage.agent_context().get("run_id")
    if not run_id:
        _audit(None, None, "pam_denied", f"host={target} reason=no active run")
        raise RuntimeError("Credential checkout refused: no active agent run")
    ticket = _run_ticket(run_id)

    why = _allow(run_id)
    if why:
        _audit(run_id, ticket, "pam_rate_limited", f"host={target} {why}")
        raise RuntimeError(f"Credential checkout refused: {why}")

    provider = "?"
    try:
        creds = registry.get_credentials()
        provider = creds.provider
        matched, data = creds.fetch(*keys)
    except NotFound:
        _audit(run_id, ticket, "pam_checkout", f"host={target} result=not_found provider={provider}")
        raise RuntimeError(f"No credential available for {keys[-1] if keys else 'unknown host'}") from None
    except PortError as e:
        _audit(run_id, ticket, "pam_checkout", f"host={target} result=unavailable provider={provider}")
        raise RuntimeError(f"PAM is unavailable ({e})") from None
    _audit(run_id, ticket, "pam_checkout", f"host={target} key={matched} result=ok provider={provider}")
    return data


_inquiry_recent: deque = deque()                 # timestamps of every inquiry checkout in the last hour
_inquiry_per_user: OrderedDict = OrderedDict()   # user_id -> checkouts so far (newest 200 users kept)


def _allow_inquiry(user_id: str):
    """None when this inquiry checkout may go ahead, else the reason it may not. Separate buckets
    from _allow/_recent/_per_run so inquiry traffic can never borrow or starve a ticket run's PAM budget."""
    now = time.time()
    with _lock:
        while _inquiry_recent and now - _inquiry_recent[0] > 3600:
            _inquiry_recent.popleft()
        if len(_inquiry_recent) >= config.get("credentials.inquiry_max_per_hour"):
            return "hourly inquiry limit reached"
        if _inquiry_per_user.get(user_id, 0) >= config.get("credentials.inquiry_max_per_user_per_hour"):
            return "per-user inquiry limit reached"
        _inquiry_recent.append(now)
        _inquiry_per_user[user_id] = _inquiry_per_user.get(user_id, 0) + 1
        _inquiry_per_user.move_to_end(user_id)
        while len(_inquiry_per_user) > 200:
            _inquiry_per_user.popitem(last=False)
    return None


_action_per_user: OrderedDict = OrderedDict()    # user_id -> [(first time, ticket, fetches)] in the last hour


def _allow_action(user_id: str, ticket: str):
    """One confirmed chat action = one ticket, even though a service restart fetches the credential three times
    (state before, restart, state after). So the hourly limit counts distinct tickets, and a single ticket may fetch
    at most 30 times so it cannot be used as a loophole."""
    now = time.time()
    with _lock:
        seen = [(t, tk, n) for (t, tk, n) in _action_per_user.get(user_id, []) if now - t < 3600]
        mine = [e for e in seen if e[1] == ticket]
        if not mine and len({e[1] for e in seen}) >= config.get("credentials.action_max_per_user_per_hour"):
            _action_per_user[user_id] = seen
            return "per-user action limit reached"
        if mine and mine[0][2] >= 30:
            _action_per_user[user_id] = seen
            return "too many credential fetches for this ticket"
        if mine:
            seen = [(t, tk, n + 1) if tk == ticket else (t, tk, n) for (t, tk, n) in seen]
        else:
            seen.append((now, ticket, 1))
        _action_per_user[user_id] = seen
        _action_per_user.move_to_end(user_id)
        while len(_action_per_user) > 200:
            _action_per_user.popitem(last=False)
    return None


@telemetry.traced("credential_store", "checkout_for_action")
def checkout_for_action(user_id: str, ticket: str, *keys: str) -> dict:
    """Credential fetch for a change a person confirmed in the approver chat (no agent run). Own hourly limit per
    person, and every checkout is audited under THEIR name with the ticket. Raises RuntimeError (safe to show)."""
    keys = tuple(k for k in keys if k)
    target = keys[0] if keys else "unknown"

    def audit(action, detail):
        try:
            from app import store
            store.audit(None, ticket, user_id, action, detail)
        except Exception as e:                    # noqa: BLE001
            log.error("PAM audit row not written (%s): %s %s", type(e).__name__, action, detail)

    why = _allow_action(user_id, ticket)
    if why:
        audit("pam_action_rate_limited", f"host={target} {why}")
        raise RuntimeError(f"Credential checkout refused: {why}")
    provider = "?"
    try:
        creds = registry.get_credentials()
        provider = creds.provider
        matched, data = creds.fetch(*keys)
    except NotFound:
        audit("pam_action_checkout", f"host={target} result=not_found provider={provider}")
        raise RuntimeError(f"No credential available for {keys[-1] if keys else 'unknown host'}") from None
    except PortError as e:
        audit("pam_action_checkout", f"host={target} result=unavailable provider={provider}")
        raise RuntimeError(f"PAM is unavailable ({e})") from None
    audit("pam_action_checkout", f"host={target} key={matched} result=ok provider={provider}")
    return data


@telemetry.traced("credential_store", "checkout_for_inquiry")
def checkout_for_inquiry(user_id: str, *keys: str) -> dict:
    """Read-only credential fetch with no agent run: used only by /api/approver-chat/inquire.
    Rate-limited separately from checkout() so inquiry traffic never shares or depletes a
    ticket run's PAM budget. Raises RuntimeError with a message that is safe to show."""
    keys = tuple(k for k in keys if k)
    target = keys[0] if keys else "unknown"

    why = _allow_inquiry(user_id)
    if why:
        _audit(None, None, "pam_inquiry_rate_limited", f"host={target} user={user_id} {why}")
        raise RuntimeError(f"Credential checkout refused: {why}")

    provider = "?"
    try:
        creds = registry.get_credentials()
        provider = creds.provider
        matched, data = creds.fetch(*keys)
    except NotFound:
        _audit(None, None, "pam_inquiry_checkout", f"host={target} user={user_id} result=not_found provider={provider}")
        raise RuntimeError(f"No credential available for {keys[-1] if keys else 'unknown host'}") from None
    except PortError as e:
        _audit(None, None, "pam_inquiry_checkout", f"host={target} user={user_id} result=unavailable provider={provider}")
        raise RuntimeError(f"PAM is unavailable ({e})") from None
    _audit(None, None, "pam_inquiry_checkout", f"host={target} user={user_id} key={matched} result=ok provider={provider}")
    return data