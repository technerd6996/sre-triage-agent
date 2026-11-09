"""Tools that CHANGE a server and that only the approver chat can use (never the automatic agent: they are not in
INFRA_TOOLS or PERF_TOOLS). Each runs only after the engineer's Confirm on a card, through app/chat_actions.py, with a
ticket the engineer owns.

Windows over WinRM, Linux over SSH through infra._win / infra._linux. Linux commands are FIXED templates run with
`sudo -n` (passwordless sudo, never a password prompt). Nothing here can raise privileges: there is no su, no sudo -i,
no sudoers or group edit, no setuid change, and packages that grant privileges are on a deny list.
Every value that reaches a command is validated first, and package names go after `--`.

Risk 1 (engineer Confirm): stop_service, set_service_startup, logoff_session, run_scheduled_job, install_packages."""
import json
import re
import secrets

from langchain_core.tools import tool

from app import config
from app.tools import infra
from app.tools.chat_tools import _fail, _ok, _only, _target, _win_json, package_manager, valid_package

config.register("chat.service_protected", "Services the chat will never stop or disable",
                "winrm,sshd,ssh,dbus,systemd-logind,rpcss,eventlog,termservice,lsass,networkmanager,network", "text", "agent",
                help="Comma separated, not case sensitive. Stopping one of these could lock everyone out of the server, "
                     "so the chat refuses (restart is not affected).")
config.register("chat.package_denylist", "Packages the chat will never install, update or remove",
                "sudo,doas,polkit*,pkexec,openssh-server,openssh-clients,systemd,glibc,libc6,kernel*,linux-image*,bash,coreutils,"
                "shadow,shadow-utils,passwd,login,pam,libpam*,dnf,yum,apt,dpkg,rpm", "text", "agent",
                help="Comma separated; a name ending in * matches a prefix. Includes anything that grants or controls privileges.")
config.register("chat.max_packages", "Most packages in one chat request", 10, "int", "agent", lo=1, hi=50)
config.register("chat.install_timeout_seconds", "Longest a package install may run (seconds)", 300, "int", "agent", lo=30, hi=1800)

_JOB_WIN = re.compile(r"^[A-Za-z0-9 ._{}()-]{1,150}$")
_JOB_LIN = re.compile(r"^[A-Za-z0-9@._-]{1,100}$")
_SESSION = re.compile(r"^[A-Za-z0-9]{1,8}$")


def _csv(setting: str) -> list:
    return [x.strip().lower() for x in str(config.get(setting) or "").split(",") if x.strip()]


def service_protected(name: str) -> bool:
    return (name or "").lower() in set(_csv("chat.service_protected"))


def package_blocked(name: str) -> bool:
    n = (name or "").lower()
    return any(n.startswith(p[:-1]) if p.endswith("*") else n == p for p in _csv("chat.package_denylist"))


def _service_ok(svc: str) -> None:
    low, allow = svc.lower(), infra._service_allowlist()
    if low in infra._service_denylist() or (allow and low not in allow):
        raise ValueError(f"Service '{svc}' is blocked by the service allow or deny list.")


# ---------------------------------------------------------------- services
@tool
def stop_service(target: str, service_name: str) -> dict:
    """Stop a service. target is 'windows' or 'linux'. Needs the engineer's Confirm. Protected services are refused."""
    try:
        _target(target)
        svc = infra._safe(service_name, "service_name")
        if service_protected(svc):
            raise ValueError(f"'{svc}' is protected: stopping it could lock everyone out of the server.")
        _service_ok(svc)
        before = infra._service_state(target, svc)
        if target == "windows":
            infra._win(f"Stop-Service -Name '{svc}' -ErrorAction Stop")
        else:
            infra._linux(f"sudo -n systemctl stop {svc}")
        after = infra._service_state(target, svc)
        return {"target": target, "service": svc, "status_before": before, "status": after,
                "summary": f"{svc}: {before} -> {after}"}
    except Exception as e:
        return _fail(target, e)


