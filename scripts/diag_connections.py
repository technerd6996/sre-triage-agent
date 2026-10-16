"""Tests every connection the agent uses and shows which one is failing.
Usage:  python scripts/diag_connections.py <windows-server-name> <linux-server-name>
(names as shown in the chat server picker; skip an argument with '-')"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.tools import aws
from app.tools import infra


def step(name, fn):
    try:
        out = fn()
        print(f"[ OK ] {name}: {str(out)[:150]}")
        return True
    except Exception as e:
        print(f"[FAIL] {name}: {str(e)[:300]}")
        return False


def pick(name):
    inst = aws.find_in_text(name)
    if not inst:
        raise RuntimeError(f"No instance named '{name}' found")
    aws.ACTIVE[inst["platform"]] = inst["instance_id"]
    return inst


win_name = sys.argv[1] if len(sys.argv) > 1 else "-"
lin_name = sys.argv[2] if len(sys.argv) > 2 else "-"

print("== AWS ==")
step("list instances (ec2:DescribeInstances)", lambda: [i["name"] for i in aws.list_instances(force=True)])

if win_name != "-":
    print("\n== WINDOWS (WinRM) ==")
    if step("find instance", lambda: pick(win_name)["name"]):
        step("host/IP", lambda: infra._host("windows"))
        step("port 5985 open", lambda: infra.check_reachability.invoke({"target": "windows"}))
        step("WinRM login (whoami)", lambda: infra._win("whoami"))
        step("is admin?", lambda: infra._win(
            "([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
            ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"))
        step("AD module available", lambda: infra._win(
            "Import-Module ActiveDirectory; (Get-Module ActiveDirectory).Version.ToString()"))
        step("AD read (Get-ADUser Administrator)", lambda: infra._win(
            "Import-Module ActiveDirectory; (Get-ADUser -Identity Administrator).SamAccountName"))
        step("temp folder access", lambda: infra._win(
            "(Get-ChildItem 'C:\\Windows\\Temp' -Force -ErrorAction Stop | Measure-Object).Count"))

if lin_name != "-":
    print("\n== LINUX (SSH) ==")
    if step("find instance", lambda: pick(lin_name)["name"]):
        step("host/IP", lambda: infra._host("linux"))
        step("port 22 open", lambda: infra.check_reachability.invoke({"target": "linux"}))
        step("SSH login (whoami)", lambda: infra._linux("whoami"))
        step("passwordless sudo for systemctl", lambda: infra._linux("sudo -n systemctl --version | head -1"))
        step("service status (nginx)", lambda: infra._linux("systemctl is-active nginx || true"))
        step("/tmp readable", lambda: infra._linux("ls /tmp | wc -l"))