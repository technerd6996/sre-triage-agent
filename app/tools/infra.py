"""Infrastructure tools for the triage agent: Windows over WinRM, Linux over SSH.

Tools take target="windows" or "linux" (hosts come from .env), never a free-form host.
Tools return {"error": ...} instead of raising, so the agent can read the failure and escalate.
Decisions (threshold checks, resolve vs escalate) are made in code and returned as "next_step",
so a small model only has to follow the instruction.
"""
import json
import os
import re
import socket
import time

import paramiko
import winrm
from langchain_core.tools import tool

from app.tools import aws

TARGETS = ("windows", "linux")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")  # blocks shell/PowerShell injection

# Disk verification state (one ticket is worked at a time, so module-level state is safe)
_CLEARED: dict[str, float] = {}   # target -> when temp files were last cleared
_BEFORE: dict[str, dict] = {}     # target -> {disk: used_pct} before the cleanup
VERIFY_WINDOW = 900               # seconds in which a disk check counts as the post-cleanup check

_ERR_STEP = "Call add_work_note with this error, then call escalate_incident."


# ---- config ----
def _threshold() -> float:
    return float(os.getenv("DISK_THRESHOLD_PCT", "85"))


def _service_allowlist() -> set[str]:
    raw = os.getenv("SERVICE_ALLOWLIST",
                    "nginx,docker,postgresql,postgres,sshd,ssh,apache2,httpd,mysql,mysqld,redis,redis-server,cron")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


# ---- low-level helpers ----
def _host(target: str) -> str:
    if target not in TARGETS:
        raise ValueError(f"target must be one of {TARGETS}")
    ip = aws.current_host(target)  # the instance the current ticket is about, via AWS
    if ip:
        return ip
    if aws.STRICT:
        raise RuntimeError("No server was identified for this ticket, so no server check was run.")
    host = os.getenv("WIN_HOST" if target == "windows" else "LINUX_HOST", "")
    if not host:
        raise RuntimeError(f"No host configured for '{target}' in .env")
    return host


def _port(target: str) -> int:
    return 5985 if target == "windows" else int(os.getenv("LINUX_SSH_PORT", "22"))


def _win(script: str) -> str:
    """Run a PowerShell script on the Windows server over WinRM and return stdout."""
    session = winrm.Session(
        f"http://{_host('windows')}:5985/wsman",
        auth=(os.environ["WIN_USER"], os.environ["WIN_PASSWORD"]),
        transport=os.getenv("WIN_WINRM_TRANSPORT", "ntlm"),
        operation_timeout_sec=20,
        read_timeout_sec=30,
    )
    r = session.run_ps(script)
    if r.status_code != 0:
        raise RuntimeError(r.std_err.decode(errors="replace")[:300])
    return r.std_out.decode(errors="replace").strip()


