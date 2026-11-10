"""Read-only chat tools: what they run, what they return, and that nothing unsafe reaches a command line."""
import json

import pytest

from app import chat_reads
from app.tools import chat_tools as T
from app.tools import infra


class Fake:
    """Records every command and answers from a script (list of replies, or a callable)."""

    def __init__(self, win=None, linux=None):
        self.win_cmds, self.lin_cmds, self.win, self.lin = [], [], win, linux

    def _w(self, script, **k):
        self.win_cmds.append(script)
        return self.win(script) if callable(self.win) else (self.win or "")

    def _l(self, cmd, **k):
        self.lin_cmds.append(cmd)
        return self.lin(cmd) if callable(self.lin) else (self.lin or "")


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(infra, "_win", f._w)
    monkeypatch.setattr(infra, "_linux", f._l)
    return f


# ---------------------------------------------------------------- uptime and reboot state
def test_uptime_windows_and_linux(fake):
    fake.win = lambda s: json.dumps({"boot": "2026-11-01 08:00:00", "seconds": 90061})
    r = T.check_uptime.invoke({"target": "windows"})
    assert r["seconds"] == 90061 and "up 1 d 1 h 1 min" in r["summary"]
    fake.lin = lambda c: "3600\n2026-11-09 01:00:00"
    r = T.check_uptime.invoke({"target": "linux"})
    assert r["seconds"] == 3600 and r["boot"] == "2026-11-09 01:00:00"


def test_pending_reboot_reports_the_reasons(fake):
    fake.win = lambda s: json.dumps({"reasons": ["Windows Update"]})
    r = T.check_pending_reboot.invoke({"target": "windows"})
    assert r["pending"] is True and r["lines"] == ["reason: Windows Update"]
    fake.lin = lambda c: "NO"
    assert T.check_pending_reboot.invoke({"target": "linux"})["pending"] is False


def test_a_bad_target_is_an_error_not_an_exception(fake):
    assert "target must be" in T.check_uptime.invoke({"target": "solaris"})["error"]


# ---------------------------------------------------------------- network
@pytest.mark.parametrize("host", ["db01;rm -rf /", "a b", "$(whoami)", "x`id`", "-oProxyCommand=x", ""])
def test_network_refuses_hosts_that_could_carry_a_command(fake, host):
    r = T.check_network.invoke({"target": "linux", "host": host, "port": 22})
    assert "error" in r and fake.lin_cmds == [] and fake.win_cmds == []


def test_network_linux_builds_a_fixed_command_and_reads_the_answer(fake):
    fake.lin = lambda c: "DNS:10.0.0.5 \nPING:yes\nTCP:no"
    r = T.check_network.invoke({"target": "linux", "host": "db01.lab.local", "port": 1433})
    assert "'db01.lab.local'" in fake.lin_cmds[0] and "/dev/tcp/db01.lab.local/1433" in fake.lin_cmds[0]
    assert r["resolves"] and r["ping"] and r["tcp"] is False and "closed or filtered" in r["lines"][2]
    assert "has a problem" in r["summary"]


def test_network_windows_reads_json(fake):
    fake.win = lambda s: json.dumps({"dns": ["10.0.0.5"], "ping": True, "tcp": True})
    r = T.check_network.invoke({"target": "windows", "host": "10.0.0.5", "port": 443})
    assert "reachable" in r["summary"] and "TcpClient" in fake.win_cmds[0]


def test_pinging_an_ip_address_does_not_report_a_dns_problem(fake):
    fake.win = lambda s: json.dumps({"dns": [], "ping": True})
    r = T.check_network.invoke({"target": "windows", "host": "8.8.8.8", "port": 0})
    assert "is reachable" in r["summary"] and "no name lookup was needed" in r["lines"][0] and r["resolves"] is True


def test_network_port_is_a_number_in_range(fake):
    assert "port must be" in T.check_network.invoke({"target": "linux", "host": "db01", "port": 70000})["error"]