@tool
def set_service_startup(target: str, service_name: str, mode: str) -> dict:
    """Set whether a service starts at boot. target is 'windows' or 'linux'; mode is 'enabled' or 'disabled'.
    Needs the engineer's Confirm. Protected services are refused."""
    try:
        _target(target)
        svc = infra._safe(service_name, "service_name")
        if mode not in ("enabled", "disabled"):
            raise ValueError("mode must be 'enabled' or 'disabled'")
        if service_protected(svc):
            raise ValueError(f"'{svc}' is protected and its start-up setting is not changed from the chat.")
        _service_ok(svc)
        if target == "windows":
            kind = "Automatic" if mode == "enabled" else "Disabled"
            now = infra._win(f"Set-Service -Name '{svc}' -StartupType {kind} -ErrorAction Stop; "
                             f"[string](Get-Service -Name '{svc}').StartType").strip()
        else:
            infra._linux(f"sudo -n systemctl {'enable' if mode == 'enabled' else 'disable'} {svc}")
            now = infra._linux(f"systemctl is-enabled {svc} || true").strip()
        return {"target": target, "service": svc, "startup": now, "summary": f"{svc} now starts at boot: {now}"}
    except Exception as e:
        return _fail(target, e)


# ---------------------------------------------------------------- sessions
def parse_sessions(target: str, lines: list) -> list:
    """[(id, user)] from the lines list_sessions returned."""
    out = []
    for ln in lines:
        if target == "windows":
            m = re.match(r"^\s*>?(\S+)\s+(?:(\S+)\s+)?(\d+)\s+(Active|Disc|Listen|Conn|Idle|Down)", ln)
            if m:
                out.append((m.group(3), m.group(1)))
        else:
            m = re.match(r"^\s*([0-9a-z]+)\s+\d+\s+(\S+)", ln)      # loginctl list-sessions: SESSION UID USER SEAT
            if m:
                out.append((m.group(1), m.group(2)))
    return out


def find_session(target: str, session_id: str):
    """(id, user) of this session on the server, or None. Read only."""
    from app.tools.chat_tools import list_sessions
    res = list_sessions.invoke({"target": target})
    if res.get("error"):
        raise RuntimeError(res["error"])
    return next((s for s in parse_sessions(target, res.get("lines", [])) if s[0] == str(session_id)), None)


@tool
def logoff_session(target: str, session_id: str) -> dict:
    """Log off one session by its ID (Windows RDP session, Linux login session). target is 'windows' or 'linux'.
    Needs the engineer's Confirm. Unsaved work in that session is lost."""
    try:
        _target(target)
        sid = str(session_id)
        if not _SESSION.match(sid) or (target == "windows" and not sid.isdigit()):
            raise ValueError("a session id is a number, as listed by 'who is logged in on <server>'")
        who = find_session(target, sid)
        if not who:
            raise ValueError(f"there is no session {sid} on this server right now")
        if target == "windows":
            infra._win(f"logoff {int(sid)}")
        else:
            infra._linux(f"sudo -n loginctl terminate-session {sid}")
        gone = find_session(target, sid) is None
        return {"target": target, "session": sid, "user": who[1], "logged_off": gone,
                "summary": f"session {sid} ({who[1]}) " + ("was logged off" if gone else "is still listed")}
    except Exception as e:
        return _fail(target, e)


# ---------------------------------------------------------------- scheduled jobs
def find_job(target: str, name: str) -> str:
    """The exact task or unit this name refers to (read only). Raises ValueError when it is not a listed, non-system job."""
    _target(target)
    n = (name or "").strip()
    if target == "windows":
        if not _JOB_WIN.match(n):
            raise ValueError("that is not a valid task name")
        out = infra._win("$n = '%s'; $t = @(Get-ScheduledTask | Where-Object { $_.TaskName -eq $n -and $_.TaskPath -notlike "
                         "'\\Microsoft\\*' }); ConvertTo-Json -InputObject @{ count = $t.Count; "
                         "path = if ($t.Count -eq 1) { $t[0].TaskPath } else { '' } } -Compress" % n.replace("'", "''"))
        d = json.loads(out)
        if d["count"] == 0:
            raise ValueError(f"no task named '{n}' that is not part of Windows (ask: list scheduled tasks on <server>)")
        if d["count"] > 1:
            raise ValueError(f"more than one task is named '{n}'; ask a person to run it")
        return d["path"] + n
    if not _JOB_LIN.match(n):
        raise ValueError("that is not a valid unit name")
    out = infra._linux("systemctl list-timers --all --no-legend --plain 2>/dev/null || true")
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 2 and n in (parts[-2], parts[-1]):
            return parts[-1]                                  # the service the timer activates
    raise ValueError(f"no systemd timer or service '{n}' is listed (ask: list scheduled jobs on <server>)")