def _linux(command: str) -> str:
    """Run a shell command on the Linux server over SSH and return stdout."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            _host("linux"),
            port=_port("linux"),
            username=os.environ["LINUX_USER"],
            password=os.getenv("LINUX_PASSWORD") or None,
            key_filename=os.getenv("LINUX_KEY_PATH") or None,
            timeout=10,
            look_for_keys=False,
            allow_agent=False,
        )
        _, stdout, stderr = client.exec_command(command, timeout=30)
        out = stdout.read().decode(errors="replace").strip()
        code = stdout.channel.recv_exit_status()
        if code != 0:
            raise RuntimeError(stderr.read().decode(errors="replace")[:300] or f"exit code {code}")
        return out
    finally:
        client.close()


def _safe(value: str, what: str) -> str:
    if not _SAFE_NAME.match(value or ""):
        raise ValueError(f"Invalid {what}: only letters, digits, '.', '_' and '-' are allowed")
    return value


_WIN_SVC = ("Get-Service -Name '{svc}' | Select-Object Name,"
            "@{{n='Status';e={{\"$($_.Status)\"}}}} | ConvertTo-Json -Compress")


def _service_state(target: str, svc: str) -> str:
    """'active' when running, else the raw state (inactive, failed, stopped, unknown...)."""
    if target == "windows":
        data = json.loads(_win(_WIN_SVC.format(svc=svc)))
        s = str(data.get("Status", "")).lower()
        return "active" if s == "running" else (s or "unknown")
    # systemctl is-active exits non-zero for a stopped service, so '|| true' keeps the state text
    return (_linux(f"systemctl is-active {svc} || true") or "unknown").strip().lower()


# ---- read-only tools ----
@tool
def check_reachability(target: str) -> dict:
    """Check whether a server is reachable. target is 'windows' or 'linux'.
    Tests the management port (WinRM 5985 for windows, SSH for linux)."""
    try:
        host, port = _host(target), _port(target)
        with socket.create_connection((host, port), timeout=5):
            pass
        return {"target": target, "reachable": True, "port": port}
    except Exception as e:
        return {"target": target, "reachable": False, "error": str(e)[:200],
                "next_step": "The server is not reachable. Call add_work_note, then call escalate_incident."}


@tool
def check_disk_usage(target: str) -> dict:
    """Get disk usage for a server. target is 'windows' or 'linux'. Returns each disk with percent used
    and a next_step to follow."""
    try:
        disks = []
        if target == "windows":
            script = (
                "$d = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | "
                "Select-Object DeviceID,"
                "@{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}},"
                "@{n='FreeGB';e={[math]::Round($_.FreeSpace/1GB,1)}}); "
                "ConvertTo-Json -InputObject $d -Compress"
            )
            for d in json.loads(_win(script)):
                size, free = d["SizeGB"], d["FreeGB"]
                disks.append(
                    {"disk": d["DeviceID"], "size_gb": size, "free_gb": free,
                     "used_pct": round(100 * (size - free) / size, 1) if size else None}
                )
        else:
            out = _linux("df -P -x tmpfs -x devtmpfs | tail -n +2")
            for line in out.splitlines():
                p = line.split()
                if len(p) >= 6:
                    disks.append(
                        {"disk": p[5], "size_gb": round(int(p[1]) / 1048576, 1),
                         "free_gb": round(int(p[3]) / 1048576, 1), "used_pct": float(p[4].rstrip("%"))}
                    )
        if not disks:
            raise RuntimeError("No disks were returned by the server.")

        thr = _threshold()
        for d in disks:
            d["over_threshold"] = d["used_pct"] is not None and d["used_pct"] > thr
        over = [d["disk"] for d in disks if d["over_threshold"]]
        res = {"target": target, "threshold_pct": thr, "disks": disks, "over_threshold": over}

        if time.time() - _CLEARED.get(target, 0) < VERIFY_WINDOW:   # this is the post-cleanup check
            _CLEARED.pop(target, None)
            res["before_used_pct"] = _BEFORE.get(target, {})
            if over:
                res["next_step"] = (f"Still over the {thr}% threshold after cleanup ({', '.join(over)}). "
                                    "Call add_work_note with the before and after used_pct, then call escalate_incident.")
            else:
                res["next_step"] = ("Cleanup verified: every disk is now under the threshold. Call add_work_note "
                                    "with the before and after used_pct, then call resolve_incident.")
        else:
            _BEFORE[target] = {d["disk"]: d["used_pct"] for d in disks}
            if over:
                res["next_step"] = (f"Over the {thr}% threshold: {', '.join(over)}. Call clear_temp_files with "
                                    f"target '{target}' (needs engineer approval).")
            else:
                res["next_step"] = ("No disk is over the threshold. Call add_work_note 'no disk issue found', "
                                    "then call resolve_incident.")
        return res
    except Exception as e:
        return {"target": target, "error": str(e)[:300], "next_step": _ERR_STEP}


@tool
def check_service_status(target: str, service_name: str) -> dict:
    """Check whether a service is running. target is 'windows' or 'linux'. Read-only.
    Returns the status and a next_step to follow."""
    try:
        if target not in TARGETS:
            raise ValueError(f"target must be one of {TARGETS}")
        svc = _safe(service_name, "service_name")
        status = _service_state(target, svc)
        if status == "active":
            step = (f"{svc} is already running. Call add_work_note with this result, then call resolve_incident.")
        else:
            step = (f"{svc} is not running (status: {status}). Call start_service with target '{target}' and "
                    f"service_name '{svc}' (needs engineer approval).")
        return {"target": target, "service": svc, "status": status, "next_step": step}
    except Exception as e:
        return {"target": target, "service": service_name, "error": str(e)[:300], "next_step": _ERR_STEP}


@tool
def lookup_ad_user(username: str) -> dict:
    """Look up an Active Directory user by username (sAMAccountName). Returns whether the account is
    enabled, locked out, or has an expired password, and a next_step to follow. Use it for access issues."""
    try:
        u = _safe(username, "username")
        script = (
            "Import-Module ActiveDirectory; "
            f"Get-ADUser -Identity '{u}' -Properties Enabled,LockedOut,PasswordExpired,LastLogonDate | "
            "Select-Object SamAccountName,Enabled,LockedOut,PasswordExpired,"
            "@{n='LastLogon';e={\"$($_.LastLogonDate)\"}} | ConvertTo-Json -Compress"
        )
        res = json.loads(_win(script))
        if not res.get("Enabled"):
            step = "The account is disabled and needs L2 review. Call add_work_note, then call escalate_incident."
        elif res.get("LockedOut"):
            step = (f"The account is locked out. Call unlock_ad_account with username '{u}' "
                    "(needs engineer approval). Do not resolve the ticket yet.")
        elif res.get("PasswordExpired"):
            step = "The password is expired, not locked. Call add_work_note, then call escalate_incident."
        else:
            step = "The account is enabled and not locked out. Call add_work_note with this, then call resolve_incident."
        res["next_step"] = step
        return res
    except Exception as e:
        return {"username": username, "error": str(e)[:300], "next_step": _ERR_STEP}


# ---- writes (these need human approval, see policy.py) ----
@tool
def clear_temp_files(target: str) -> dict:
    """Delete temp files older than 1 day on a server to free disk space. target is 'windows' or 'linux'.
    Needs engineer approval. Afterwards call check_disk_usage to verify."""
    try:
        if target == "windows":
            script = (
                "$f = @(Get-ChildItem -Path $env:TEMP,'C:\\Windows\\Temp' -Recurse -Force -File "
                "-ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) }); "
                "$b = ($f | Measure-Object Length -Sum).Sum; "
                "$f | Remove-Item -Force -ErrorAction SilentlyContinue; "
                "Write-Output ([math]::Round($b/1MB,1))"
            )
            res = {"target": target, "freed_mb": float(_win(script) or 0)}
        else:
            out = _linux("find /tmp -xdev -type f -mtime +1 -delete -print | wc -l")
            res = {"target": target, "files_deleted": int(out or 0)}
        _CLEARED[target] = time.time()
        res["next_step"] = f"Cleanup done. Call check_disk_usage with target '{target}' to verify."
        return res
    except Exception as e:
        return {"target": target, "error": str(e)[:300], "next_step": _ERR_STEP}


@tool
def start_service(target: str, service_name: str) -> dict:
    """Start a stopped service. target is 'windows' or 'linux'. Needs engineer approval.
    Returns the new status and a next_step to follow."""
    try:
        if target not in TARGETS:
            raise ValueError(f"target must be one of {TARGETS}")
        svc = _safe(service_name, "service_name")
        if svc.lower() not in _service_allowlist():
            raise ValueError(f"Service '{svc}' is not on the allowed list (SERVICE_ALLOWLIST).")
        if target == "windows":
            _win(f"Start-Service -Name '{svc}'")
        else:
            _linux(f"sudo -n systemctl start {svc}")
        status = _service_state(target, svc)
        if status == "active":
            step = (f"{svc} is now running. Call add_work_note with before and after status, "
                    "then call resolve_incident.")
        else:
            step = (f"{svc} is still not running (status: {status}). Call add_work_note with this, "
                    "then call escalate_incident.")
        return {"target": target, "service": svc, "status": status, "next_step": step}
    except Exception as e:
        return {"target": target, "service": service_name, "error": str(e)[:300], "next_step": _ERR_STEP}


@tool
def unlock_ad_account(username: str) -> dict:
    """Unlock a locked Active Directory account. Needs engineer approval before it runs."""
    try:
        u = _safe(username, "username")
        script = (
            "Import-Module ActiveDirectory; "
            f"Unlock-ADAccount -Identity '{u}'; "
            f"Get-ADUser -Identity '{u}' -Properties LockedOut | "
            "Select-Object SamAccountName,LockedOut | ConvertTo-Json -Compress"
        )
        res = json.loads(_win(script))
        if res.get("LockedOut"):
            res["next_step"] = "The account is still locked. Call add_work_note, then call escalate_incident."
        else:
            res["next_step"] = ("The account is now unlocked. Call add_work_note with before and after "
                                "LockedOut values, then call resolve_incident.")
        return res
    except Exception as e:
        return {"username": username, "error": str(e)[:300], "next_step": _ERR_STEP}


INFRA_TOOLS = [check_reachability, check_disk_usage, check_service_status, lookup_ad_user,
               clear_temp_files, start_service, unlock_ad_account]
HIGH_RISK_TOOLS = {"unlock_ad_account"}