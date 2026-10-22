"""P0 tests for the Hosts and Context ports. No AWS, ServiceNow or network needed."""
import sys
import types

import pytest

from app.adapters.aws.hosts import AwsHosts, map_error
from app.adapters.servicenow.context import ServiceNowContext
from app.ports import registry
from app.ports.errors import AuthFailed, NotFound, PortError, Unavailable
from app.ports.context import ContextPort
from app.ports.hosts import HostsPort

INST = {"instance_id": "i-1", "name": "web01", "state": "running", "platform": "linux",
        "public_ip": "3.3.3.3", "private_ip": "10.0.0.5"}


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _stub_aws(insts):
    m = types.ModuleType("app.tools.aws")
    m.list_instances = lambda force=False: list(insts.values())

    def get(i):
        if i not in insts:
            raise FakeClientError("InvalidInstanceID.NotFound")
        return dict(insts[i])
    m.get_instance = get
    m.find_in_text = lambda t: next((v for v in insts.values() if v["name"] in t), None)
    return m


@pytest.fixture
def hosts(monkeypatch):
    insts = {"i-1": dict(INST), "i-2": {**INST, "instance_id": "i-2", "name": "db01",
                                        "state": "stopped", "public_ip": ""},
             "i-3": {**INST, "instance_id": "i-3", "name": "app01", "public_ip": ""}}
    monkeypatch.setitem(sys.modules, "app.tools.aws", _stub_aws(insts))
    return AwsHosts()


def test_hosts_contract(hosts):
    assert isinstance(hosts, HostsPort)
    assert [h.id for h in hosts.list_hosts()] == ["i-1", "i-2", "i-3"]
    h = hosts.get_host("i-1")
    assert (h.name, h.os, h.address, h.provider) == ("web01", "linux", "3.3.3.3", "aws")
    assert hosts.find_in_text("disk full on web01").id == "i-1"
    assert hosts.find_in_text("nothing here") is None
    assert hosts.connect_address("i-1") == "3.3.3.3"
    with pytest.raises(NotFound):
        hosts.get_host("i-999")
    with pytest.raises(Unavailable):
        hosts.connect_address("i-2")       # stopped
    with pytest.raises(Unavailable):
        hosts.connect_address("i-3")       # running, no public IP


@pytest.mark.parametrize("exc,cls", [
    (FakeClientError("InvalidInstanceID.Malformed"), NotFound),
    (IndexError("list index out of range"), NotFound),
    (FakeClientError("UnauthorizedOperation"), AuthFailed),
    (FakeClientError("RequestLimitExceeded"), Unavailable),
    (type("NoCredentialsError", (Exception,), {})("no creds"), AuthFailed),
    (type("EndpointConnectionError", (Exception,), {})("down"), Unavailable),
    (RuntimeError("odd"), PortError),
])
def test_aws_error_mapping(exc, cls):
    assert type(map_error(exc)) is cls


def test_context_adapter(monkeypatch):
    cm = types.ModuleType("app.tools.cmdb")
    cm.service_context = lambda h, s="": {"host": h, "service": s, "found": True, "criticality": 1}
    cm.critical_by_cmdb = lambda h, s: (True, "CMDB: critical")
    monkeypatch.setitem(sys.modules, "app.tools.cmdb", cm)
    c = ServiceNowContext()
    assert isinstance(c, ContextPort)
    assert c.service_context("web01", "nginx")["found"] is True
    assert c.is_critical("web01", "nginx") == (True, "CMDB: critical")
    cm.critical_by_cmdb = lambda h, s: (_ for _ in ()).throw(ValueError("bad env"))
    assert c.is_critical("web01", "nginx") == (False, "")      # never raises


def test_registry(monkeypatch):
    for v in ("HOSTS_PROVIDER", "CONTEXT_PROVIDER"):
        monkeypatch.delenv(v, raising=False)
    assert [m.provider for m in registry.available("hosts")] == ["aws"]
    assert [m.provider for m in registry.available("context")] == ["servicenow"]
    assert isinstance(registry.get_hosts(), AwsHosts)
    assert isinstance(registry.get_context(), ServiceNowContext)