@tool
def run_scheduled_job(target: str, name: str) -> dict:
    """Start one scheduled job now (a Windows task that is not part of Windows, or the service a systemd timer activates).
    target is 'windows' or 'linux'. Needs the engineer's Confirm. It starts the job and returns; it does not wait for it."""
    try:
        resolved = find_job(target, name)
        if target == "windows":
            nm = name.strip().replace("'", "''")
            state = infra._win(f"$t = Get-ScheduledTask | Where-Object {{ $_.TaskName -eq '{nm}' -and $_.TaskPath -notlike "
                               "'\\Microsoft\\*' }; Start-ScheduledTask -InputObject $t -ErrorAction Stop; Start-Sleep -Seconds 2; "
                               "[string](Get-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath).State").strip()
        else:
            infra._linux(f"sudo -n systemctl start --no-block {resolved}")
            state = "start requested"
        return {"target": target, "job": resolved, "state": state, "summary": f"{resolved}: {state}"}
    except Exception as e:
        return _fail(target, e)


# ---------------------------------------------------------------- Linux packages
def validate_packages(packages: list) -> list:
    names = list(dict.fromkeys(p.strip() for p in packages if p and p.strip()))
    if not names:
        raise ValueError("no package names were given")
    if len(names) > int(config.get("chat.max_packages")):
        raise ValueError(f"at most {config.get('chat.max_packages')} packages in one request")
    for n in names:
        valid_package(n)
        if package_blocked(n):
            raise ValueError(f"'{n}' is on the protected package list and is not changed from the chat")
    return names


def _pkg_cmd(pm: str, mode: str, names: list, simulate: bool) -> str:
    quoted = " ".join(f"'{n}'" for n in names)
    if pm == "apt":
        base = "apt-get -s" if simulate else "sudo -n apt-get -y -q -o Dpkg::Options::=--force-confold"
        return f"{base} install {'--only-upgrade ' if mode == 'update' else ''}-- {quoted}" + (" 2>&1 || true" if simulate else "")
    verb = "upgrade" if mode == "update" else "install"
    return (f"sudo -n {pm} {'--assumeno ' if simulate else '-y '}{verb} -- {quoted}" + (" 2>&1 || true" if simulate else ""))


def parse_plan(pm: str, text: str) -> dict:
    """What a simulated install or update would do: {'adds': [...], 'removes': [...], 'error': ''}."""
    adds, removes, error = [], [], ""
    if pm == "apt":
        for ln in text.splitlines():
            if ln.startswith("Inst "):
                adds.append(ln[5:].split("[")[0].split("(")[0].strip())
            elif ln.startswith("Remv "):
                removes.append(ln[5:].split("[")[0].split("(")[0].strip())
            elif ln.startswith("E: "):
                error = ln[3:].strip()
        return {"adds": adds, "removes": removes, "error": error}
    section = ""
    all_lines = text.splitlines()
    for i, ln in enumerate(all_lines):
        s = ln.strip()
        if s == "Error:" or s.startswith("Error: ") and not error:
            # dnf puts the reason on the lines AFTER "Error:" (for example a conflicting installed package)
            more = [x.strip() for x in all_lines[i + 1:i + 4] if x.strip() and not x.strip().startswith("(")]
            error = " ".join([s] + more).strip()
            continue
        if s.endswith(":") and s.rstrip(":") in ("Installing", "Upgrading", "Removing", "Installing dependencies", "Upgrading dependencies",
                                                  "Installing weak dependencies", "Removing dependent packages", "Downgrading", "Reinstalling"):
            section = s.rstrip(":")
            continue
        if not s:
            if section and not ln.startswith(" "):
                section = ""
            continue
        if s.startswith("Transaction Summary") or s.startswith("====") or s.startswith("Package "):
            section = "" if s.startswith("Transaction") else section
            continue
        if s.startswith("No match for argument") or s.startswith("Error:") or "Unable to find a match" in s:
            error = s
        if section in ("Removing", "Removing dependent packages") and not ln.startswith(("Last metadata", "Dependencies")):
            removes.append(" ".join(s.split()[:3]))
        elif section and not ln.startswith(("Last metadata", "Dependencies")) and len(s.split()) >= 3:
            adds.append(" ".join(s.split()[:3]))
    return {"adds": adds, "removes": removes, "error": error}


