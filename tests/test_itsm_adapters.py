"""CyberArk and the non-ServiceNow ticketing adapters, against recorded-shape fake replies (no real instance was available),
and the rule that a licence key selects exactly one ticketing tool."""
import json
from urllib.parse import parse_qs

import pytest
import requests

from app import config, knowledge, licence, owner_contacts
from app.adapters.cyberark.credentials import CyberArkCredentials
from app.adapters.itsm import base, bmc as _bmc, freshservice as _fresh, jira as _jira, manageengine as _me  # noqa: F401
from app.ports import registry
from app.ports.errors import AuthFailed, NotFound, PortError, Unavailable
from app.ports.models import TicketState
from app.tools import cmdb


class R:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._b, self.text = status, body, text
        self.content = b"x" if body is not None or text else b""

    def json(self):
        if self._b is None:
            raise ValueError("no json")
        return self._b


@pytest.fixture
def cfg(monkeypatch):
    values, real = {}, config.get
    monkeypatch.setattr(config, "get", lambda k: values[k] if k in values else real(k))
    monkeypatch.setattr(config, "get_secret", lambda k: "s3cret-value")
    return values


# ---------------------------------------------------------------- CyberArk
@pytest.fixture
def ccp(cfg, monkeypatch):
    cfg.update({"credentials.ccp_url": "https://ccp.lab", "credentials.ccp_app_id": "TRIAGE1", "credentials.ccp_safe": "Servers"})
    calls, replies = [], []

    def get(url, **kw):
        calls.append((url, kw))
        return replies.pop(0)
    monkeypatch.setattr(requests, "get", get)
    return calls, replies


def test_cyberark_password_account(ccp):
    calls, replies = ccp
    replies.append(R(200, {"Content": "Pw123", "UserName": "Administrator", "Address": "srv"}))
    key, sec = CyberArkCredentials().fetch("i-123")
    assert key == "i-123" and sec == {"username": "Administrator", "password": "Pw123"}
    url, kw = calls[0]
    assert url == "https://ccp.lab/AIMWebService/api/Accounts" and kw["params"]["AppID"] == "TRIAGE1"
    assert kw["params"]["Safe"] == "Servers" and kw["params"]["Object"] == "i-123" and kw["verify"] is True


def test_cyberark_ssh_key_account_is_returned_as_a_private_key(ccp):
    _, replies = ccp
    replies.append(R(200, {"Content": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc", "UserName": "ec2-user"}))
    _, sec = CyberArkCredentials().fetch("i-1")
    assert "password" not in sec and sec["private_key"].startswith("-----BEGIN")


def test_cyberark_tries_the_next_key_then_reports_not_found(ccp):
    calls, replies = ccp
    replies += [R(404, {"ErrorCode": "APPAP004E"}), R(200, {"Content": "x", "UserName": "u"})]
    assert CyberArkCredentials().fetch("id", "10.0.0.1")[0] == "10.0.0.1"
    replies += [R(404, {"ErrorCode": "APPAP004E"})]
    with pytest.raises(NotFound):
        CyberArkCredentials().fetch("gone")


def test_cyberark_errors_never_carry_the_reply(ccp):
    _, replies = ccp
    replies.append(R(403, {"ErrorCode": "APPAP282E", "ErrorMsg": "secret detail"}))
    with pytest.raises(AuthFailed) as e:
        CyberArkCredentials().fetch("x")
    assert "secret detail" not in str(e.value)
    replies.append(R(500, {"ErrorCode": "X"}))
    with pytest.raises(Unavailable):
        CyberArkCredentials().fetch("x")


def test_cyberark_needs_https_and_settings(cfg, monkeypatch):
    with pytest.raises(Unavailable, match="not configured"):
        CyberArkCredentials().fetch("x")
    cfg.update({"credentials.ccp_url": "http://ccp.lab", "credentials.ccp_app_id": "A", "credentials.ccp_safe": "S"})
    with pytest.raises(Unavailable, match="https"):
        CyberArkCredentials().fetch("x")


def test_cyberark_connection_failure_is_unavailable(ccp, monkeypatch):
    def boom(url, **kw):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(Unavailable):
        CyberArkCredentials().fetch("x")


# ---------------------------------------------------------------- ticketing: a scripted HTTP server
@pytest.fixture
def http(monkeypatch):
    log, routes = [], {}

    def request(method, url, **kw):
        log.append((method, url, kw))
        for (m, frag), reply in routes.items():
            if m == method and frag in url:
                return reply(kw) if callable(reply) else reply
        raise AssertionError(f"unexpected call {method} {url}")
    monkeypatch.setattr(requests, "request", request)
    return log, routes


def _calls(log, method, frag):
    return [c for c in log if c[0] == method and frag in c[1]]


# Jira
@pytest.fixture
def jira(cfg, http):
    cfg.update({"jira.url": "https://x.atlassian.net", "jira.email": "a@b.co", "jira.project": "OPS", "jira.assignee": "bot@b.co",
                "jira.group": "L1", "jira.escalation_group": "L2", "jira.team_field": "customfield_1"})
    log, routes = http
    issue = {"key": "OPS-7", "fields": {"summary": "disk full", "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                                        "priority": {"name": "High"}, "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "details"}]}]}}}
    routes.update({("GET", "/issue/OPS-7/transitions"): R(200, {"transitions": [{"id": "31", "name": "Resolve", "to": {"name": "Resolved"}}, {"id": "11", "name": "In Progress", "to": {"name": "In Progress"}}]}),
                   ("POST", "/issue/OPS-7/transitions"): R(204), ("POST", "/issue/OPS-7/comment"): R(201, {}), ("PUT", "/issue/OPS-7"): R(204),
                   ("GET", "/issue/OPS-7"): R(200, issue), ("GET", "/user/search"): R(200, [{"accountId": "acc1", "displayName": "Bot", "emailAddress": "bot@b.co"}]),
                   ("POST", "/rest/api/3/issue"): R(201, {"key": "OPS-7"}), ("GET", "/search/jql"): R(200, {"issues": [{"key": "OPS-7"}, {"key": "OPS-8"}]})})
    return _jira.JiraTicketing(), log


