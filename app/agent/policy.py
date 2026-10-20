"""Risk policy. Enforced in code by the approval middleware, never by the prompt.
L0 low: automatic. L1 medium: human approval. L2 high: human approval. L3 critical: no autonomous action."""

RISK = {
    # L0: read-only and ticket bookkeeping run automatically
    "get_incident": 0, "claim_incident": 0, "add_work_note": 0, "create_incident": 0,
    "resolve_incident": 0, "escalate_incident": 0,
    "check_reachability": 0, "check_disk_usage": 0, "check_service_status": 0, "lookup_ad_user": 0,
    "check_ec2_status": 0, "check_security_group": 0, "get_cloudwatch_metric": 0,
    "escalate_l3": 0,  # notification only; no remediation happens at L3
    # L1: standard operations need a human
    "clear_temp_files": 1, "start_service": 1, "unlock_ad_account": 1,
}
APPROVAL_FROM_LEVEL = 1


def needs_approval(tool_name: str) -> bool:
    # A tool missing from the table is treated as high risk (L2)
    return RISK.get(tool_name, 2) >= APPROVAL_FROM_LEVEL


def interrupt_config(tools) -> dict:
    return {t.name: True for t in tools if needs_approval(t.name)}

import os
import time
from app import store

FLAP_LIMIT = 3
_RESTORES: dict[str, list[float]] = {}


def autonomy_mode() -> str:
    return store.get_setting("autonomy_mode", "shadow")          # off | shadow | on


def _critical() -> set[str]:
    return {s.strip().lower() for s in os.getenv("CRITICAL_SERVICES", "").split(",") if s.strip()}


def autonomous_restore(actions, host: str = "") -> tuple[bool, str]:
    """(run without a human?, reason). Only a single start_service on a critical service that is really stopped."""
    mode = autonomy_mode()
    if mode == "off" or len(actions) != 1 or actions[0].get("name") != "start_service":
        return False, ""
    args = actions[0].get("args") or {}
    svc, target = str(args.get("service_name", "")).lower(), args.get("target", "")
    if svc in _critical():
        crit_why = "listed in CRITICAL_SERVICES"
    else:
        from app.tools import cmdb                            # lazy import avoids a circular import
        is_crit, crit_why = cmdb.critical_by_cmdb(host, svc)
        if not is_crit:
            return False, ""
    now = time.time()
    recent = [t for t in _RESTORES.get(svc, []) if now - t < 3600]
    if len(recent) >= FLAP_LIMIT:
        return False, f"flap protection: {svc} auto-restored {len(recent)} times in the last hour, needs a human"
    from app.tools.infra import check_service_status          # lazy import avoids a circular import
    res = check_service_status.invoke({"target": target, "service_name": svc})
    if res.get("status") not in ("inactive", "failed", "stopped"):
        return False, f"preflight: {svc} is '{res.get('status') or res.get('error', 'unknown')}', not clearly stopped"
    if svc == "nginx" and target == "linux":
        from app.tools.infra import _linux
        try:
            _linux("sudo -n nginx -t")                        # a broken config would make the restart fail
        except Exception as e:
            return False, f"preflight: nginx config test failed or could not run ({str(e)[:150]}), needs a human"
    if mode == "shadow":
        return False, f"shadow mode: would have auto-restored {svc} on {target} ({crit_why})"
    _RESTORES[svc] = recent + [now]
    return True, f"auto-restoring critical service {svc} on {target} ({crit_why})"