def simulate_packages(target: str, packages: list, mode: str) -> dict:
    """Read only dry run: {'manager', 'adds', 'removes', 'error'}. Runs `apt-get -s` or `dnf --assumeno`; changes nothing."""
    _only(target, "linux", "Package install")
    names = validate_packages(packages)
    pm = package_manager()
    plan = parse_plan(pm, infra._linux(_pkg_cmd(pm, mode, names, True), timeout=90))
    plan["manager"], plan["packages"] = pm, names
    return plan


@tool
def install_packages(target: str, packages: list, mode: str = "install") -> dict:
    """Linux only: install (mode 'install') or update (mode 'update') the named packages with the server's package manager.
    Needs the engineer's Confirm, and the server owners are mailed afterwards. A package list that would remove other
    packages is refused. target must be 'linux'."""
    try:
        _only(target, "linux", "Package install")
        if mode not in ("install", "update"):
            raise ValueError("mode must be 'install' or 'update'")
        names = validate_packages(list(packages))
        pm = package_manager()
        plan = parse_plan(pm, infra._linux(_pkg_cmd(pm, mode, names, True), timeout=90))
        if plan["error"]:
            raise RuntimeError(plan["error"])
        if plan["removes"]:
            raise RuntimeError("this would also remove: " + ", ".join(plan["removes"][:5]) + ". Not run.")
        out = infra._linux(_pkg_cmd(pm, mode, names, False), timeout=int(config.get("chat.install_timeout_seconds")))
        tail = [ln.strip() for ln in out.splitlines() if ln.strip()][-8:]
        ver_cmd = ("dpkg-query -W -f='${Package} ${Version}\\n'" if pm == "apt" else "rpm -q --qf '%{NAME} %{VERSION}-%{RELEASE}\\n'")
        have = infra._linux(f"{ver_cmd} {' '.join(repr(n) for n in names)} 2>&1 || true").splitlines()
        return _ok(target, f"{mode} finished with {pm}: " + (", ".join(plan["adds"][:6]) or "nothing needed changing"),
                   [f"now: {h.strip()}" for h in have if h.strip()] + [f"log: {t}" for t in tail],
                   manager=pm, packages=names, changed=len(plan["adds"]))
    except Exception as e:
        return _fail(target, e)


# ---------------------------------------------------------------- Linux package removal (owner approval first)
def _remove_cmd(pm: str, names: list, simulate: bool) -> str:
    quoted = " ".join(f"'{n}'" for n in names)
    if pm == "apt":
        return f"apt-get -s remove -- {quoted} 2>&1 || true" if simulate else f"sudo -n apt-get -y -q remove -- {quoted}"
    return (f"sudo -n {pm} --assumeno remove -- {quoted} 2>&1 || true" if simulate else f"sudo -n {pm} -y remove -- {quoted}")


def simulate_removal(target: str, packages: list) -> dict:
    """Read only dry run of a removal: {'manager', 'packages', 'removes', 'extra', 'error'}. `extra` = other packages that would go too."""
    _only(target, "linux", "Package removal")
    names = validate_packages(packages)
    pm = package_manager()
    plan = parse_plan(pm, infra._linux(_remove_cmd(pm, names, True), timeout=90))
    named = {n.lower() for n in names}
    plan["extra"] = [r for r in plan["removes"] if r.split()[0].lower() not in named]
    plan["manager"], plan["packages"] = pm, names
    return plan