def test_jira_reads_a_ticket(jira):
    tk, _ = jira
    t = tk.get("OPS-7")
    assert (t.id, t.title, t.description, t.state, t.provider) == ("OPS-7", "disk full", "details", TicketState.IN_PROGRESS, "jira")


def test_jira_note_is_internal(jira):
    tk, log = jira
    tk.add_note("OPS-7", "hello")
    body = _calls(log, "POST", "/comment")[0][2]["json"]
    assert body["properties"] == [{"key": "sd.public.comment", "value": {"internal": True}}] and body["body"]["type"] == "doc"


def test_jira_resolve_uses_the_workflow_transition(jira):
    tk, log = jira
    tk.resolve("OPS-7", "fixed")
    assert _calls(log, "POST", "/transitions")[0][2]["json"] == {"transition": {"id": "31"}}


def test_jira_queue_and_exclude(jira):
    tk, log = jira
    assert tk.list_queue(exclude=["OPS-8"]) == ["OPS-7"]
    assert 'project = "OPS"' in _calls(log, "GET", "/search/jql")[0][2]["params"]["jql"]


def test_jira_claim_and_escalate(jira):
    tk, log = jira
    tk.claim("OPS-7", "picked up")
    assert _calls(log, "PUT", "/issue/OPS-7")[0][2]["json"]["fields"]["assignee"] == {"accountId": "acc1"}
    tk.escalate("OPS-7", "needs L2")
    f = _calls(log, "PUT", "/issue/OPS-7")[-1][2]["json"]["fields"]
    assert f["customfield_1"] == {"name": "L2"} and f["assignee"] is None


def test_jira_missing_transition_is_a_clear_error(jira):
    tk, _ = jira
    with pytest.raises(PortError, match="no transition"):
        tk.close("OPS-7", "done")


def test_jira_query_text_cannot_carry_operators(jira):
    tk, log = jira
    tk.open_tickets("a@b.co", host='x" OR 1=1')
    assert 'text ~ "x OR 11" ORDER BY' in _calls(log, "GET", "/search/jql")[0][2]["params"]["jql"]      # the quote in the input is gone


@pytest.mark.parametrize("code,err", [(401, AuthFailed), (403, AuthFailed), (404, NotFound), (429, Unavailable), (503, Unavailable), (400, PortError)])
def test_http_failures_map_to_port_errors(jira, http, code, err):
    tk, _ = jira
    http[1].clear()
    http[1][("GET", "/myself")] = R(code, {"errorMessages": ["secret detail"]})
    with pytest.raises(err) as e:
        tk.ping()
    assert "secret detail" not in str(e.value)