# ---------------------------------------------------------------- events, sessions, services, jobs
def test_event_errors_windows_and_linux_with_a_permission_retry(fake):
    fake.win = lambda s: json.dumps([{"time": "2026-11-09 01:00:00", "log": "System", "source": "Disk", "id": 7, "message": "bad block"}])
    r = T.get_event_errors.invoke({"target": "windows", "minutes": 30})
    assert r["count"] == 1 and "Disk (7): bad block" in r["lines"][0]
    calls = []

    def lin(c):
        calls.append(c)
        return "No journal files were opened due to insufficient permissions." if not c.startswith("sudo") else "2026-11-09T01:00:00+0000 srv sshd[1]: error x"
    fake.lin = lin
    r = T.get_event_errors.invoke({"target": "linux", "minutes": 10})
    assert calls[1].startswith("sudo -n journalctl") and r["count"] == 1


def test_event_minutes_and_limit_are_clamped(fake):
    fake.lin = lambda c: ""
    T.get_event_errors.invoke({"target": "linux", "minutes": 99999, "limit": 999})
    assert "1440 minutes ago" in fake.lin_cmds[0] and "-n 25" in fake.lin_cmds[0]


def test_sessions_none_and_some(fake):
    fake.win = lambda s: "No User exists for *"
    assert T.list_sessions.invoke({"target": "windows"})["summary"] == "nobody is logged on"
    fake.lin = lambda c: "alice pts/0 2026-11-09 01:00\n---\n 1 1000 alice seat0"
    assert len(T.list_sessions.invoke({"target": "linux"})["lines"]) == 2


def test_failed_services(fake):
    fake.win = lambda s: json.dumps({"Name": "Spooler", "State": "Stopped", "ExitCode": 1})
    r = T.list_failed_services.invoke({"target": "windows"})
    assert r["count"] == 1 and "trigger start" in r["note"]
    fake.lin = lambda c: "nginx.service loaded failed failed A web server"
    assert T.list_failed_services.invoke({"target": "linux"})["lines"] == ["nginx.service loaded failed failed"]


def test_scheduled_jobs_linux_sections(fake):
    fake.lin = lambda c: ("Mon 2026-11-09 17:30:00 UTC 2min 57s left Mon 2026-11-09 17:20:00 UTC 7min ago "
                          "sysstat-collect.timer sysstat-collect.service\n---\nphp")
    lines = T.list_scheduled_jobs.invoke({"target": "linux"})["lines"]
    assert lines[0] == "timer: sysstat-collect.timer runs sysstat-collect.service, next Mon 2026-11-09 17:30:00 UTC"
    assert lines[1] == "cron file in /etc/cron.d: php"


# ---------------------------------------------------------------- Windows updates and Linux packages
def test_windows_updates_lists_pending_and_installed(fake):
    fake.win = lambda s: json.dumps({"pending": [{"title": "Cumulative Update", "kb": "KB5000001", "severity": "Critical"}],
                                     "installed": [{"kb": "KB4999999", "on": "2026-10-01"}], "wu_service": "Running", "search_error": ""})
    r = T.check_windows_updates.invoke({"target": "windows"})
    assert r["pending"] == 1 and "pending: KB5000001 Cumulative Update [Critical]" in r["lines"] and "installed: KB4999999 on 2026-10-01" in r["lines"]


def test_windows_updates_keeps_the_installed_list_when_the_search_is_blocked(fake):
    fake.win = lambda s: json.dumps({"pending": [], "installed": [{"kb": "KB1", "on": "2026-10-01"}], "wu_service": "Running",
                                     "search_error": "Access is denied"})
    r = T.check_windows_updates.invoke({"target": "windows"})
    assert "could not search" in r["summary"] and "Access is denied" in r["note"] and r["lines"]


def test_os_specific_tools_refuse_the_wrong_os(fake):
    assert "Windows check" in T.check_windows_updates.invoke({"target": "linux"})["error"]
    assert "Linux check" in T.list_packages.invoke({"target": "windows"})["error"]


def test_packages_detect_the_manager_and_validate_the_filter(fake):
    fake.lin = lambda c: "apt" if "command -v" in c else "nginx 1.24.0\nnginx-common 1.24.0"
    r = T.list_packages.invoke({"target": "linux", "name": "nginx"})
    assert r["manager"] == "apt" and r["count"] == 2 and "grep -i -F -- 'nginx'" in fake.lin_cmds[1]
    bad = T.list_packages.invoke({"target": "linux", "name": "x'; reboot #"})
    assert "not a valid package name" in bad["error"]