@tool
def remove_packages(target: str, packages: list) -> dict:
    """Linux only: remove the named packages (not purge, no autoremove). Refused when the removal would also remove other
    packages. Needs the owners' approval and the engineer's Confirm. target must be 'linux'."""
    try:
        _only(target, "linux", "Package removal")
        plan = simulate_removal(target, list(packages))
        if plan["error"]:
            raise RuntimeError(plan["error"])
        if plan["extra"]:
            raise RuntimeError("this would also remove: " + ", ".join(plan["extra"][:5]) + ". Not run.")
        if not plan["removes"]:
            raise RuntimeError("none of those packages is installed")
        pm, names = plan["manager"], plan["packages"]
        out = infra._linux(_remove_cmd(pm, names, False), timeout=int(config.get("chat.install_timeout_seconds")))
        tail = [ln.strip() for ln in out.splitlines() if ln.strip()][-6:]
        ver = ("dpkg-query -W -f='${Package} ${Version}\\n'" if pm == "apt" else "rpm -q --qf '%{NAME} %{VERSION}\\n'")
        left = [ln.strip() for ln in infra._linux(f"{ver} {' '.join(repr(n) for n in names)} 2>&1 || true").splitlines() if ln.strip()]
        return _ok(target, f"removed with {pm}: " + ", ".join(r.split()[0] for r in plan["removes"][:8]),
                   [f"now: {x}" for x in left] + [f"log: {t}" for t in tail], manager=pm, packages=names, removed=len(plan["removes"]))
    except Exception as e:
        return _fail(target, e)


