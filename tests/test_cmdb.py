import re

import pytest

from app.tools import cmdb

H1, H2, A1, A2, S1, S2 = "h1", "h2", "a1", "a2", "s1", "s2"
NAMES = {"victim-test": [H1], "other-host": [H2], "nginx": [A1, A2],
         "customer website": [S1], "intranet": [S2]}
RELS = [(A1, H1), (A2, H2), (S1, A1), (S2, H1)]          # (parent, child)
DETAILS = {
    A1: {"sys_id": A1, "name": "nginx", "sys_class_name": "Application"},
    S1: {"sys_id": S1, "name": "Customer Website", "sys_class_name": "Business Service",
         "business_criticality": "1 - most critical", "owned_by": {"display_value": "Priya"},
         "support_group": "L2_Server_Team"},
    S2: {"sys_id": S2, "name": "Intranet", "sys_class_name": "Business Service",
         "business_criticality": "3 - less critical"},
}


def fake(method, path, identity="agent", **kw):
    q = kw["params"]["sysparm_query"]
    if path == "cmdb_ci" and q.startswith("name="):
        return [{"sys_id": i} for i in NAMES.get(q[5:].lower(), [])]
    if path == "cmdb_rel_ci":
        m = re.fullmatch(r"childIN([\w,]+)\^parentIN([\w,]+)", q)
        if m:
            kids, pars = set(m[1].split(",")), set(m[2].split(","))
            return [{"parent": {"value": p}} for p, c in RELS if c in kids and p in pars]
        kids = set(q[len("childIN"):].split(","))
        return [{"parent": {"value": p}} for p, c in RELS if c in kids]
    if path in ("cmdb_ci", "cmdb_ci_service") and q.startswith("sys_idIN"):
        ids = q[len("sys_idIN"):].split(",")
        return [DETAILS[i] for i in ids if i in DETAILS] if path == "cmdb_ci" else []
    raise AssertionError((path, q))


@pytest.fixture(autouse=True)
def fake_snow(monkeypatch):
    cmdb._CACHE.clear()
    monkeypatch.setattr(cmdb, "_request", fake)
    monkeypatch.delenv("CRITICALITY_AUTONOMY_MAX", raising=False)


def test_service_scope_picks_the_nginx_on_this_host_and_its_website():
    ctx = cmdb.service_context("victim-test", "nginx")
    assert ctx["scope"] == "service" and ctx["found"]
    assert [s["name"] for s in ctx["services"]] == ["Customer Website"]      # not the sibling Intranet
    assert ctx["criticality"] == 1
    assert "Customer Website" in ctx["summary"] and "Priya" in ctx["summary"]


def test_host_scope_reports_but_never_grants_autonomy():
    ctx = cmdb.service_context("victim-test", "apache")                       # no CI named apache
    assert ctx["scope"] == "host" and ctx["criticality"] == 1
    assert {s["name"] for s in ctx["services"]} == {"Customer Website", "Intranet"}
    assert cmdb.critical_by_cmdb("victim-test", "apache") == (False, "")


def test_threshold(monkeypatch):
    assert cmdb.critical_by_cmdb("victim-test", "nginx")[0] is True
    cmdb._CACHE.clear()
    monkeypatch.setenv("CRITICALITY_AUTONOMY_MAX", "0")
    assert cmdb.critical_by_cmdb("victim-test", "nginx")[0] is False


def test_cmdb_error_fails_closed_and_is_not_cached(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("SNOW unreachable")
    monkeypatch.setattr(cmdb, "_request", boom)
    ctx = cmdb.service_context("victim-test", "nginx")
    assert not ctx["found"] and "SNOW" in ctx["error"]
    assert cmdb.critical_by_cmdb("victim-test", "nginx") == (False, "")
    monkeypatch.setattr(cmdb, "_request", fake)
    assert cmdb.service_context("victim-test", "nginx")["found"]