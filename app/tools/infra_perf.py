"""Memory, CPU, process and log tools for the performance playbooks.
Linux over SSH, Windows over WinRM, through the helpers in infra.py.

Same rules as infra.py: target is "windows" or "linux", tools return {"error": ...} instead of
raising, and the decisions (thresholds, what to do next) are made here and returned as "next_step".

next_step depends on who is asking. An L1 run works a ticket (add_work_note, resolve_incident,
escalate_incident). An L2 chat session talks to an engineer and has no ticket to close, so its
wording is different. The L2 chat runner calls set_audience("l2") before it runs the agent. This is
a ContextVar, like active.py, so a chat session and an L1 run never see each other's value.

Read-only (risk 0): check_memory_usage, check_cpu_usage, top_processes, check_oom_events,
                    get_service_logs, find_large_files
Changes things:     restart_service (1), clean_old_logs (1), kill_process (2)
Add all of them to policy.RISK. The agent builds its tool list from PERF_TOOLS.
"""
import contextvars
import json
import os
import posixpath
import re
import time

from langchain_core.tools import tool

from app.tools import infra
from app.tools.infra import TARGETS, _ERR_STEP

# ---------------------------------------------------------------- audience
_AUDIENCE: contextvars.ContextVar = contextvars.ContextVar("triage_audience", default="l1")


def set_audience(value: str):
    """'l1' (ticket run, the default) or 'l2' (engineer chat). Returns a token for reset()."""
    return _AUDIENCE.set("l2" if value == "l2" else "l1")


def _step(l1: str, l2: str) -> str:
    return l2 if _AUDIENCE.get() == "l2" else l1


_L2_NEXT = "Report this result to the engineer in plain words, then continue with the next step of the playbook."
_L2_DONE = "Report the before and after values to the engineer, then ask whether they need anything else."
_L2_ERR = "Tell the engineer about this error and stop. Do not retry."


def _err_step() -> str:
    return _step(_ERR_STEP, _L2_ERR)


# ---------------------------------------------------------------- config
def _mem_limit() -> float:
    return float(os.getenv("MEMORY_THRESHOLD_PCT", "90"))


def _cpu_limit() -> float:
    return float(os.getenv("CPU_THRESHOLD_PCT", "90"))


def _min_share() -> float:
    """A process must use at least this much (percent of RAM, or of all CPUs) to be named as the cause."""
    return float(os.getenv("TOP_PROCESS_MIN_SHARE_PCT", "30"))


def _csv(name: str, default: str) -> list:
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


VERIFY_WINDOW = 900        # seconds in which a check counts as the post-fix check
_FIXED: dict = {}          # target -> when a restart or kill last ran
_BEFORE: dict = {}         # (kind, target) -> value before the fix
_RESTARTS: dict = {}       # (target, service) -> [times]
RESTART_LIMIT_L1 = 1       # an L1 run may restart one service once per hour; an engineer is not limited

# Never killed, and never suggested for restart (svchost hosts many services, so one PID is not one service).
_PROTECTED = {
    "systemd", "systemd-journald", "systemd-logind", "init", "kthreadd", "sshd", "dbus-daemon", "agetty",
    "login", "zabbix_agentd", "zabbix_agent2",
    "system", "idle", "registry", "smss", "csrss", "wininit", "services", "lsass", "winlogon", "svchost",
    "wsmprovhost", "winrshost", "memory compression", "msmpeng", "dwm",
}


def _protected(name: str) -> bool:
    extra = {s.lower() for s in _csv("PROTECTED_PROCESSES", "")}
    base = re.sub(r"\.exe$", "", (name or "").strip().lower())
    base = re.sub(r"#\d+$", "", base)
    return base in _PROTECTED or base in extra


def _check_target(target: str) -> None:
    if target not in TARGETS:
        raise ValueError(f"target must be one of {TARGETS}")


