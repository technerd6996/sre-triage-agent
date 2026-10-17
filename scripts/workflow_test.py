"""End-to-end workflow test against the RUNNING server (real ServiceNow, real agent, real host).

Run from the project root, server and Colab model up:
    python -m scripts.workflow_test                       # access + chat switch (safe, fast)
    python -m scripts.workflow_test access chat shadow    # add the approval workflow
    python -m scripts.workflow_test auto                  # autonomous restore
    python -m scripts.workflow_test p1                    # P1 proposal and notice (server needs P1_TIME_BUDGET_MIN=1)

Credentials come from the environment (or .env):
    WF_BASE=http://localhost:8000
    WF_ADMIN=you@example.com      WF_ADMIN_PASS=...
    WF_USER=enduser@test.com      WF_USER_PASS=...     (an end-user account, for the access checks)
Scenarios shadow, auto and p1 create real tickets and need nginx STOPPED on victim-test beforehand.
The script restores chat=on, autonomy=shadow and the previous auto-mode setting when it finishes.
"""
import os
import re
import subprocess
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()
BASE = os.getenv("WF_BASE", "http://localhost:8000").rstrip("/")
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    return bool(cond)


def login(user, pw):
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"username": user, "password": pw}, timeout=15)
    if r.status_code != 200:
        sys.exit(f"Login failed for {user}: HTTP {r.status_code} {r.text[:120]}")
    return s


def api(s, method, path, **kw):
    for attempt in (1, 2, 3):
        try:
            return s.request(method, BASE + path, timeout=30, **kw)
        except requests.exceptions.ConnectionError:
            if attempt == 3:
                sys.exit(f"Lost the server while calling {path}. Check the uvicorn terminal: a reload or crash drops in-memory runs.")
            time.sleep(5)


def new_ticket():
    out = subprocess.run([sys.executable, "-m", "scripts.simulate_zabbix"], capture_output=True, text=True, timeout=120)
    m = re.search(r"Created (INC\d+)", out.stdout)
    if not m:
        sys.exit("Could not create a ticket:\n" + out.stdout + out.stderr)
    return m.group(1)


def start_run(adm, ticket):
    r = api(adm, "POST", "/api/agent/run", json={"ticket": ticket})
    if r.status_code != 200:
        sys.exit(f"Could not start a run for {ticket}: HTTP {r.status_code} {r.text[:150]} (is another run active?)")
    return r.json()["run_id"]


def wait_for(adm, run_id, want, timeout=240):
    """Poll until status is in `want`. Returns (run, statuses_seen)."""
    seen, end = set(), time.time() + timeout
    while time.time() < end:
        d = api(adm, "GET", f"/api/agent/runs/{run_id}").json()
        seen.add(d["status"])
        if d["status"] in want:
            if d["status"] == "error":
                for e in d.get("events", []):
                    if e.get("type") == "error":
                        print("    agent error:", str(e.get("message"))[:300])
            return d, seen
        time.sleep(2)
    return d, seen


def need_nginx_stopped():
    if os.getenv("WF_CONFIRM") != "1":
        ans = input("Is nginx STOPPED on victim-test right now? [y/N] ").strip().lower()
        if ans != "y":
            sys.exit("Stop nginx on victim-test first, then run again.")

def _checker_steps(adm, rid):
    full = api(adm, "GET", f"/api/agent/runs/{rid}?after=0").json()
    return full, [e for e in full["events"] if e.get("type") == "verification"]


def t_checker(adm, usr):
    print("\n== Checker: fix holds (server needs CHECKER_MODE=shadow or on; set WF_CHECKER_MODE to match) ==")
    need_nginx_stopped()
    expect = os.getenv("WF_CHECKER_MODE", "shadow")
    api(adm, "POST", "/api/admin/autonomy", json={"mode": "shadow"})
    tk = new_ticket()
    print(f"  ticket {tk}")
    rid = start_run(adm, tk)
    d, _ = wait_for(adm, rid, {"awaiting_approval", "done", "error"})
    if not check("run pauses for approval", d["status"] == "awaiting_approval", d["status"]):
        return
    api(adm, "POST", f"/api/agent/runs/{rid}/decision", json={"approve": True, "feedback": "checker test"})
    d, _ = wait_for(adm, rid, {"done", "error"}, 420)      # includes the checker soak
    check("run finishes", d["status"] == "done", d["status"])
    full, ver = _checker_steps(adm, rid)
    check("checker step recorded", len(ver) >= 2, f"{len(ver)} verification events")
    verdict = ver[-1].get("verdict") if ver else None
    want = "pass" if expect == "on" else "shadow-pass"
    check(f"verdict is {want}", verdict == want, str(verdict))
    check("outcome stays resolved", d.get("outcome") == "resolved", d.get("outcome"))
    print(f"  Check manually: {tk} is {'Closed' if expect == 'on' else 'still Resolved'}; work note from triage-checker;"
          " `verifications` row exists.")