# Freshservice
@pytest.fixture
def fresh(cfg, http):
    cfg.update({"freshservice.url": "https://x.freshservice.com", "freshservice.group": "L1", "freshservice.escalation_group": "L2", "freshservice.assignee": "bot@b.co",
                "freshservice.requester_email": "desk@b.co"})
    log, routes = http
    t = {"id": 55, "subject": "cpu high", "status": 2, "priority": 3, "description_text": "d"}
    routes.update({("POST", "/tickets/55/notes"): R(201, {}), ("GET", "/tickets/55"): R(200, {"ticket": t}), ("PUT", "/tickets/55"): R(200, {"ticket": t}),
                   ("POST", "/api/v2/tickets"): R(201, {"ticket": t}), ("GET", "/api/v2/groups"): R(200, {"groups": [{"id": 9, "name": "L1"}, {"id": 10, "name": "L2"}]}),
                   ("GET", "/api/v2/agents"): R(200, {"agents": [{"id": 77}]}), ("GET", "/tickets/filter"): R(200, {"tickets": [t]})})
    return _fresh.FreshserviceTicketing(), log


def test_freshservice_read_queue_and_private_note(fresh):
    tk, log = fresh
    assert tk.get("55").state is TicketState.NEW and tk.list_queue() == ["55"]
    assert "group_id:9" in _calls(log, "GET", "/tickets/filter")[0][2]["params"]["query"]
    tk.add_note("55", "a\nb")
    assert _calls(log, "POST", "/notes")[0][2]["json"] == {"body": "a<br>b", "private": True}


def test_freshservice_state_group_and_reversed_priority(fresh):
    tk, log = fresh
    tk.resolve("55", "ok")
    assert _calls(log, "PUT", "/tickets/55")[-1][2]["json"] == {"status": 4}
    tk.escalate("55", "up")
    assert _calls(log, "PUT", "/tickets/55")[-1][2]["json"] == {"group_id": 10, "responder_id": None}
    tk.set_severity("55", 1, 1, "sev")
    assert _calls(log, "PUT", "/tickets/55")[-1][2]["json"] == {"priority": 4}      # 4 is urgent in Freshservice


def test_freshservice_create_and_bad_id(fresh):
    tk, log = fresh
    tk.create("t", "d", 2, 2, "", "srv1")
    body = _calls(log, "POST", "/api/v2/tickets")[0][2]["json"]
    assert body["email"] == "desk@b.co" and body["group_id"] == 9 and body["responder_id"] == 77 and "srv1" in body["description"]
    with pytest.raises(NotFound):
        tk.get("../etc")


# ManageEngine
@pytest.fixture
def me(cfg, http):
    cfg.update({"manageengine.url": "https://sdp.lab", "manageengine.group": "L1", "manageengine.escalation_group": "L2", "manageengine.assignee": "bot@b.co"})
    log, routes = http
    r = {"id": "101", "subject": "svc down", "status": {"name": "Open"}, "priority": {"name": "High"}, "group": {"name": "L1"}}
    routes.update({("POST", "/requests/101/notes"): R(201, {}), ("GET", "/api/v3/requests/101"): R(200, {"request": r}), ("PUT", "/api/v3/requests/101"): R(200, {"request": r}),
                   ("POST", "/api/v3/requests"): R(201, {"request": r}), ("GET", "/api/v3/requests"): R(200, {"requests": [r, {**r, "id": "102", "technician": {"name": "x"}}]})})
    return _me.ManageEngineTicketing(), log


def _input(call):
    return json.loads(call[2]["data"]["input_data"])


def test_manageengine_auth_header_read_and_unassigned_queue(me):
    tk, log = me
    assert tk.get("101").state is TicketState.NEW and tk.list_queue() == ["101"]
    assert log[0][2]["headers"]["authtoken"] == "s3cret-value"


def test_manageengine_note_and_status_use_input_data(me):
    tk, log = me
    tk.resolve("101", "fine")
    assert _input(_calls(log, "POST", "/notes")[0])["note"]["show_to_requester"] is False
    assert _input(_calls(log, "PUT", "/requests/101")[-1])["request"]["status"] == {"name": "Resolved"}


def test_manageengine_create_for_a_person_stays_out_of_the_queue(me):
    tk, log = me
    tk.create_for_user("t", "d", "eng@b.co")
    body = _input(_calls(log, "POST", "/api/v3/requests")[0])["request"]
    assert body["technician"] == {"email_id": "eng@b.co"} and "group" not in body


# BMC
@pytest.fixture
def bmc(cfg, http, monkeypatch):
    cfg.update({"bmc.url": "https://helix.lab", "bmc.user": "svc", "bmc.group": "L1", "bmc.escalation_group": "L2", "bmc.assignee": "bot", "bmc.company": "Acme"})
    monkeypatch.setitem(_bmc._token, "value", "")
    monkeypatch.setattr(requests, "post", lambda url, **kw: R(200, text="JWT123"))
    log, routes = http
    v = {"Incident Number": "INC000000000042", "Description": "db slow", "Status": "Assigned", "Priority": "High"}
    routes.update({("GET", "HPD:IncidentInterface?"): R(200, {"entries": []}),
                   ("POST", "HPD:WorkLog"): R(201), ("PUT", "/entry/HPD:IncidentInterface/"): R(204),
                   ("POST", "HPD:IncidentInterface_Create"): R(201, {"values": {"Incident Number": "INC000000000042"}}),
                   ("GET", "HPD:IncidentInterface"): R(200, {"entries": [{"values": v, "_links": {"self": [{"href": "https://helix.lab/api/arsys/v1/entry/HPD:IncidentInterface/000123"}]}}]})})
    return _bmc.BmcTicketing(), log