# ---------------------------------------------------------------- parsers (pure, tested without a server)
def parse_meminfo(text: str) -> dict:
    d = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\w+):\s+(\d+)", line)
        if m:
            d[m.group(1)] = int(m.group(2))
    total, avail = d.get("MemTotal"), d.get("MemAvailable")
    if not total or avail is None:
        raise RuntimeError("could not read MemTotal and MemAvailable from /proc/meminfo")
    st, sf = d.get("SwapTotal", 0), d.get("SwapFree", 0)
    return {"total_mb": round(total / 1024), "available_mb": round(avail / 1024),
            "used_pct": round(100 * (1 - avail / total), 1),
            "swap_total_mb": round(st / 1024),
            "swap_used_pct": round(100 * (st - sf) / st, 1) if st else 0.0}


def _stat_vals(line: str):
    n = [int(x) for x in line.split()[1:]]
    return sum(n[:8]), n[3] + (n[4] if len(n) > 4 else 0)      # total, idle + iowait


def parse_cpu(out: str) -> dict:
    """Output of: head -n1 /proc/stat; sleep 2; head -n1 /proc/stat; nproc; cat /proc/loadavg"""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    t1, i1 = _stat_vals(lines[0])
    t2, i2 = _stat_vals(lines[1])
    dt = t2 - t1
    busy = round(100 * (dt - (i2 - i1)) / dt, 1) if dt > 0 else 0.0
    cores = int(lines[2])
    load = [float(x) for x in lines[3].split()[:3]]
    return {"cpu_pct": busy, "cores": cores, "load_avg": load, "load5_per_core": round(load[1] / cores, 2)}


_PS_ROW = re.compile(r"^\s*(\d+)\s+(.+?)\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s*$")


def parse_ps(out: str) -> list:
    rows = []
    for ln in out.splitlines():
        m = _PS_ROW.match(ln)
        if m and m.group(2) not in ("ps", "head", "sort"):
            rows.append({"pid": int(m.group(1)), "name": m.group(2), "mem_pct": float(m.group(3)),
                         "cpu_pct": float(m.group(4)), "mem_mb": round(int(m.group(5)) / 1024)})
    return rows


def parse_cgroups(out: str) -> dict:
    """'<pid> <unit>.service' lines -> {pid: unit}. Login sessions (user@1000.service) do not count."""
    res = {}
    for ln in out.splitlines():
        p = ln.split()
        if len(p) == 2 and p[1].endswith(".service") and not p[1].startswith("user@"):
            res[int(p[0])] = p[1][:-len(".service")]
    return res


# ---------------------------------------------------------------- shared bits
def _post_fix(kind: str, target: str) -> bool:
    """True once, when this check is the one that follows a restart or kill."""
    if time.time() - _FIXED.get(target, 0) < VERIFY_WINDOW:
        _FIXED.pop(target, None)
        return True
    return False


def _fs_used_pct(target: str, path: str):
    try:
        if target == "linux":
            out = infra._linux(f"df -P {path} | tail -n 1").split()
            return float(out[4].rstrip("%"))
        drive = path[0]
        script = (f"$d = Get-PSDrive -Name '{drive}'; "
                  "[math]::Round(100 * $d.Used / ($d.Used + $d.Free), 1)")
        return float(infra._win(script))
    except Exception:
        return None


def _allowed_path(target: str, path: str, env: str, default: str) -> str:
    """Only a path inside one of the allowed folders may be used. The model never gets a free path."""
    path = (path or "").strip()
    if target == "linux":
        norm = posixpath.normpath(path)
        ok = norm.startswith("/") and ".." not in path.split("/") and re.fullmatch(r"[A-Za-z0-9._/-]+", norm)
        allowed = [posixpath.normpath(p) for p in _csv(env + "_LINUX", default)]
        if ok and any(norm == a or norm.startswith(a + "/") for a in allowed):
            return norm
    else:
        norm = path.rstrip("\\")
        ok = re.fullmatch(r"[A-Za-z]:\\[A-Za-z0-9._\\ -]+", norm) and ".." not in norm.split("\\")
        allowed = [p.rstrip("\\").lower() for p in _csv(env + "_WINDOWS", "C:\\Windows\\Temp,C:\\inetpub\\logs\\LogFiles")]
        if ok and any(norm.lower() == a or norm.lower().startswith(a + "\\") for a in allowed):
            return norm
    raise ValueError(f"path '{path}' is not in the allowed folders for {target}")


