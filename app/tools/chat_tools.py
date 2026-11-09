"""Tools that only the approver chat can use (Windows over WinRM, Linux over SSH, through the helpers in infra.py).

These are deliberately NOT in INFRA_TOOLS or PERF_TOOLS, so the automatic agent can never call them (the agent builds its
tool list from those two lists). The chat reaches them through app/chat_actions.py (changes) and app/api/approver_chat.py
(read-only checks).

Rules, same as infra.py: target is "windows" or "linux"; a tool returns {"error": ...} instead of raising; every value that
goes into a command is validated first; output is capped. Linux commands are fixed templates, with `sudo -n` only where a
command truly needs it, and nothing here can raise privileges (no su, no sudo -i, no sudoers or group changes).

Read-only (risk 0): check_uptime, check_pending_reboot, check_network, get_event_errors, list_sessions, list_failed_services,
list_scheduled_jobs, check_windows_updates, list_packages, list_upgradable, list_security_updates."""
import json
import re

from langchain_core.tools import tool

from app.tools import infra
from app.tools.infra import TARGETS

MAX_LINES = 40            # most lines any tool returns
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_PKG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]{0,63}$")


def _target(target: str) -> str:
    if target not in TARGETS:
        raise ValueError(f"target must be one of {TARGETS}")
    return target


def _only(target: str, os_name: str, what: str) -> None:
    if _target(target) != os_name:
        raise ValueError(f"{what} is a {os_name.capitalize()} check; this server is {target.capitalize()}")


def _cap(lines: list, n: int = MAX_LINES) -> tuple:
    lines = [ln for ln in lines if ln]
    return lines[:n], max(0, len(lines) - n)


def _ok(target: str, summary: str, lines: list = None, **extra) -> dict:
    shown, more = _cap(lines or [])
    out = {"target": target, "summary": summary, "lines": shown, **extra}
    if more:
        out["more"] = more
    return out


def _fail(target: str, e: Exception) -> dict:
    return {"target": target, "error": str(e)[:300]}


def _win_json(script: str):
    raw = infra._win(script)
    return json.loads(raw) if raw else None


def human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return " ".join(p for p in (f"{d} d" if d else "", f"{h} h" if h or d else "", f"{m} min") if p)


# ---------------------------------------------------------------- uptime and reboot state
@tool
def check_uptime(target: str) -> dict:
    """When the server last started and how long it has been up. target is 'windows' or 'linux'."""
    try:
        _target(target)
        if target == "windows":
            d = _win_json("$o = Get-CimInstance Win32_OperatingSystem; [pscustomobject]@{ boot = "
                          "$o.LastBootUpTime.ToString('yyyy-MM-dd HH:mm:ss'); seconds = "
                          "[int]((Get-Date) - $o.LastBootUpTime).TotalSeconds } | ConvertTo-Json -Compress")
            boot, secs = d["boot"], int(d["seconds"])
        else:
            out = infra._linux("s=$(cut -d. -f1 /proc/uptime); echo $s; "
                               "date -d \"@$(( $(date +%s) - s ))\" '+%Y-%m-%d %H:%M:%S'").splitlines()
            secs, boot = int(out[0]), out[1].strip()
        return _ok(target, f"up {human_duration(secs)}, last started {boot}", seconds=secs, boot=boot)
    except Exception as e:
        return _fail(target, e)


_WIN_PENDING = r"""
$r = @()
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $r += 'Component Based Servicing' }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $r += 'Windows Update' }
if (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue) { $r += 'Pending file rename operations' }
ConvertTo-Json -InputObject @{ reasons = @($r) } -Compress
"""
_LIN_PENDING = ("if [ -f /var/run/reboot-required ]; then echo REQUIRED; "
                "elif command -v needs-restarting >/dev/null 2>&1; then needs-restarting -r >/dev/null 2>&1 && echo NO || echo REQUIRED; "
                "else echo NO; fi")


@tool
def check_pending_reboot(target: str) -> dict:
    """Whether the server is waiting for a restart (after updates or installs). target is 'windows' or 'linux'."""
    try:
        _target(target)
        if target == "windows":
            reasons = (_win_json(_WIN_PENDING) or {}).get("reasons", [])
        else:
            reasons = ["the system reports a reboot is required"] if infra._linux(_LIN_PENDING).strip() == "REQUIRED" else []
        pending = bool(reasons)
        return _ok(target, "a restart is pending" if pending else "no restart is pending", [f"reason: {r}" for r in reasons],
                   pending=pending)
    except Exception as e:
        return _fail(target, e)


