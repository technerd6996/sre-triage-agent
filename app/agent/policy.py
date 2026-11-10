"""Risk policy. Enforced in code by the approval middleware, never by the prompt.
L0 low: automatic. L1 medium: human approval. L2 high: human approval. L3 critical: no autonomous action."""
import os
import time

from app import config, store

DEFAULT_RISK = {
    # L0: read-only and ticket bookkeeping run automatically
    "get_incident": 0, "claim_incident": 0, "add_work_note": 0, "create_incident": 0,
    "resolve_incident": 0, "escalate_incident": 0,
    "check_reachability": 0, "check_disk_usage": 0, "check_service_status": 0, "lookup_ad_user": 0,
    "check_ec2_status": 0, "check_security_group": 0, "get_cloudwatch_metric": 0,
    "check_memory_usage": 0, "check_cpu_usage": 0, "top_processes": 0,
    "check_oom_events": 0, "get_service_logs": 0, "find_large_files": 0,
    # approver-chat-only read-only checks (app/tools/chat_tools.py; the agent does not have them)
    "check_uptime": 0, "check_pending_reboot": 0, "check_network": 0, "get_event_errors": 0, "list_sessions": 0,
    "list_failed_services": 0, "find_services": 0, "list_scheduled_jobs": 0, "check_windows_updates": 0, "list_packages": 0,
    "list_upgradable": 0, "list_security_updates": 0,
    "escalate_l3": 0,  # notification only; no remediation happens at L3
    # L1: standard operations need a human
    "clear_temp_files": 1, "start_service": 1, "unlock_ad_account": 1,
    "restart_service": 1, "clean_old_logs": 1, "kill_process": 2,
    # approver-chat-only changes (app/tools/chat_changes.py; the agent does not have them)
    "stop_service": 1, "set_service_startup": 1, "logoff_session": 1, "run_scheduled_job": 1, "install_packages": 1,
    # these three need the owners' email approval first (app/owner_approval.py), then the engineer's Confirm
    "reboot_server": 2, "shutdown_server": 2, "remove_packages": 2, "install_windows_updates": 2, "check_patch_job": 0,
}


def _to_text(table: dict) -> str:
    return chr(10).join(f"{k} = {v}" for k, v in table.items())


def _parse_table(text: str) -> dict:
    """'tool = level' per line (blank lines and # comments allowed). Raises ValueError with a message for the admin."""
    out = {}
    for n, raw in enumerate(str(text).splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"line {n}: expected 'tool = level'")
        k, v = (x.strip() for x in line.split("=", 1))
        if not k.replace("_", "").isalnum():
            raise ValueError(f"line {n}: '{k}' is not a tool name")
        if v not in ("0", "1", "2", "3"):
            raise ValueError(f"line {n}: level for {k} must be 0, 1, 2 or 3")
        out[k] = int(v)
    if not out:
        raise ValueError("the table cannot be empty")
    return out


def _check_table(text: str) -> None:
    _parse_table(text)


config.register("policy.risk", "Tool risk levels (tool = level, one per line; unlisted tools are L2)", _to_text(DEFAULT_RISK),
                "text", "policy", multiline=True, max_len=20000, check=_check_table,
                help="L0 automatic, L1 medium, L2 high, L3 critical. Levels at or above the approval setting wait for a human.")

_table_cache: tuple = ("", {})


def risk_table() -> dict:
    """The tool -> level table from the admin setting (the built-in defaults if the setting is unreadable)."""
    global _table_cache
    try:
        text = config.get("policy.risk")
    except Exception:
        return dict(DEFAULT_RISK)
    if _table_cache[0] != text:
        try:
            _table_cache = (text, _parse_table(text))
        except ValueError:
            return dict(DEFAULT_RISK)
    return _table_cache[1]


def risk_level(tool_name: str) -> int:
    # A tool missing from the table is treated as high risk (L2)
    return risk_table().get(tool_name, 2)


def needs_approval(tool_name: str) -> bool:
    return risk_level(tool_name) >= config.get("policy.approval_from_level")


def interrupt_config(tools) -> dict:
    return {t.name: True for t in tools if needs_approval(t.name)}


_RESTORES: dict[str, list[float]] = {}


def autonomy_mode() -> str:
    return store.get_setting("autonomy_mode", "shadow")          # off | shadow | on


def _never_auto() -> set[str]:
    """Optional: services that must always go to a human. Empty by default."""
    return {s.strip().lower() for s in config.get("agent.never_auto_services").split(",") if s.strip()}


def autonomous_restore(actions, host: str = "") -> tuple[bool, str]:
    """(run without a human?, reason). Only a single start_service on a critical service that is really stopped."""
    mode = autonomy_mode()
    if mode == "off" or len(actions) != 1 or actions[0].get("name") != "start_service":
        return False, ""
    args = actions[0].get("args") or {}
    svc, target = str(args.get("service_name", "")).lower(), args.get("target", "")
    if svc in _never_auto():
        return False, f"{svc} is in NEVER_AUTO_SERVICES, needs a human"
    from app.ports import registry                            # lazy import avoids a circular import
    _, crit_why = registry.get_context().is_critical(host, svc)   # context for the note only, not a gate
    crit_why = crit_why or "stopped service, preflight and flap checks passed"
    now = time.time()
    recent = [t for t in _RESTORES.get(svc, []) if now - t < 3600]
    if len(recent) >= config.get("limits.flap_limit"):
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
    return True, f"auto-restoring {svc} on {target} ({crit_why})"