# ---------------------------------------------------------------- read-only tools
@tool
def check_memory_usage(target: str, inquiry_user: str = None) -> dict:
    """Get memory usage for a server. target is 'windows' or 'linux'. Returns percent used (based on
    available memory, so file cache does not count as used), swap use, and a next_step to follow."""
    try:
        _check_target(target)
        if target == "linux":
            m = parse_meminfo(infra._linux("grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo",
                                            inquiry_user=inquiry_user))
        else:
            script = ("$os = Get-CimInstance Win32_OperatingSystem; "
                      "$pm = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory; "
                      "[pscustomobject]@{TotalKB=[int64]$os.TotalVisibleMemorySize; "
                      "AvailKB=[int64]($pm.AvailableBytes/1KB)} | ConvertTo-Json -Compress")
            d = json.loads(infra._win(script, inquiry_user=inquiry_user))
            total, avail = d["TotalKB"], d["AvailKB"]
            m = {"total_mb": round(total / 1024), "available_mb": round(avail / 1024),
                 "used_pct": round(100 * (1 - avail / total), 1), "swap_used_pct": None}
        limit = _mem_limit()
        over = m["used_pct"] > limit
        res = {"target": target, "threshold_pct": limit, **m, "over_threshold": over}
        if _post_fix("memory", target):
            before = _BEFORE.get(("memory", target))
            res["before_used_pct"] = before
            if over:
                res["next_step"] = _step(
                    f"Memory is still over the {limit}% limit after the fix ({m['used_pct']}%). Call add_work_note "
                    "with the before and after used_pct, then call escalate_incident.", _L2_DONE)
            else:
                res["next_step"] = _step(
                    "Fix verified: memory is back under the limit. Call add_work_note with the before and after "
                    "used_pct, then call resolve_incident.", _L2_DONE)
        else:
            _BEFORE[("memory", target)] = m["used_pct"]
            if over:
                res["next_step"] = _step(
                    f"Memory is {m['used_pct']}% used (limit {limit}%). Call top_processes with target '{target}' "
                    "and sort_by 'memory' to find what is using it.", _L2_NEXT)
            else:
                res["next_step"] = _step(
                    f"Memory is {m['used_pct']}%, under the {limit}% limit, so the spike has passed and no action "
                    "was needed. Call add_work_note with this, then call resolve_incident.", _L2_NEXT)
        return res
    except Exception as e:
        return {"target": target, "error": str(e)[:300], "next_step": _err_step()}


@tool
def check_cpu_usage(target: str, inquiry_user: str = None) -> dict:
    """Get CPU usage for a server, averaged over a few seconds. target is 'windows' or 'linux'.
    Returns percent busy and a next_step to follow."""
    try:
        _check_target(target)
        if target == "linux":
            c = parse_cpu(infra._linux("head -n1 /proc/stat; sleep 2; head -n1 /proc/stat; nproc; cat /proc/loadavg",
                                        inquiry_user=inquiry_user))
        else:
            script = ("$s = @(1..3 | ForEach-Object { (Get-CimInstance Win32_Processor | "
                      "Measure-Object -Property LoadPercentage -Average).Average; Start-Sleep -Seconds 2 }); "
                      "ConvertTo-Json -InputObject $s -Compress")
            vals = [float(v) for v in json.loads(infra._win(script, inquiry_user=inquiry_user))]
            c = {"cpu_pct": round(sum(vals) / len(vals), 1)}
        limit = _cpu_limit()
        over = c["cpu_pct"] > limit
        res = {"target": target, "threshold_pct": limit, **c, "over_threshold": over}
        if _post_fix("cpu", target):
            res["before_cpu_pct"] = _BEFORE.get(("cpu", target))
            if over:
                res["next_step"] = _step(
                    f"CPU is still over the {limit}% limit after the fix ({c['cpu_pct']}%). Call add_work_note with "
                    "the before and after cpu_pct, then call escalate_incident.", _L2_DONE)
            else:
                res["next_step"] = _step(
                    "Fix verified: CPU is back under the limit. Call add_work_note with the before and after "
                    "cpu_pct, then call resolve_incident.", _L2_DONE)
        else:
            _BEFORE[("cpu", target)] = c["cpu_pct"]
            if over:
                res["next_step"] = _step(
                    f"CPU is {c['cpu_pct']}% busy (limit {limit}%). Call top_processes with target '{target}' "
                    "and sort_by 'cpu' to find what is using it.", _L2_NEXT)
            else:
                res["next_step"] = _step(
                    f"CPU is {c['cpu_pct']}%, under the {limit}% limit, so the spike has passed and no action "
                    "was needed. Call add_work_note with this, then call resolve_incident.", _L2_NEXT)
        return res
    except Exception as e:
        return {"target": target, "error": str(e)[:300], "next_step": _err_step()}