# ---------------------------------------------------------------- network from the server
def valid_host(value: str) -> str:
    if not _HOST.match(value or ""):
        raise ValueError("that is not a valid host name or address (letters, digits, dots and dashes only)")
    return value


@tool
def check_network(target: str, host: str, port: int = 0) -> dict:
    """From the server: resolve a name, ping it and (when a port is given) test a TCP connection. target is 'windows'
    or 'linux'; host is a name or IP address; port 0 skips the TCP test."""
    try:
        _target(target)
        h = valid_host(host)
        port = int(port or 0)
        if port and not 1 <= port <= 65535:
            raise ValueError("port must be 1 to 65535")
        if target == "windows":
            tcp = ("$c = New-Object System.Net.Sockets.TcpClient; try { $a = $c.BeginConnect($h, %d, $null, $null); "
                   "$res.tcp = ($a.AsyncWaitHandle.WaitOne(3000) -and $c.Connected) } catch { $res.tcp = $false } "
                   "finally { $c.Close() }" % port) if port else ""
            d = _win_json("$h = '%s'; $res = [ordered]@{}; "
                          "try { $res.dns = @(Resolve-DnsName -Name $h -ErrorAction Stop | Where-Object { $_.IPAddress } | "
                          "ForEach-Object { $_.IPAddress }) } catch { $res.dns = @() }; "
                          "try { $res.ping = [bool](Test-Connection -ComputerName $h -Count 2 -Quiet -ErrorAction Stop) } "
                          "catch { $res.ping = $false }; %s ConvertTo-Json -InputObject $res -Compress" % (h, tcp))
            dns, ping, conn = list(d.get("dns") or []), bool(d.get("ping")), d.get("tcp")
        else:
            tcp = ("; if timeout 3 bash -c 'echo > /dev/tcp/%s/%d' 2>/dev/null; then echo TCP:yes; else echo TCP:no; fi"
                   % (h, port)) if port else ""
            out = infra._linux("echo \"DNS:$(getent hosts '%s' | awk '{print $1}' | head -n 3 | tr '\\n' ' ')\"; "
                               "if ping -c 2 -W 2 '%s' >/dev/null 2>&1; then echo PING:yes; else echo PING:no; fi%s" % (h, h, tcp))
            kv = dict(ln.split(":", 1) for ln in out.splitlines() if ":" in ln)
            dns, ping = kv.get("DNS", "").split(), kv.get("PING", "").strip() == "yes"
            conn = (kv.get("TCP", "").strip() == "yes") if port else None
        is_ip = bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", h))
        lines = ["an address was given, so no name lookup was needed" if is_ip else
                 f"name resolves to: {', '.join(dns) if dns else 'nothing (DNS lookup failed)'}",
                 f"ping: {'replies' if ping else 'no reply (it may block ping)'}"]
        if conn is not None:
            lines.append(f"TCP port {port}: {'open' if conn else 'closed or filtered'}")
        resolves = True if is_ip else bool(dns)
        # a port test is the real answer when one was asked for; otherwise ping (an IP) or the lookup (a name) decides
        ok = conn if conn is not None else (ping if is_ip else resolves)
        dns = [h] if is_ip else dns
        return _ok(target, f"from this server, {h}" + (f":{port}" if port else "") + (" is reachable" if ok else " has a problem"),
                   lines, host=h, port=port, resolves=resolves, ping=ping, tcp=conn)
    except Exception as e:
        return _fail(target, e)


