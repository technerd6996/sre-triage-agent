"""Infrastructure tools for the triage agent: Windows over WinRM, Linux over SSH.

Tools take target="windows" or "linux" (hosts come from .env), never a free-form host.
Tools return {"error": ...} instead of raising, so the agent can read the failure and escalate.
"""
import json
import os
import re
import socket

import paramiko
import winrm
from langchain_core.tools import tool

from app.tools import aws

TARGETS = ("windows", "linux")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")  # blocks shell/PowerShell injection


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
        return {"target": target, "reachable": False, "error": str(e)[:200]}


@tool
def check_disk_usage(target: str) -> dict:
    """Get disk usage for a server. target is 'windows' or 'linux'. Returns each disk with percent used."""
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
        return {"target": target, "disks": disks}
    except Exception as e:
        return {"target": target, "error": str(e)[:300]}


@tool
def lookup_ad_user(username: str) -> dict:
    """Look up an Active Directory user by username (sAMAccountName). Returns whether the account is
    enabled, locked out, or has an expired password. Use it for access issues."""
    try:
        u = _safe(username, "username")
        script = (
            "Import-Module ActiveDirectory; "
            f"Get-ADUser -Identity '{u}' -Properties Enabled,LockedOut,PasswordExpired,LastLogonDate | "
            "Select-Object SamAccountName,Enabled,LockedOut,PasswordExpired,"
            "@{n='LastLogon';e={\"$($_.LastLogonDate)\"}} | ConvertTo-Json -Compress"
        )
        return json.loads(_win(script))
    except Exception as e:
        return {"username": username, "error": str(e)[:300]}


# ---- low-risk write ----
@tool
def clear_temp_files(target: str) -> dict:
    """Delete temp files older than 1 day on a server to free disk space. target is 'windows' or 'linux'.
    Safe and low risk. Run check_disk_usage afterwards to verify."""
    try:
        if target == "windows":
            script = (
                "$f = @(Get-ChildItem -Path $env:TEMP,'C:\\Windows\\Temp' -Recurse -Force -File "
                "-ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) }); "
                "$b = ($f | Measure-Object Length -Sum).Sum; "
                "$f | Remove-Item -Force -ErrorAction SilentlyContinue; "
                "Write-Output ([math]::Round($b/1MB,1))"
            )
            return {"target": target, "freed_mb": float(_win(script) or 0)}
        out = _linux("find /tmp -xdev -type f -mtime +1 -delete -print | wc -l")
        return {"target": target, "files_deleted": int(out or 0)}
    except Exception as e:
        return {"target": target, "error": str(e)[:300]}

@tool
def manage_service(target: str, service_name: str, action: str) -> dict:
    """Manage a system service (start, stop, restart, status). target is 'windows' or 'linux'.
    action must be 'start', 'stop', 'restart', or 'status'."""
    try:
        svc = _safe(service_name, "service_name")
        act = action.lower()
        if act not in ("start", "stop", "restart", "status"):
            raise ValueError("action must be start, stop, restart, or status")

        if target == "windows":
            script = f"Get-Service -Name '{svc}' | Select-Object Name,Status | ConvertTo-Json -Compress"
            if act != "status":
                script = f"Restart-Service -Name '{svc}' -Force; " + script if act == "restart" else f"Set-Service -Name '{svc}' -StartupType Automatic; Start-Service -Name '{svc}'; " + script
            return json.loads(_win(script))
        else:
            if act == "status":
                out = _linux(f"systemctl is-active {svc}")
                return {"service": svc, "status": out.strip()}
            elif act == "restart":
                _linux(f"sudo systemctl restart {svc}")
            elif act == "start":
                _linux(f"sudo systemctl start {svc}")
            elif act == "stop":
                _linux(f"sudo systemctl stop {svc}")
            out = _linux(f"systemctl is-active {svc}")
            return {"service": svc, "status": out.strip()}
    except Exception as e:
        return {"service": service_name, "error": str(e)[:300]}

# ---- high risk: the agent must get human approval first (enforced in Phase 4) ----
@tool
def unlock_ad_account(username: str) -> dict:
    """Unlock a locked Active Directory account. HIGH RISK: requires human approval before it runs."""
    try:
        u = _safe(username, "username")
        script = (
            "Import-Module ActiveDirectory; "
            f"Unlock-ADAccount -Identity '{u}'; "
            f"Get-ADUser -Identity '{u}' -Properties LockedOut | "
            "Select-Object SamAccountName,LockedOut | ConvertTo-Json -Compress"
        )
        return json.loads(_win(script))
    except Exception as e:
        return {"username": username, "error": str(e)[:300]}


INFRA_TOOLS = [check_reachability, check_disk_usage, lookup_ad_user, clear_temp_files, unlock_ad_account]
HIGH_RISK_TOOLS = {"unlock_ad_account"}