def _win_top(sort_by: str) -> str:
    key = "PercentProcessorTime" if sort_by == "cpu" else "WorkingSetPrivate"
    return (
        "$cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors; "
        "$tot = (Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize * 1KB; "
        "$svc = @{}; Get-CimInstance Win32_Service | Where-Object { $_.ProcessId -gt 0 } | ForEach-Object { "
        "$k = [int]$_.ProcessId; if ($svc.ContainsKey($k)) { $svc[$k] += ',' + $_.Name } else { $svc[$k] = $_.Name } }; "
        "$rows = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | "
        "Where-Object { $_.IdProcess -gt 0 -and $_.Name -ne '_Total' } | "
        f"Sort-Object -Property {key} -Descending | Select-Object -First 5; "
        "$out = @($rows | ForEach-Object { [pscustomobject]@{ pid=[int]$_.IdProcess; name=($_.Name -replace '#\\d+$',''); "
        "cpu_pct=[math]::Round($_.PercentProcessorTime / $cores, 1); mem_mb=[math]::Round($_.WorkingSetPrivate / 1MB, 0); "
        "mem_pct=[math]::Round(100 * $_.WorkingSetPrivate / $tot, 1); service=$svc[[int]$_.IdProcess] } }); "
        "ConvertTo-Json -InputObject $out -Compress"
    )


@tool
def top_processes(target: str, sort_by: str = "memory", inquiry_user: str = None) -> dict:
    """List the 5 processes using the most memory or CPU on a server. target is 'windows' or 'linux'.
    sort_by is 'memory' or 'cpu'. Names the service behind the top process when it is safe to restart,
    and gives a next_step to follow."""
    try:
        _check_target(target)
        if sort_by not in ("memory", "cpu"):
            raise ValueError("sort_by must be 'memory' or 'cpu'")
        if target == "linux":
            key = "%mem" if sort_by == "memory" else "%cpu"
            rows = parse_ps(infra._linux(f"ps -eo pid=,comm=,%mem=,%cpu=,rss= --sort=-{key} | head -n 7",
                                          inquiry_user=inquiry_user))[:5]
            if rows:
                pids = " ".join(str(r["pid"]) for r in rows)        # integers only, so safe in the shell
                cg = parse_cgroups(infra._linux(
                    f"for p in {pids}; do echo \"$p $(grep -o '[^/]*\\.service' /proc/$p/cgroup 2>/dev/null | tail -n 1)\"; done",
                    inquiry_user=inquiry_user))
                for r in rows:
                    r["service"] = cg.get(r["pid"], "")
        else:
            rows = json.loads(infra._win(_win_top(sort_by), inquiry_user=inquiry_user))
            for r in rows:
                r["service"] = r.get("service") or ""
        if not rows:
            raise RuntimeError("the server returned no process list")

        share_key = "mem_pct" if sort_by == "memory" else "cpu_pct"
        for r in rows:
            r["protected"] = _protected(r["name"])
        top = rows[0]
        res = {"target": target, "sort_by": sort_by, "processes": rows, "min_share_pct": _min_share()}
        svc = top["service"]
        deny = {s.lower() for s in _csv("SERVICE_DENYLIST", "")}
        allow = {s.lower() for s in _csv("SERVICE_ALLOWLIST", "")}
        restartable = (svc and "," not in svc and not top["protected"] and svc.lower() not in deny
                       and (not allow or svc.lower() in allow))
        if top[share_key] < _min_share():
            res["next_step"] = _step(
                f"No single process stands out (the top one uses {top[share_key]}%, the bar is {_min_share()}%). "
                "Call add_work_note with the top processes, then call escalate_incident.",
                "Show the engineer the table. No single process stands out, so say that and ask what they want to check next.")
        elif restartable:
            res["culprit_service"] = svc
            res["next_step"] = _step(
                f"The main user is {top['name']} (pid {top['pid']}, {top[share_key]}%), which is service '{svc}'. "
                f"Call add_work_note with the top processes, then call restart_service with target '{target}' and "
                f"service_name '{svc}' (needs engineer approval). Restart no other service.",
                f"Show the engineer the table. The main user is {top['name']} (pid {top['pid']}, {top[share_key]}%), "
                f"service '{svc}'. Suggest restart_service for '{svc}' and wait for the engineer's answer.")
        else:
            res["next_step"] = _step(
                f"The main user is {top['name']} (pid {top['pid']}, {top[share_key]}%), but it is not a service "
                "this playbook may restart. Call add_work_note with the top processes, then call escalate_incident.",
                f"Show the engineer the table. The main user is {top['name']} (pid {top['pid']}, {top[share_key]}%) "
                "and it is not a service that can be restarted by name. Ask whether to stop the process or look further.")
        return res
    except Exception as e:
        return {"target": target, "error": str(e)[:300], "next_step": _err_step()}


