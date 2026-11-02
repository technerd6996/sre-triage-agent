"""Replay world: a small simulated estate that sits *under* the real infra tools.

The real tools (check_service_status, start_service, check_disk_usage, ...) run unchanged, so their
parsing and their next_step wording are exercised. Only what touches the outside is replaced:
the PowerShell and SSH runners, the socket check, and the ticket adapter.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from types import SimpleNamespace
from unittest import mock

L1_GROUP = "L1-SRE-Agent"
L2_GROUP = "L2-Support"
CLOSED = {"Resolved", "Closed", "Canceled"}


def threshold() -> float:
    return float(os.getenv("DISK_THRESHOLD_PCT", "85"))


class World:
    def __init__(self, case: dict):
        self.case = case
        host = case.get("host") or {}
        self.platform = host.get("platform")
        self.reachable = host.get("reachable", True)
        w = case.get("world") or {}
        self.services = {k.lower(): {"name": k, **v} for k, v in (w.get("services") or {}).items()}
        self.disks = [dict(d) for d in (w.get("disks") or [])]
        self.users = {k.lower(): dict(v) for k, v in (w.get("users") or {}).items()}
        self.number = case.get("number", "INC9990001")
        self.state, self.group, self.assignee = "New", L1_GROUP, ""
        self.notes: list[str] = []
        self.close_notes = ""
        self.outcome: str | None = None          # resolved | escalated | None
        self.truth_at_close: bool | None = None  # was the goal really met when the agent resolved?

    # ---- lookups ----
    @staticmethod
    def _arg(script: str, pattern: str) -> str:
        m = re.search(pattern, script)
        if not m:
            raise RuntimeError("replay world: could not read the name from the command")
        return m.group(1)

    def _svc(self, name: str) -> dict:
        s = self.services.get(str(name).lower())
        if s is None:
            raise RuntimeError(f"Cannot find any service with service name '{name}'.")
        return s

    def _user(self, name: str) -> dict:
        u = self.users.get(str(name).lower())
        if u is None:
            raise RuntimeError(f"Cannot find an object with identity: '{name}'.")
        return u

    def _cleanup(self) -> float:
        freed = 0.0
        for d in self.disks:
            f = min(d.get("frees_on_cleanup", 0), d["used_gb"])
            d["used_gb"] -= f
            d["frees_on_cleanup"] = 0
            freed += f
        return freed

    @staticmethod
    def used_pct(d: dict) -> float:
        return 100 * d["used_gb"] / d["size_gb"]

    # ---- the PowerShell runner ----
    def win(self, script: str) -> str:
        if not self.reachable:
            raise RuntimeError("WinRM connection timed out: host unreachable")
        s = script
        if "Unlock-ADAccount" in s:                      # also contains Get-ADUser, so check first
            name = self._arg(s, r"Unlock-ADAccount -Identity '([^']+)'")
            u = self._user(name)
            if u.get("unlock_works", True):
                u["locked"] = False
            return json.dumps({"SamAccountName": name, "LockedOut": bool(u.get("locked"))})
        if "Get-ADUser" in s:
            name = self._arg(s, r"Get-ADUser -Identity '([^']+)'")
            u = self._user(name)
            return json.dumps({"SamAccountName": name, "Enabled": u.get("enabled", True),
                               "LockedOut": bool(u.get("locked")), "PasswordExpired": bool(u.get("expired")),
                               "LastLogon": "10/26/2026 09:12:00"})
        if "Start-Service" in s:
            svc = self._svc(self._arg(s, r"Start-Service -Name '([^']+)'"))
            if svc.get("start_works", True):
                svc["running"] = True
            return ""
        if "Get-Service" in s:
            svc = self._svc(self._arg(s, r"Get-Service -Name '([^']+)'"))
            return json.dumps({"Name": svc["name"], "Status": "Running" if svc.get("running") else "Stopped"})
        if "Win32_LogicalDisk" in s:
            return json.dumps([{"DeviceID": d["id"], "SizeGB": d["size_gb"],
                                "FreeGB": round(d["size_gb"] - d["used_gb"], 1)} for d in self.disks])
        if "Remove-Item" in s:
            return str(round(self._cleanup() * 1024, 1))
        raise RuntimeError("replay world: this PowerShell is not simulated")

    # ---- the SSH runner ----
    def linux(self, cmd: str) -> str:
        if not self.reachable:
            raise RuntimeError("SSH connection timed out: host unreachable")
        m = re.fullmatch(r"systemctl is-active (\S+) \|\| true", cmd)
        if m:
            svc = self.services.get(m.group(1).lower())
            if svc is None or svc.get("running"):
                return "active" if svc else "inactive"
            return svc.get("stopped_as", "inactive")
        m = re.fullmatch(r"sudo -n systemctl start (\S+)", cmd)
        if m:
            svc = self._svc(m.group(1))
            if svc.get("start_works", True):
                svc["running"] = True
            return ""
        if cmd.startswith("df -P"):
            rows = []
            for d in self.disks:
                size_kb, used_kb = int(d["size_gb"] * 1048576), int(d["used_gb"] * 1048576)
                rows.append(f"/dev/xvda1 {size_kb} {used_kb} {size_kb - used_kb} "
                            f"{round(self.used_pct(d))}% {d['id']}")
            return "\n".join(rows)
        if cmd.startswith("find /tmp"):
            return str(int(self._cleanup() * 100))
        raise RuntimeError("replay world: this command is not simulated")

    # ---- ground truth ----
    def goal_met(self) -> bool:
        g = self.case.get("goal") or {"type": "unfixable"}
        t = g["type"]
        if t == "service_running":
            return bool(self._svc(g["name"]).get("running"))
        if t == "disks_ok":
            return all(self.used_pct(d) <= threshold() for d in self.disks)
        if t == "account_ok":
            u = self._user(g["name"])
            return bool(u.get("enabled", True) and not u.get("locked") and not u.get("expired"))
        return False                                   # "unfixable": resolving is always wrong

    def apply_after_resolve(self) -> None:
        """Trap cases: the fix does not hold. Used to score checkers later."""
        stop = (self.case.get("after_resolve") or {}).get("stop_service")
        if stop:
            self._svc(stop)["running"] = False

    def closed(self) -> bool:
        return self.state in CLOSED or self.group != L1_GROUP

    def record(self) -> dict:
        return {"number": self.number, "short_description": self.case["short_description"],
                "description": self.case.get("description", ""), "state": self.state,
                "assignment_group": self.group, "priority": "3", "caller_id": "replay.user",
                "assigned_to": self.assignee, "work_notes": "\n".join(self.notes)}

    def snapshot(self) -> dict:
        return {"services": {s["name"]: bool(s.get("running")) for s in self.services.values()},
                "disks": {d["id"]: round(self.used_pct(d), 1) for d in self.disks},
                "users": {k: dict(v) for k, v in self.users.items()}}


class FakeTicketing:
    """Just the Ticketing methods the agent tools call."""

    def __init__(self, world: World):
        self.w = world

    def _t(self):
        return SimpleNamespace(raw=self.w.record())

    def get(self, number):
        return self._t()

    def claim(self, number, note=None):
        self.w.state, self.w.assignee = "In Progress", "svc_triage_01"
        if note:
            self.w.notes.append(note)
        return self._t()

    def add_note(self, number, note):
        self.w.notes.append(note)

    def resolve(self, number, close_notes):
        self.w.truth_at_close = self.w.goal_met()
        self.w.state, self.w.outcome, self.w.close_notes = "Resolved", "resolved", close_notes
        self.w.apply_after_resolve()
        return self._t()

    def escalate(self, number, reason):
        self.w.group, self.w.assignee, self.w.outcome = L2_GROUP, "", "escalated"
        self.w.notes.append(f"Escalated: {reason}")
        return self._t()


def all_tools() -> list:
    from app.tools.infra import INFRA_TOOLS
    from app.tools.ticketing_tools import SNOW_TOOLS
    return list(SNOW_TOOLS) + list(INFRA_TOOLS)


@contextlib.contextmanager
def patched(world: World):
    """Put the world under the real tools for the duration of one run."""
    from app.tools import infra, ticketing_tools
    fake = FakeTicketing(world)

    def host(target: str) -> str:
        if target not in infra.TARGETS:
            raise ValueError(f"target must be one of {infra.TARGETS}")
        if target != world.platform:
            raise RuntimeError("No server was identified for this ticket, so no server check was run.")
        return "10.0.0.5"

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def create_connection(addr, timeout=None):
        if not world.reachable:
            raise OSError("timed out")
        return _Conn()

    sock = SimpleNamespace(create_connection=create_connection)
    os.environ["DISK_THRESHOLD_PCT"] = os.environ.get("DISK_THRESHOLD_PCT", "85")
    def win(script: str) -> str:
        host("windows")          # same as the real _win: resolve the host first
        return world.win(script)

    def linux(cmd: str) -> str:
        host("linux")
        return world.linux(cmd)

    with mock.patch.object(infra, "_host", host), \
         mock.patch.object(infra, "_win", win), \
         mock.patch.object(infra, "_linux", linux), \
         mock.patch.object(infra, "socket", sock), \
         mock.patch.object(ticketing_tools, "_tk", lambda: fake):
        infra._CLEARED.clear()
        infra._BEFORE.clear()
        yield