def test_upgradable_and_security_lists(fake):
    fake.lin = lambda c: "dnf" if "command -v" in c else "openssl.x86_64 3.0.8-1 updates"
    assert T.list_upgradable.invoke({"target": "linux"})["count"] == 1
    assert T.list_security_updates.invoke({"target": "linux"})["manager"] == "dnf"


def test_no_package_manager_is_a_clear_error(fake):
    fake.lin = lambda c: "none"
    assert "no supported package manager" in T.list_packages.invoke({"target": "linux"})["error"]


def test_output_is_capped(fake):
    fake.lin = lambda c: "apt" if "command -v" in c else "\n".join(f"pkg{i} 1.0" for i in range(300))
    r = T.list_packages.invoke({"target": "linux"})
    assert len(r["lines"]) == T.MAX_LINES and r["more"] == 260


# ---------------------------------------------------------------- the agent must not have these tools
def test_the_automatic_agent_cannot_call_them():
    from app.agent import agent
    names = {t.name for t in agent.TOOLS}
    assert not (names & {t.name for t in T.READ_ONLY_TOOLS})


def test_policy_knows_them_as_read_only():
    from app.agent import policy
    assert all(policy.risk_level(t.name) == 0 for t in T.READ_ONLY_TOOLS)


# ---------------------------------------------------------------- routing and argument reading
@pytest.mark.parametrize("q,tool", [
    ("What is the uptime of term-win-srv?", T.check_uptime),
    ("is term-win-srv back?", T.check_uptime),
    ("does zabbix-mon need a reboot", T.check_pending_reboot),
    ("ping 10.0.0.5 from term-win-srv", T.check_network),
    ("can term-win-srv reach db01 on port 1433", T.check_network),
    ("recent errors in the event log on term-win-srv", T.get_event_errors),
    ("who is logged in on term-win-srv", T.list_sessions),
    ("which services are stopped on term-win-srv", T.list_failed_services),
    ("list scheduled tasks on term-win-srv", T.list_scheduled_jobs),
    ("pending windows updates on term-win-srv", T.check_windows_updates),
    ("security updates on victim-test", T.list_security_updates),
    ("which packages are upgradable on victim-test", T.list_upgradable),
    ("is nginx installed on victim-test", T.list_packages),
])
def test_questions_route_to_the_right_check(q, tool):
    assert chat_reads.pick(q) is tool


def test_how_to_questions_and_plain_chat_are_not_checks():
    assert chat_reads.pick("how do I check the uptime of a server") is None
    assert chat_reads.pick("What is the CPU utilization?") is None          # handled by the older rules


def test_platform_mapping():
    assert chat_reads.for_platform(T.list_upgradable, "windows") is T.check_windows_updates
    assert chat_reads.for_platform(T.check_windows_updates, "linux") is T.list_upgradable
    assert chat_reads.for_platform(T.check_uptime, "linux") is T.check_uptime


def test_network_target_and_port_are_read_from_the_message():
    assert chat_reads.network_target("ping 10.0.0.5 from term-win-srv", ["term-win-srv"]) == ("10.0.0.5", 0)
    assert chat_reads.network_target("can term-win-srv reach db01.lab.local on port 1433", ["term-win-srv"]) == ("db01.lab.local", 1433)
    assert chat_reads.network_target("telnet db01 port 22", ["srv"]) == ("db01", 22)
    with pytest.raises(chat_reads.NeedsMore):
        chat_reads.network_target("can term-win-srv reach it", ["term-win-srv"])


def test_other_argument_reading():
    assert chat_reads.minutes_in("errors in the last 2 hours") == 120 and chat_reads.minutes_in("last 30 minutes") == 30
    assert chat_reads.minutes_in("recent errors") == 60
    assert chat_reads.package_filter("is nginx installed") == "nginx"
    assert chat_reads.package_filter("which packages are installed") == ""
    assert chat_reads.source_hint("ping db01 from web01") == "web01"
    assert chat_reads.source_hint("can web01 reach db01 on port 1433") == "web01"
    assert chat_reads.source_hint("can it reach db01") == ""


@pytest.mark.parametrize("q", ["is the victim-test rebooted?", "is it up yet?", "has victim-test been restarted", "is victim-test up",
                               "did victim-test come back", "when was victim-test rebooted"])
def test_follow_up_questions_after_a_reboot_route_to_the_uptime_check(q):
    assert chat_reads.pick(q) is T.check_uptime