@tool
def check_oom_events(target: str) -> dict:
    """Check whether the server killed processes because it ran out of memory (last 24 hours).
    target is 'windows' or 'linux'. Read-only."""
    try:
        _check_target(target)
        if target == "linux":
            out = infra._linux("journalctl -k --since '24 hours ago' --no-pager 2>/dev/null | "
                               "grep -i -E 'out of memory|oom-kill|killed process' | tail -n 8 || true")
            events = [ln[:240] for ln in out.splitlines() if ln.strip()]
        else:
            script = ("$e = @(Get-WinEvent -FilterHashtable @{LogName='System'; Id=2004; "
                      "StartTime=(Get-Date).AddHours(-24)} -MaxEvents 5 -ErrorAction SilentlyContinue | "
                      "ForEach-Object { [pscustomobject]@{ time=\"$($_.TimeCreated)\"; "
                      "message=$_.Message.Substring(0,[Math]::Min(240,$_.Message.Length)) } }); "
                      "ConvertTo-Json -InputObject $e -Compress")
            events = [f"{e['time']}: {e['message']}" for e in json.loads(infra._win(script) or "[]")]
        return {"target": target, "oom_events": events, "count": len(events),
                "note": "" if events else "No out-of-memory events found. If this is Linux, the account may not be able to read the kernel log.",
                "next_step": _step("Call add_work_note with this result.", _L2_NEXT)}
    except Exception as e:
        return {"target": target, "error": str(e)[:300], "next_step": _err_step()}


@tool
def get_service_logs(target: str, service_name: str, lines: int = 30) -> dict:
    """Get the most recent warnings and errors logged by one service. target is 'windows' or 'linux'.
    Read-only. lines is 5 to 60."""
    try:
        _check_target(target)
        svc = infra._safe(service_name, "service_name")
        n = max(5, min(int(lines), 60))
        if target == "linux":
            out = infra._linux(f"journalctl -u {svc} -n {n} --no-pager -p warning 2>/dev/null || true")
            entries = [ln[:240] for ln in out.splitlines() if ln.strip() and not ln.startswith("-- ")]
        else:
            script = ("$e = @(Get-WinEvent -LogName System,Application -MaxEvents 400 -ErrorAction SilentlyContinue | "
                      f"Where-Object {{ $_.Message -like '*{svc}*' -and $_.LevelDisplayName -in 'Error','Warning','Critical' }} | "
                      f"Select-Object -First {n} | ForEach-Object {{ [pscustomobject]@{{ time=\"$($_.TimeCreated)\"; "
                      "level=$_.LevelDisplayName; id=$_.Id; message=$_.Message.Substring(0,[Math]::Min(200,$_.Message.Length)) }} }); "
                      "ConvertTo-Json -InputObject $e -Compress")
            entries = [f"{e['time']} [{e['level']}] {e['id']}: {e['message']}" for e in json.loads(infra._win(script) or "[]")]
        return {"target": target, "service": svc, "entries": entries, "count": len(entries),
                "next_step": _step("Call add_work_note with the key lines.", _L2_NEXT)}
    except Exception as e:
        return {"target": target, "service": service_name, "error": str(e)[:300], "next_step": _err_step()}


