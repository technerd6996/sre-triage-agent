"""Risk policy. Enforced in code by the approval middleware, never by the prompt.
L0 low: automatic. L1 medium: human approval. L2 high: human approval. L3 critical: no autonomous action."""

RISK = {
    # L0: read-only and ticket bookkeeping run automatically
    "get_incident": 0, "claim_incident": 0, "add_work_note": 0, "create_incident": 0,
    "resolve_incident": 0, "escalate_incident": 0,
    "check_reachability": 0, "check_disk_usage": 0, "lookup_ad_user": 0,
    "check_ec2_status": 0, "check_security_group": 0, "get_cloudwatch_metric": 0,
    # L1: standard operations need a human
    "clear_temp_files": 1, "unlock_ad_account": 1,
}
APPROVAL_FROM_LEVEL = 1


def needs_approval(tool_name: str) -> bool:
    # A tool missing from the table is treated as high risk (L2)
    return RISK.get(tool_name, 2) >= APPROVAL_FROM_LEVEL


def interrupt_config(tools) -> dict:
    return {t.name: True for t in tools if needs_approval(t.name)}