def test_bmc_signs_in_with_a_jwt_and_reads(bmc):
    tk, log = bmc
    t = tk.get("INC000000000042")
    assert t.state is TicketState.NEW and t.title == "db slow" and log[0][2]["headers"]["Authorization"] == "AR-JWT JWT123"


def test_bmc_refused_login(bmc, monkeypatch):
    tk, _ = bmc
    monkeypatch.setattr(requests, "post", lambda url, **kw: R(401, text="no"))
    with pytest.raises(AuthFailed):
        tk.ping()


def test_bmc_resolve_updates_status_on_the_entry_and_writes_a_worklog(bmc):
    tk, log = bmc
    tk.resolve("INC000000000042", "done")
    assert _calls(log, "POST", "HPD:WorkLog")[0][2]["json"]["values"]["View Access"] == "Internal"
    assert _calls(log, "PUT", "/000123")[0][2]["json"]["values"]["Status"] == "Resolved"


def test_bmc_create_returns_the_new_incident(bmc):
    tk, log = bmc
    assert tk.create("t", "d", 2, 2).id == "INC000000000042"
    assert _calls(log, "POST", "_Create")[0][2]["json"]["values"]["Company"] == "Acme"


# ---------------------------------------------------------------- every adapter fits the port
def test_every_adapter_implements_the_ticketing_port():
    from app.ports.ticketing import TicketingPort
    for cls in (_jira.JiraTicketing, _fresh.FreshserviceTicketing, _me.ManageEngineTicketing, _bmc.BmcTicketing):
        assert isinstance(cls(), TicketingPort)


def test_an_address_without_https_is_refused_before_any_credential_is_sent(cfg, http):
    cfg["jira.url"] = "http://x.atlassian.net"
    with pytest.raises(PortError, match="https"):
        _jira.JiraTicketing().ping()
    assert http[0] == []


# ---------------------------------------------------------------- the licence picks the tool
def _tool(monkeypatch, name):
    monkeypatch.setattr(licence, "current", lambda: {"itsm": name})


def test_the_licence_selects_the_ticketing_tool_and_the_environment_cannot_override_it(monkeypatch):
    monkeypatch.setenv("TICKETING_PROVIDER", "servicenow")
    _tool(monkeypatch, "jira")
    assert registry.active_provider("ticketing", "servicenow") == "jira" and not registry.is_servicenow()
    assert isinstance(registry.get_ticketing(), _jira.JiraTicketing)


def test_a_key_without_a_tool_keeps_the_default(monkeypatch):
    monkeypatch.delenv("TICKETING_PROVIDER", raising=False)
    _tool(monkeypatch, "")
    assert registry.is_servicenow()


def test_cyberark_is_chosen_in_the_settings(cfg):
    cfg["credentials.provider"] = "cyberark"
    assert isinstance(registry.get_credentials(), CyberArkCredentials)


def test_servicenow_only_features_switch_off_with_a_reason(monkeypatch):
    _tool(monkeypatch, "freshservice")
    assert "another ticketing tool" in owner_contacts.contacts("srv1")["error"]
    cmdb._CACHE.clear()
    ctx = cmdb.service_context("srv1")
    assert ctx["found"] is False and "another ticketing tool" in ctx["error"]
    assert knowledge.relationships("what depends on srv1") == ""
    assert knowledge.retrieve("nginx 502")["articles"] == []


def test_the_admin_panel_shows_only_the_licensed_tool(monkeypatch):
    from app.api import config_pane
    _tool(monkeypatch, "jira")
    monkeypatch.setattr(config_pane, "_bootstrap_items", lambda: [])
    ids = [s["id"] for s in config_pane.sections()["sections"]]
    assert "jira" in ids and "ticketing" not in ids and "bmc" not in ids


def test_priority_and_query_helpers():
    assert base.priority_of(1, 1) == 1 and base.priority_of(3, 3) == 4 and base.priority_of(2, 2) == 3
    assert base.clean('a"b OR ^x') == "ab OR x"
    assert parse_qs("a=1") == {"a": ["1"]}