@tool
def find_large_files(target: str, path: str) -> dict:
    """List the biggest folders (linux) or files (windows) under an allowed path, and how full its disk is.
    target is 'windows' or 'linux'. Read-only. Only folders in LARGE_FILE_PATHS_LINUX / _WINDOWS are allowed."""
    try:
        _check_target(target)
        p = _allowed_path(target, path, "LARGE_FILE_PATHS", "/var/log,/tmp,/var/tmp,/var/cache")
        if target == "linux":
            out = infra._linux(f"du -xk --max-depth=2 {p} 2>/dev/null | sort -rn | head -n 10 || true")
            items = []
            for ln in out.splitlines():
                kb, _, name = ln.partition("\t")
                if kb.strip().isdigit():
                    items.append({"path": name.strip(), "size_mb": round(int(kb) / 1024, 1)})
        else:
            script = (f"$f = @(Get-ChildItem -Path '{p}' -Recurse -File -Force -ErrorAction SilentlyContinue | "
                      "Sort-Object Length -Descending | Select-Object -First 10 | ForEach-Object { "
                      "[pscustomobject]@{ path=$_.FullName; size_mb=[math]::Round($_.Length/1MB,1) } }); "
                      "ConvertTo-Json -InputObject $f -Compress")
            items = json.loads(infra._win(script) or "[]")
        return {"target": target, "path": p, "largest": items, "filesystem_used_pct": _fs_used_pct(target, p),
                "next_step": _step("Call add_work_note with the largest items.", _L2_NEXT)}
    except Exception as e:
        return {"target": target, "path": path, "error": str(e)[:300], "next_step": _err_step()}


# ---------------------------------------------------------------- tools that change things (approval, see policy.py)
def _recent_restarts(target: str, svc: str) -> int:
    now = time.time()
    times = [t for t in _RESTARTS.get((target, svc), []) if now - t < 3600]
    _RESTARTS[(target, svc)] = times
    return len(times)


@tool
def restart_service(target: str, service_name: str) -> dict:
    """Restart a service (stop, then start). target is 'windows' or 'linux'. Needs engineer approval.
    Returns the new status and a next_step to follow."""
    try:
        _check_target(target)
        svc = infra._safe(service_name, "service_name")
        low, allow = svc.lower(), infra._service_allowlist()
        if low in infra._service_denylist() or (allow and low not in allow):
            raise ValueError(f"Service '{svc}' is blocked by SERVICE_DENYLIST / SERVICE_ALLOWLIST.")
        if _AUDIENCE.get() == "l1" and _recent_restarts(target, svc) >= RESTART_LIMIT_L1:
            return {"target": target, "service": svc, "error": f"{svc} was already restarted in the last hour.",
                    "next_step": "Do not restart it again. Call add_work_note with this, then call escalate_incident."}
        before = infra._service_state(target, svc)
        if target == "windows":
            infra._win(f"Restart-Service -Name '{svc}' -Force")
        else:
            infra._linux(f"sudo -n systemctl restart {svc}")
        _RESTARTS.setdefault((target, svc), []).append(time.time())
        _FIXED[target] = time.time()
        status = infra._service_state(target, svc)
        if status == "active":
            step = _step(f"{svc} was restarted and is {status}. Now run the verification check named in your "
                         "playbook.", f"{svc} restarted and is {status}. Tell the engineer, then run the check "
                         "the playbook names to confirm the fix.")
        else:
            step = _step(f"{svc} did not come back (status: {status}). Call add_work_note with this, then call "
                         "escalate_incident.", f"{svc} did not come back (status: {status}). Tell the engineer, "
                         "show the service logs with get_service_logs, and stop.")
        return {"target": target, "service": svc, "status_before": before, "status": status, "next_step": step}
    except Exception as e:
        return {"target": target, "service": service_name, "error": str(e)[:300], "next_step": _err_step()}