# ---------------------------------------------------------------- recent errors
@tool
def get_event_errors(target: str, minutes: int = 60, limit: int = 10) -> dict:
    """Recent error and critical entries from the system logs (Windows event log, Linux journal). target is 'windows'
    or 'linux'; minutes 5 to 1440; limit 1 to 25."""
    try:
        _target(target)
        minutes, limit = max(5, min(int(minutes), 1440)), max(1, min(int(limit), 25))
        if target == "windows":
            events = _win_json(
                "$s = (Get-Date).AddMinutes(-%d); $e = @(Get-WinEvent -FilterHashtable @{LogName='System','Application'; "
                "Level=1,2; StartTime=$s} -MaxEvents %d -ErrorAction SilentlyContinue | ForEach-Object { "
                "$m = ([string]$_.Message -replace '\\s+',' '); if ($m.Length -gt 160) { $m = $m.Substring(0,160) }; "
                "[pscustomobject]@{ time = $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'); log = $_.LogName; "
                "source = $_.ProviderName; id = $_.Id; message = $m } }); ConvertTo-Json -InputObject $e -Compress"
                % (minutes, limit)) or []
            if isinstance(events, dict):
                events = [events]
            lines = [f"{e['time']}  {e['log']}  {e['source']} ({e['id']}): {e['message']}" for e in events]
        else:
            cmd = f"journalctl -p err --since '{minutes} minutes ago' -n {limit} --no-pager -o short-iso 2>&1"
            out = infra._linux(cmd)
            if "insufficient permissions" in out.lower():
                out = infra._linux("sudo -n " + cmd)
            lines = [ln[:220] for ln in out.splitlines() if ln and not ln.startswith("-- ")]
        n = len(lines)
        return _ok(target, f"{n} error entr{'y' if n == 1 else 'ies'} in the last {minutes} minutes" if n
                   else f"no error entries in the last {minutes} minutes", lines, count=n)
    except Exception as e:
        return _fail(target, e)


# ---------------------------------------------------------------- sessions, failed services, scheduled jobs
@tool
def list_sessions(target: str) -> dict:
    """Who is logged on (Windows quser, Linux who and loginctl). target is 'windows' or 'linux'."""
    try:
        _target(target)
        if target == "windows":
            out = infra._win("(quser 2>&1 | Out-String); exit 0")
            lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
            none = not lines or "no user exists" in out.lower()
            lines = [] if none else lines
        else:
            out = infra._linux("who; echo ---; loginctl list-sessions --no-legend 2>/dev/null || true")
            lines = [ln.rstrip() for ln in out.splitlines() if ln.strip() and ln.strip() != "---"]
        return _ok(target, f"{max(0, len(lines) - (1 if target == 'windows' and lines else 0))} session(s)" if lines
                   else "nobody is logged on", lines)
    except Exception as e:
        return _fail(target, e)


@tool
def list_failed_services(target: str) -> dict:
    """Services that should be running but are not (Windows: automatic start and stopped; Linux: failed units).
    target is 'windows' or 'linux'."""
    try:
        _target(target)
        if target == "windows":
            rows = _win_json("$s = @(Get-CimInstance Win32_Service | Where-Object { $_.StartMode -eq 'Auto' -and "
                             "$_.State -ne 'Running' } | Select-Object Name, State, ExitCode); "
                             "ConvertTo-Json -InputObject $s -Compress") or []
            if isinstance(rows, dict):
                rows = [rows]
            lines = [f"{r['Name']}: {r['State']} (exit code {r['ExitCode']})" for r in rows]
            note = "Some automatic services (trigger start) stop by design; check the name before acting."
        else:
            out = infra._linux("systemctl --failed --no-legend --plain 2>/dev/null || true")
            lines = [" ".join(ln.split()[:4]) for ln in out.splitlines() if ln.strip()]
            note = ""
        return _ok(target, f"{len(lines)} service(s) not running" if lines else "no service is failed or stopped", lines,
                   note=note, count=len(lines))
    except Exception as e:
        return _fail(target, e)


@tool
def list_scheduled_jobs(target: str) -> dict:
    """Scheduled jobs that are not part of the operating system (Windows non-Microsoft tasks, Linux systemd timers and
    cron files). target is 'windows' or 'linux'."""
    try:
        _target(target)
        if target == "windows":
            rows = _win_json("$t = @(Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' } | "
                             "Select-Object -First 50 | ForEach-Object { [pscustomobject]@{ name = $_.TaskName; "
                             "path = $_.TaskPath; state = [string]$_.State } }); ConvertTo-Json -InputObject $t -Compress") or []
            if isinstance(rows, dict):
                rows = [rows]
            lines = [f"{r['path']}{r['name']}  [{r['state']}]" for r in rows]
        else:
            out = infra._linux("systemctl list-timers --all --no-legend --plain 2>/dev/null | head -n 30; echo ---; "
                               "ls -1 /etc/cron.d 2>/dev/null | head -n 20")
            lines = []
            section = "timer"
            for ln in out.splitlines():
                if ln.strip() == "---":
                    section = "cron file in /etc/cron.d"
                elif ln.strip():
                    parts = ln.split()
                    if section == "timer" and len(parts) >= 6:
                        # columns: NEXT (4 words: day date time zone) LEFT... LAST... PASSED UNIT ACTIVATES
                        lines.append(f"timer: {parts[-2]} runs {parts[-1]}, next {' '.join(parts[:4])}")
                    else:
                        lines.append(f"{section}: {ln.strip()}")
        return _ok(target, f"{len(lines)} scheduled job(s)" if lines else "no scheduled jobs found", lines, count=len(lines))
    except Exception as e:
        return _fail(target, e)