def t_checker_flap(adm, usr):
    print("\n== Checker: fix does not hold (needs VERIFY_SOAK_SECONDS>=30 and CHECKER_MODE=on) ==")
    need_nginx_stopped()
    api(adm, "POST", "/api/admin/autonomy", json={"mode": "shadow"})
    tk = new_ticket()
    print(f"  ticket {tk}")
    rid = start_run(adm, tk)
    d, _ = wait_for(adm, rid, {"awaiting_approval", "done", "error"})
    if not check("run pauses for approval", d["status"] == "awaiting_approval", d["status"]):
        return
    api(adm, "POST", f"/api/agent/runs/{rid}/decision", json={"approve": True, "feedback": "checker flap test"})
    end = time.time() + 300
    while time.time() < end:                               # wait for the first checker step
        if any("verifying" in str(s.get("text", "")) for s in api(adm, "GET", f"/api/agent/runs/{rid}?after=0").json()["steps"]):
            break
        time.sleep(2)
    else:
        check("checker started", False, "no 'Checker: verifying' step within 5 min")
        return
    print("  >>> The checker is in its soak window. NOW stop nginx on victim-test (sudo systemctl stop nginx).")
    d, _ = wait_for(adm, rid, {"done", "error"}, 300)
    full, ver = _checker_steps(adm, rid)
    check("verdict is fail", bool(ver) and ver[-1].get("verdict") == "fail", str(ver[-1].get("verdict") if ver else None))
    check("outcome reported as escalated", d.get("outcome") == "escalated", d.get("outcome"))
    print(f"  Check manually: {tk} is In Progress, assigned to the L2 group, with a 'Verification FAILED' note;"
          " a P1 proposal appears in pending approvals (nginx is critical).")

# ---------------- scenarios ----------------
def t_access(adm, usr):
    print("\n== Access control ==")
    check("anonymous cannot read approvals", requests.get(f"{BASE}/api/agent/approvals", timeout=10).status_code == 401)
    check("end user cannot read approvals", api(usr, "GET", "/api/agent/approvals").status_code == 403)
    check("end user cannot change autonomy", api(usr, "POST", "/api/admin/autonomy", json={"mode": "on"}).status_code == 403)
    check("end user cannot toggle chat", api(usr, "POST", "/api/admin/chat", json={"enabled": False}).status_code == 403)
    check("admin can read approvals", api(adm, "GET", "/api/agent/approvals").status_code == 200)
    check("admin role reported", api(adm, "GET", "/api/auth/me").json().get("role") == "admin")


def t_chat(adm, usr):
    print("\n== Chat switch ==")
    try:
        check("switch chat off", api(adm, "POST", "/api/admin/chat", json={"enabled": False}).status_code == 200)
        time.sleep(6)                                   # settings cache is 5 s
        check("chat page replaced by 'disabled' page", "Chat is currently disabled" in api(usr, "GET", "/").text)
        check("chat API refuses with 403", api(usr, "POST", "/api/chat", json={"query": "hello"}).status_code == 403)
    finally:
        api(adm, "POST", "/api/admin/chat", json={"enabled": True})
    time.sleep(6)
    check("chat page is back after switching on", "Chat is currently disabled" not in api(usr, "GET", "/").text)


def t_shadow(adm, usr):
    print("\n== Approval workflow (shadow mode) ==")
    need_nginx_stopped()
    api(adm, "POST", "/api/admin/autonomy", json={"mode": "shadow"})
    tk = new_ticket()
    print(f"  ticket {tk}")
    rid = start_run(adm, tk)
    d, _ = wait_for(adm, rid, {"awaiting_approval", "done", "error"})
    if not check("run pauses for approval", d["status"] == "awaiting_approval", d["status"]):
        return
    check("pending action is start_service", any(a["name"] == "start_service" for a in d["pending"] or []))
    pend = api(adm, "GET", "/api/agent/approvals").json()
    check("run is listed in pending approvals", any(p["run_id"] == rid for p in pend))
    check("end user cannot decide", api(usr, "POST", f"/api/agent/runs/{rid}/decision", json={"approve": True}).status_code == 403)
    check("reject without comment is refused", api(adm, "POST", f"/api/agent/runs/{rid}/decision", json={"approve": False, "feedback": ""}).status_code == 422)
    check("approve with comment accepted", api(adm, "POST", f"/api/agent/runs/{rid}/decision", json={"approve": True, "feedback": "workflow test"}).status_code == 200)
    check("second decision is refused", api(adm, "POST", f"/api/agent/runs/{rid}/decision", json={"approve": True, "feedback": "again"}).status_code == 409)
    d, _ = wait_for(adm, rid, {"done", "error"}, 300)
    check("run finishes", d["status"] == "done", d["status"])
    check("outcome is resolved", d.get("outcome") == "resolved", d.get("outcome"))
    check("pending list is empty again", not any(p["run_id"] == rid for p in api(adm, "GET", "/api/agent/approvals").json()))