@pytest.mark.parametrize("q", ["reboot victim-test for patch update", "does victim-test need a reboot"])
def test_asking_for_a_reboot_is_not_an_uptime_question(q):
    assert chat_reads.pick(q) is not T.check_uptime


# ---------------------------------------------------------------- finding services (the SCCM answer that was invented)
def test_find_services_reports_that_nothing_matches_instead_of_guessing(fake):
    fake.win = lambda s: ""                                    # PowerShell prints nothing when no service matches
    r = T.find_services.invoke({"target": "windows", "names": "ccmexec, SMS Agent Host"})
    assert r["count"] == 0 and r["summary"] == "no service matching ccmexec, SMS Agent Host exists on this server" and r["lines"] == []
    assert "'ccmexec','SMS Agent Host'" in fake.win_cmds[0]


def test_find_services_lists_what_exists(fake):
    fake.win = lambda s: json.dumps([{"Name": "Spooler", "DisplayName": "Print Spooler", "State": "Running", "StartMode": "Auto"}])
    r = T.find_services.invoke({"target": "windows", "names": "spooler"})
    assert r["count"] == 1 and r["lines"] == ["Spooler (Print Spooler): Running, startup Auto"]
    fake.lin = lambda c: "nginx.service loaded active running A high performance web server"
    r = T.find_services.invoke({"target": "linux", "names": "nginx"})
    assert r["lines"] == ["nginx.service: active (running)"] and "-e 'nginx'" in fake.lin_cmds[0]


@pytest.mark.parametrize("bad", ["x'; reboot #", "a$(id)", "", ",,", "a,b,c,d,e,f,g,h,i", "*"])
def test_find_services_refuses_names_that_could_carry_a_command(fake, bad):
    assert "error" in T.find_services.invoke({"target": "windows", "names": bad}) and not fake.win_cmds


def test_windows_updates_summary_names_the_last_install_date(fake):
    fake.win = lambda s: json.dumps({"pending": [], "installed": [{"kb": "KB2", "on": "2026-10-23"}, {"kb": "KB1", "on": "2026-09-01"}],
                                     "wu_service": "Running", "search_error": ""})
    assert "the last update was installed on 2026-10-23" in T.check_windows_updates.invoke({"target": "windows"})["summary"]


@pytest.mark.parametrize("q,tool", [
    ("what is the patch status on term-win-srv?", T.check_windows_updates),
    ("When was the last patch installed to that server?", T.check_windows_updates),
    ("how about the sccm services?", T.find_services),
    ("What are those sccm service in it?", T.find_services),
    ("is the spooler service running on term-win-srv", T.find_services),
    ("patch job status on win-test", T.check_patch_job),
    ("which services are stopped on term-win-srv", T.list_failed_services),
])
def test_server_questions_reach_a_real_check_not_the_model(q, tool):
    assert chat_reads.pick(q) is tool


@pytest.mark.parametrize("q", ["what does the print spooler service do", "which services depend on sql", "how do I restart a service"])
def test_general_service_questions_are_left_to_the_model(q):
    assert chat_reads.pick(q) is None


def test_service_names_come_from_the_alias_list_or_the_message():
    assert "ccmexec" in chat_reads.service_names("how about the sccm services?")
    assert chat_reads.service_names("is the spooler service running") == "spooler"
    assert chat_reads.service_names("any services named foo-bar there") == "foo-bar"
    with pytest.raises(chat_reads.NeedsMore):
        chat_reads.service_names("are the services running")


def test_the_chat_model_is_told_not_to_invent_server_facts():
    from app import config
    from app.agent import prompts
    assert "cannot see, query or run anything on any server" in config.get("prompt.grounding")
    assert prompts.GROUNDING_RULE.startswith("GROUNDING RULE")


@pytest.mark.parametrize("q", ["What is the update status on victim-test?", "any updates available on victim-test", "are there updates waiting on victim-test",
                               "when was the last update on victim-test", "what updates are installed on victim-test", "is victim-test up to date"])
def test_update_questions_without_the_word_windows_reach_the_updates_check(q):
    assert chat_reads.pick(q) is T.check_windows_updates                  # "up to date" is not an uptime question


@pytest.mark.parametrize("q", ["update victim-test", "how do I update nginx"])
def test_an_instruction_or_how_to_about_updating_is_not_an_updates_check(q):
    assert chat_reads.pick(q) is None