# ---------------------------------------------------------------- Windows updates (read only)
_WIN_UPDATES = r"""
$out = [ordered]@{ pending = @(); installed = @(); wu_service = '' ; search_error = '' }
try {
  $sess = New-Object -ComObject Microsoft.Update.Session
  $res = $sess.CreateUpdateSearcher().Search("IsInstalled=0 and Type='Software'")
  $out.pending = @($res.Updates | ForEach-Object { [pscustomobject]@{ title = $_.Title; kb = (($_.KBArticleIDs | ForEach-Object { 'KB' + $_ }) -join ','); severity = [string]$_.MsrcSeverity } })
} catch { $out.search_error = $_.Exception.Message }
$out.installed = @(Get-HotFix -ErrorAction SilentlyContinue | Sort-Object InstalledOn -Descending | Select-Object -First 8 | ForEach-Object { [pscustomobject]@{ kb = $_.HotFixID; on = if ($_.InstalledOn) { $_.InstalledOn.ToString('yyyy-MM-dd') } else { '' } } })
$out.wu_service = [string](Get-Service wuauserv -ErrorAction SilentlyContinue).Status
ConvertTo-Json -InputObject $out -Depth 4 -Compress
"""


@tool
def check_windows_updates(target: str) -> dict:
    """Windows only: updates waiting to be installed, the most recently installed updates, and the Windows Update service
    state. Read only. target must be 'windows'."""
    try:
        _only(target, "windows", "Windows Update")
        d = _win_json(_WIN_UPDATES) or {}
        pending, installed = d.get("pending") or [], d.get("installed") or []
        if isinstance(pending, dict):
            pending = [pending]
        if isinstance(installed, dict):
            installed = [installed]
        lines = [f"pending: {p['kb'] or '(no KB)'} {p['title']}" + (f" [{p['severity']}]" if p.get("severity") else "") for p in pending]
        lines += [f"installed: {i['kb']} on {i['on'] or 'unknown date'}" for i in installed]
        err = d.get("search_error", "")
        summary = (f"{len(pending)} update(s) waiting" if not err else "could not search for waiting updates") + \
                  f"; Windows Update service is {d.get('wu_service') or 'unknown'}"
        out = _ok(target, summary, lines, pending=len(pending), search_error=err)
        if err:
            out["note"] = ("Windows blocks the update search in some remote sessions; the installed list above is still correct. "
                           f"Detail: {err[:160]}")
        return out
    except Exception as e:
        return _fail(target, e)


# ---------------------------------------------------------------- Linux packages (read only)
_PM = ("if command -v dpkg-query >/dev/null 2>&1; then echo apt; elif command -v rpm >/dev/null 2>&1 && "
       "command -v dnf >/dev/null 2>&1; then echo dnf; elif command -v rpm >/dev/null 2>&1; then echo yum; else echo none; fi")


def package_manager() -> str:
    pm = infra._linux(_PM).strip()
    if pm not in ("apt", "dnf", "yum"):
        raise RuntimeError("no supported package manager (apt, dnf or yum) was found on this server")
    return pm


def valid_package(name: str) -> str:
    if not _PKG.match(name or ""):
        raise ValueError(f"'{name}' is not a valid package name")
    return name