# ---------------------------------------------------------------- reboot and shutdown (owner approval first)
def _power(target: str, mode: str, delay_seconds: int, reason: str) -> dict:
    _target(target)
    delay = max(10, min(int(delay_seconds), 600))
    why = re.sub(r"[^A-Za-z0-9@._ -]", "", reason or "")[:80].strip() or "requested in the chat"
    word = "restart" if mode == "reboot" else "shutdown"
    if target == "windows":
        infra._win(f'shutdown /{"r" if mode == "reboot" else "s"} /t {delay} /c "TRIAGE-1: {why}"')
        cancel = "shutdown /a"
    else:
        minutes = max(1, -(-delay // 60))
        infra._linux(f'sudo -n shutdown {"-r" if mode == "reboot" else "-h"} +{minutes} "TRIAGE-1: {why}"')
        cancel, delay = "shutdown -c", minutes * 60
    return {"target": target, "mode": mode, "delay_seconds": delay,
            "summary": f"{word} scheduled in {delay} seconds", "lines": [f"to cancel in time, run: {cancel}"]}


@tool
def reboot_server(target: str, delay_seconds: int = 60, reason: str = "") -> dict:
    """Restart the whole server after a warning delay (10 to 600 seconds). target is 'windows' or 'linux'. Needs the owners'
    approval and the engineer's Confirm. Never used on the server TRIAGE-1 runs on."""
    try:
        return _power(target, "reboot", delay_seconds, reason)
    except Exception as e:
        return _fail(target, e)


@tool
def shutdown_server(target: str, delay_seconds: int = 60, reason: str = "") -> dict:
    """Shut the whole server down (power off) after a warning delay (10 to 600 seconds). target is 'windows' or 'linux'.
    Needs the owners' approval and the engineer's Confirm. Someone must power it on again by hand or from the cloud console."""
    try:
        return _power(target, "shutdown", delay_seconds, reason)
    except Exception as e:
        return _fail(target, e)


# ---------------------------------------------------------------- Windows updates (owner approval first)
# Windows refuses to install updates from a remote session, so a ONE-TIME scheduled task runs this script as SYSTEM. It uses the
# built-in Windows Update API, writes its progress to C:\ProgramData\triage\wu_<job>.json, and removes the task and itself at the end.
_KB = re.compile(r"^KB\d{6,8}$")
_JOB = re.compile(r"^[0-9a-f]{8}$")
_WU_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$id = '__ID__'
$want = @(__KBS__)
$out = "C:\ProgramData\triage\wu_$id.json"
$res = [ordered]@{ id = $id; status = 'running'; started = (Get-Date).ToString('s'); updates = @(); reboot_required = $false; error = '' }
function Save { ($res | ConvertTo-Json -Depth 4 -Compress) | Set-Content -Path $out -Encoding UTF8 }
Save
try {
  $session = New-Object -ComObject Microsoft.Update.Session
  $found = $session.CreateUpdateSearcher().Search("IsInstalled=0 and Type='Software'")
  $coll = New-Object -ComObject Microsoft.Update.UpdateColl
  foreach ($u in $found.Updates) {
    $kbs = @($u.KBArticleIDs | ForEach-Object { 'KB' + $_ })
    if (@($kbs | Where-Object { $want -contains $_ }).Count -gt 0) {
      if (-not $u.EulaAccepted) { $u.AcceptEula() }
      [void]$coll.Add($u)
    }
  }
  if ($coll.Count -eq 0) {
    $res.status = 'nothing'; $res.error = 'none of the requested updates is pending any more'
  } else {
    $dl = $session.CreateUpdateDownloader(); $dl.Updates = $coll; [void]$dl.Download()
    $inst = $session.CreateUpdateInstaller(); $inst.Updates = $coll
    $r = $inst.Install()
    $list = @()
    for ($n = 0; $n -lt $coll.Count; $n++) { $list += [pscustomobject]@{ title = $coll.Item($n).Title; code = [int]$r.GetUpdateResult($n).ResultCode } }
    $res.updates = $list
    $res.reboot_required = [bool]$r.RebootRequired
    $res.status = if ($r.ResultCode -eq 2 -or $r.ResultCode -eq 3) { 'done' } else { 'failed' }
  }
} catch { $res.status = 'failed'; $res.error = $_.Exception.Message }
Save
Unregister-ScheduledTask -TaskName "TRIAGE1-WU-$id" -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -Path $PSCommandPath -Force -ErrorAction SilentlyContinue
"""


def validate_kbs(kbs: list) -> list:
    names = list(dict.fromkeys(str(k).strip().upper() for k in kbs if str(k).strip()))
    if not names:
        raise ValueError("no update numbers were given (for example KB5007651)")
    if len(names) > int(config.get("chat.max_packages")):
        raise ValueError(f"at most {config.get('chat.max_packages')} updates in one request")
    for k in names:
        if not _KB.match(k):
            raise ValueError(f"'{k}' is not an update number like KB5007651")
    return names


def wu_script(job: str, kbs: list) -> str:
    """The script the scheduled task runs. Only a validated job id and KB numbers are put into it."""
    if not _JOB.match(job):
        raise ValueError("bad job id")
    return _WU_SCRIPT.replace("__ID__", job).replace("__KBS__", ",".join(f"'{k}'" for k in validate_kbs(kbs)))


def wu_register_command(job: str, script: str) -> str:
    """PowerShell that writes the script, registers the one-time SYSTEM task and starts it. The here-string end marker is on its own line."""
    return "\n".join([
        r"$d = 'C:\ProgramData\triage'",
        "New-Item -ItemType Directory -Force -Path $d | Out-Null",
        f'$p = "$d\\wu_{job}.ps1"',
        "$s = @'",
        script.strip("\n"),
        "'@",
        "Set-Content -Path $p -Value $s -Encoding UTF8",
        '$a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File $p"',
        "$pr = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest",
        f"Register-ScheduledTask -TaskName 'TRIAGE1-WU-{job}' -Action $a -Principal $pr -Force | Out-Null",
        f"Start-ScheduledTask -TaskName 'TRIAGE1-WU-{job}'",
    ])


@tool
def install_windows_updates(target: str, kbs: list) -> dict:
    """Windows only: install the named updates (KB numbers) through the Windows Update API, run by a one-time scheduled task
    as SYSTEM. It starts the job and returns at once; progress is written to a result file that the chat watches. Needs the
    owners' approval and the engineer's Confirm. A restart is NOT done automatically. target must be 'windows'."""
    try:
        _only(target, "windows", "Windows update install")
        names = validate_kbs(list(kbs))
        job = secrets.token_hex(4)
        infra._win(wu_register_command(job, wu_script(job, names)), timeout=60)
        return {"target": target, "job": job, "kbs": names, "summary": f"install of {', '.join(names)} started (job {job})",
                "lines": [f"result file on the server: C:\\ProgramData\\triage\\wu_{job}.json",
                          "the chat checks it every few minutes and writes the result on the ticket"]}
    except Exception as e:
        return _fail(target, e)


CHANGE_TOOLS = [stop_service, set_service_startup, logoff_session, run_scheduled_job, install_packages,
                remove_packages, reboot_server, shutdown_server, install_windows_updates]
