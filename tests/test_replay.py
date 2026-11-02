import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("SKILLS_SOURCE", "file")

from replay.run import CASES, task_message
from replay.world import World, all_tools, patched

CASE_LIST = json.loads(Path(CASES).read_text(encoding="utf-8"))
BY_ID = {c["id"]: c for c in CASE_LIST}


def test_case_ids_unique_and_complete():
    ids = [c["id"] for c in CASE_LIST]
    assert len(ids) == len(set(ids))
    for c in CASE_LIST:
        assert {"short_description", "host", "expect"} <= set(c), c["id"]
        assert c["expect"]["outcome"] in ("resolved", "escalated"), c["id"]


@pytest.mark.parametrize("case", CASE_LIST, ids=[c["id"] for c in CASE_LIST])
def test_each_case_matches_its_playbook_and_tools(case):
    _, skill, allowed = task_message(case)
    want = case["expect"].get("skill", "*")
    if want != "*":
        assert (skill or None) == want
    assert allowed <= {t.name for t in all_tools()}


def _tool(name):
    return next(t for t in all_tools() if t.name == name)


def test_service_flow_uses_the_real_tools():
    w = World(BY_ID["win-spooler-fixable"])
    with patched(w):
        r = _tool("check_service_status").invoke({"target": "windows", "service_name": "Spooler"})
        assert r["status"] == "stopped" and "start_service" in r["next_step"]
        r = _tool("start_service").invoke({"target": "windows", "service_name": "Spooler"})
        assert r["status"] == "active" and "resolve_incident" in r["next_step"]
    assert w.goal_met()


def test_failed_start_leads_to_escalation_step():
    w = World(BY_ID["lin-nginx-start-fails"])
    with patched(w):
        r = _tool("start_service").invoke({"target": "linux", "service_name": "nginx"})
        assert "escalate_incident" in r["next_step"]
    assert not w.goal_met()


def test_disk_cleanup_flow():
    w = World(BY_ID["win-disk-fixable"])
    with patched(w):
        r = _tool("check_disk_usage").invoke({"target": "windows"})
        assert r["over_threshold"] == ["C:"]
        _tool("clear_temp_files").invoke({"target": "windows"})
        r = _tool("check_disk_usage").invoke({"target": "windows"})
        assert r["over_threshold"] == [] and "resolve_incident" in r["next_step"]


def test_linux_disk_parsing():
    w = World(BY_ID["lin-disk-fixable"])
    with patched(w):
        r = _tool("check_disk_usage").invoke({"target": "linux"})
        assert r["disks"][0]["disk"] == "/" and r["over_threshold"] == ["/"]


def test_unreachable_host_and_wrong_target(monkeypatch):
    monkeypatch.delenv("LINUX_HOST", raising=False)
    monkeypatch.delenv("WIN_HOST", raising=False)
    w = World(BY_ID["win-spooler-host-unreachable"])
    with patched(w):
        assert _tool("check_reachability").invoke({"target": "windows"})["reachable"] is False
        assert "error" in _tool("check_service_status").invoke({"target": "linux", "service_name": "nginx"})


def test_ad_unlock_flow():
    w = World(BY_ID["ad-locked-fixable"])
    with patched(w):
        r = _tool("lookup_ad_user").invoke({"username": "jdoe"})
        assert "unlock_ad_account" in r["next_step"]
        _tool("unlock_ad_account").invoke({"username": "jdoe"})
    assert w.goal_met()


def test_trap_case_resolves_honestly_then_breaks():
    w = World(BY_ID["win-spooler-dies-again"])
    with patched(w):
        _tool("start_service").invoke({"target": "windows", "service_name": "Spooler"})
        from replay.world import FakeTicketing
        FakeTicketing(w).resolve(w.number, "done")
    assert w.truth_at_close is True and not w.goal_met()