@tool
def list_packages(target: str, name: str = "") -> dict:
    """Linux only: installed packages, optionally only those whose name contains `name`. target must be 'linux'."""
    try:
        _only(target, "linux", "Package listing")
        pm = package_manager()
        flt = valid_package(name) if name else ""
        grep = f" | grep -i -F -- '{flt}'" if flt else ""
        cmd = ("dpkg-query -W -f='${Package} ${Version}\\n'" if pm == "apt" else "rpm -qa --qf '%{NAME} %{VERSION}-%{RELEASE}\\n'")
        out = infra._linux(f"{cmd} 2>/dev/null | sort{grep} | head -n 300 || true")
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return _ok(target, (f"{len(lines)} package(s) match '{flt}'" if flt else f"{len(lines)}+ installed packages (first 300)")
                   + f" ({pm})", lines, manager=pm, count=len(lines))
    except Exception as e:
        return _fail(target, e)


@tool
def list_upgradable(target: str) -> dict:
    """Linux only: packages that have a newer version available (from the package lists already on the server).
    target must be 'linux'."""
    try:
        _only(target, "linux", "Upgradable packages")
        pm = package_manager()
        if pm == "apt":
            out = infra._linux("apt list --upgradable 2>/dev/null | tail -n +2 | head -n 200 || true")
        else:
            out = infra._linux(f"{pm} -q check-update 2>/dev/null | grep -E '^[A-Za-z0-9]' | head -n 200 || true")
        lines = [" ".join(ln.split()[:3]) for ln in out.splitlines() if ln.strip()]
        stale = " (apt does not refresh its package lists in this check)" if pm == "apt" else ""
        return _ok(target, f"{len(lines)} package(s) can be upgraded ({pm}){stale}"
                   if lines else f"nothing to upgrade according to the package lists on the server ({pm}){stale}",
                   lines, manager=pm, count=len(lines))
    except Exception as e:
        return _fail(target, e)


@tool
def list_security_updates(target: str) -> dict:
    """Linux only: pending updates that the distribution marks as security fixes. target must be 'linux'."""
    try:
        _only(target, "linux", "Security updates")
        pm = package_manager()
        if pm == "apt":
            out = infra._linux("apt list --upgradable 2>/dev/null | grep -i security | head -n 200 || true")
        else:
            out = infra._linux(f"{pm} -q updateinfo list security 2>/dev/null | grep -E '^[A-Za-z0-9]' | head -n 200 || true")
        lines = [" ".join(ln.split()[:3]) for ln in out.splitlines() if ln.strip()]
        return _ok(target, f"{len(lines)} security update(s) pending ({pm})" if lines else f"no pending security updates found ({pm})",
                   lines, manager=pm, count=len(lines))
    except Exception as e:
        return _fail(target, e)


@tool
def check_patch_job(target: str, job: str = "") -> dict:
    """Windows only: progress or result of a Windows update install started from the chat (newest job when no id is given).
    target must be 'windows'."""
    try:
        _only(target, "windows", "Patch job status")
        j = (job or "").strip().lower()
        if j and not re.fullmatch(r"[0-9a-f]{8}", j):
            raise ValueError("a patch job id is 8 letters or digits, as shown when the install started")
        pick = (f"Get-Item 'C:\\ProgramData\\triage\\wu_{j}.json' -ErrorAction SilentlyContinue" if j else
                "Get-ChildItem 'C:\\ProgramData\\triage\\wu_*.json' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1")
        raw = infra._win(f"$f = {pick}; if ($f) {{ Get-Content -Raw -Path $f.FullName }} else {{ '' }}")
        if not raw.strip():
            return _ok(target, "no patch job was found on this server", [])
        d = json.loads(raw)
        lines = [f"{u.get('title', '')}: " + {2: "installed", 3: "installed with errors", 4: "failed", 5: "cancelled"}.get(u.get("code"), f"code {u.get('code')}")
                 for u in (d.get("updates") or [])]
        if d.get("error"):
            lines.append(f"error: {d['error']}")
        if d.get("reboot_required"):
            lines.append("a restart is required to finish (say: reboot <server>)")
        return _ok(target, f"patch job {d.get('id')} is {d.get('status')} (started {d.get('started')})", lines,
                   job=d.get("id"), status=d.get("status"), reboot_required=bool(d.get("reboot_required")))
    except Exception as e:
        return _fail(target, e)


READ_ONLY_TOOLS = [check_patch_job, check_uptime, check_pending_reboot, check_network, get_event_errors, list_sessions, list_failed_services,
                   list_scheduled_jobs, check_windows_updates, list_packages, list_upgradable, list_security_updates]