def t_auto(adm, usr):
    print("\n== Autonomous restore (mode on) ==")
    need_nginx_stopped()
    try:
        api(adm, "POST", "/api/admin/autonomy", json={"mode": "on"})
        tk = new_ticket()
        print(f"  ticket {tk}")
        rid = start_run(adm, tk)
        d, seen = wait_for(adm, rid, {"done", "error"}, 300)
        check("run finishes", d["status"] == "done", d["status"])
        check("never waited for a human", "awaiting_approval" not in seen, ",".join(sorted(seen)))
        full = api(adm, "GET", f"/api/agent/runs/{rid}?after=0").json()
        check("decision recorded as policy", any("policy" in str(e.get("by", "")) for e in full["events"] if e["type"] == "decision"))
        check("outcome is resolved", d.get("outcome") == "resolved", d.get("outcome"))
    finally:
        api(adm, "POST", "/api/admin/autonomy", json={"mode": "shadow"})


def t_p1(adm, usr):
    print("\n== P1 proposal and notice (server needs P1_TIME_BUDGET_MIN=1) ==")
    need_nginx_stopped()
    api(adm, "POST", "/api/admin/autonomy", json={"mode": "shadow"})
    tk = new_ticket()
    print(f"  ticket {tk}")
    rid = start_run(adm, tk)
    d, _ = wait_for(adm, rid, {"awaiting_approval", "done", "error"})
    if not check("run pauses for approval", d["status"] == "awaiting_approval", d["status"]):
        return
    print("  waiting for the time budget to pass (up to 3 min) ...")
    found, end = None, time.time() + 60
    while time.time() < end and not found:
        found = next((p for p in api(adm, "GET", "/api/agent/approvals").json()
                      if p["run_id"] == rid and p["actions"][0]["name"] == "declare_p1"), None)
        time.sleep(5)
    if not check("P1 proposal appears in pending approvals", found):
        run = api(adm, "GET", f"/api/agent/runs/{rid}").json()
        print("    run p1 field:", run.get("p1"))
        print("    -> null means the watchdog never proposed: check P1_TIME_BUDGET_MIN, CRITICAL_SERVICES, and 'Watchdog error' in the uvicorn log")
        print("    -> filled in means the proposal exists but /api/agent/approvals does not list it")
        return
    p1 = api(adm, "GET", f"/api/agent/runs/{rid}").json()["p1"]
    check("proposal has draft, recipients and bridge", p1["draft"] and "bridge" in p1, str(p1)[:120])
    check("end user cannot declare", api(usr, "POST", f"/api/agent/runs/{rid}/p1", json={"approve": True}).status_code == 403)
    r = api(adm, "POST", f"/api/agent/runs/{rid}/p1", json={"approve": True, "body": p1["draft"] + "\n(workflow test)"})
    check("declare accepted", r.status_code == 200, r.text[:150])
    st = r.json().get("status")
    check("notice sent (else see the error below)", st == "declared", f"{st}: {r.json().get('notice_error')}")
    r2 = api(adm, "POST", f"/api/agent/runs/{rid}/p1", json={"approve": True})
    check("declaring again is refused or a no-op" if st == "declared" else "retry path answers", r2.status_code in (200, 409))
    # clean up: reject the agent's own pending approval
    api(adm, "POST", f"/api/agent/runs/{rid}/decision", json={"approve": False, "feedback": "workflow test cleanup"})
    print(f"  Check manually: email received, {tk} impact/urgency 1 with 2 work notes, `notices` row exists.")


SCENARIOS = {"access": t_access, "chat": t_chat, "shadow": t_shadow, "auto": t_auto, "p1": t_p1,
             "checker": t_checker, "checker_flap": t_checker_flap}

if __name__ == "__main__":
    names = sys.argv[1:] or ["access", "chat"]
    bad = [n for n in names if n not in SCENARIOS]
    if bad:
        sys.exit(f"Unknown scenario(s): {bad}. Choose from {list(SCENARIOS)}")
    adm = login(os.environ["WF_ADMIN"], os.environ["WF_ADMIN_PASS"])
    usr = login(os.environ["WF_USER"], os.environ["WF_USER_PASS"])
    was_auto = api(adm, "GET", "/api/agent/auto").json().get("enabled", False)
    api(adm, "POST", "/api/agent/auto", json={"enabled": False})        # so auto mode cannot steal our tickets
    try:
        for n in names:
            SCENARIOS[n](adm, usr)
    finally:
        api(adm, "POST", "/api/admin/chat", json={"enabled": True})
        api(adm, "POST", "/api/admin/autonomy", json={"mode": "shadow"})
        api(adm, "POST", "/api/agent/auto", json={"enabled": was_auto})
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)
