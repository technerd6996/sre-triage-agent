"""Phase 0 check. Run from project root:  python scripts/phase0_check.py
Needs in .env: SNOW_INSTANCE, SNOW_USER, SNOW_PASSWORD,
               LINUX_HOST, LINUX_SSH_USER, LINUX_SSH_KEY_PATH (or LINUX_SSH_PASSWORD),
               WINDOWS_HOST, WINDOWS_WINRM_USER, WINDOWS_WINRM_PASSWORD,
               COLAB_API_URL (your existing /generate endpoint)
"""
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root

load_dotenv()
e = os.getenv


def report(name, passed, detail=""):
    print(("PASS  " if passed else "FAIL  ") + name, "-", detail)


# 1. ServiceNow REST
try:
    r = requests.get(
        f"https://{e('SNOW_INSTANCE')}.service-now.com/api/now/table/incident",
        params={"sysparm_limit": 1},
        auth=(e("SNOW_USER"), e("SNOW_PASSWORD")),
        headers={"Accept": "application/json"},
        timeout=20,
    )
    report("SNOW REST", r.status_code == 200, f"HTTP {r.status_code}")
except Exception as ex:
    report("SNOW REST", False, str(ex)[:150])

# 2. Linux over SSH
try:
    import paramiko
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        e("LINUX_HOST"), username=e("LINUX_SSH_USER"),
        key_filename=e("LINUX_SSH_KEY_PATH") or None,
        password=e("LINUX_SSH_PASSWORD") or None, timeout=15,
    )
    _, out, _ = c.exec_command("df -h / | tail -1")
    report("Linux SSH + df", True, out.read().decode().strip())
    c.close()
except Exception as ex:
    report("Linux SSH", False, str(ex)[:150])

# 3. Windows over WinRM, then Active Directory
try:
    import winrm
    s = winrm.Session(
        f"http://{e('WINDOWS_HOST')}:5985/wsman",
        auth=(e("WINDOWS_WINRM_USER"), e("WINDOWS_WINRM_PASSWORD")),
        transport="ntlm",
    )
    r = s.run_ps("(Get-PSDrive C).Free")
    report("Windows WinRM", r.status_code == 0, r.std_out.decode().strip()[:60] or r.std_err.decode()[:100])
    r = s.run_ps("(Get-ADDomain).DNSRoot")
    report("Active Directory", r.status_code == 0 and bool(r.std_out.strip()),
           r.std_out.decode().strip() or "not a domain controller / AD module missing")
except Exception as ex:
    report("Windows WinRM", False, str(ex)[:150])

# 4. LLM tool-calling (the big risk with a 7B model)
try:
    from app.llm.colab_chat import ColabChat
    from langchain.tools import tool

    @tool
    def get_disk_usage(host: str) -> str:
        """Return disk usage for a server."""
        return "97%"

    llm = ColabChat(url=e("COLAB_API_URL"))
    msg = llm.bind_tools([get_disk_usage]).invoke("Check disk usage on linux-01")
    report("LLM tool-calling", bool(msg.tool_calls), str(msg.tool_calls)[:120] or "no tool call returned")
except Exception as ex:
    report("LLM tool-calling", False, str(ex)[:150])