def _proc_name(target: str, pid: int) -> str:
    if target == "linux":
        return infra._linux(f"ps -p {pid} -o comm= || true").strip()
    return infra._win(f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).ProcessName").strip()


@tool
def kill_process(target: str, pid: int, force: bool = False) -> dict:
    """Stop one process by its PID. target is 'windows' or 'linux'. Needs engineer approval.
    force=False asks the process to exit (SIGTERM); force=True ends it at once (SIGKILL / Stop-Process -Force).
    Core system processes are refused. Use top_processes first to get the PID."""
    try:
        _check_target(target)
        pid = int(pid)
        if pid <= 4:
            raise ValueError("pid must be a normal process id")
        name = _proc_name(target, pid)
        if not name:
            raise ValueError(f"no process with pid {pid} on {target}")
        if _protected(name):
            raise ValueError(f"{name} (pid {pid}) is a protected system process and will not be stopped")
        if target == "linux":
            infra._linux(f"sudo -n kill -{9 if force else 15} {pid}")
            time.sleep(3)
        else:
            infra._win(f"Stop-Process -Id {pid} -Force")
            time.sleep(2)
        alive = bool(_proc_name(target, pid))
        _FIXED[target] = time.time()
        if alive:
            step = _step("The process is still running. Call add_work_note with this, then call escalate_incident.",
                         "The process is still running. Tell the engineer, and ask before trying again with force=true.")
        else:
            step = _step("The process has stopped. Run the verification check named in your playbook.",
                         "The process has stopped. Tell the engineer, then run the check the playbook names.")
        return {"target": target, "pid": pid, "process": name, "force": bool(force),
                "still_running": alive, "next_step": step}
    except Exception as e:
        return {"target": target, "pid": pid, "error": str(e)[:300], "next_step": _err_step()}


@tool
def clean_old_logs(target: str, path: str, older_than_days: int = 14) -> dict:
    """Delete rotated or old log files older than N days under an allowed log folder. target is 'windows' or
    'linux'. Needs engineer approval. Live log files are not touched on linux. Only folders in
    LOG_CLEAN_PATHS_LINUX / _WINDOWS are allowed. older_than_days is 3 to 365."""
    try:
        _check_target(target)
        p = _allowed_path(target, path, "LOG_CLEAN_PATHS", "/var/log")
        floor = int(os.getenv("LOG_CLEAN_MIN_DAYS", "3"))
        days = max(floor, min(int(older_than_days), 365))
        before = _fs_used_pct(target, p)
        if target == "linux":
            out = infra._linux(
                f"sudo -n find {p} -xdev -type f -not -path '*/journal/*' "
                "\\( -name '*.gz' -o -name '*.old' -o -name '*.[0-9]' -o -name '*.log.[0-9]*' \\) "
                f"-mtime +{days} -delete -print | wc -l")
            res = {"files_deleted": int(out or 0)}
        else:
            script = (f"$f = @(Get-ChildItem -Path '{p}' -Recurse -File -Force -ErrorAction SilentlyContinue | "
                      f"Where-Object {{ $_.Extension -in '.log','.gz','.zip' -and $_.LastWriteTime -lt (Get-Date).AddDays(-{days}) }}); "
                      "$b = ($f | Measure-Object Length -Sum).Sum; $f | Remove-Item -Force -ErrorAction SilentlyContinue; "
                      "Write-Output ([math]::Round($b/1MB,1))")
            res = {"freed_mb": float(infra._win(script) or 0)}
        after = _fs_used_pct(target, p)
        return {"target": target, "path": p, "older_than_days": days, **res,
                "used_pct_before": before, "used_pct_after": after, "next_step": _step(
                    "Call add_work_note with the before and after used_pct.", _L2_DONE)}
    except Exception as e:
        return {"target": target, "path": path, "error": str(e)[:300], "next_step": _err_step()}


PERF_TOOLS = [check_memory_usage, check_cpu_usage, top_processes, check_oom_events, get_service_logs,
              find_large_files, restart_service, kill_process, clean_old_logs]
HIGH_RISK_PERF_TOOLS = {"kill_process"}
