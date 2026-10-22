"""Tests for the active-server state and the host resolver, using a fake Hosts adapter."""
import pytest

from app.agent import hosts
from app.ports import registry
from app.ports.errors import Unavailable
from app.ports.manifest import Manifest
from app.ports.models import Host
from app.tools import active

H = [Host("i-1", "web01", "linux", "running", "3.3.3.3", "10.0.0.5", "fake"),
     Host("w-1", "win01", "windows", "running", "4.4.4.4", "10.0.0.6", "fake"),
     Host("w-2", "win02", "windows", "stopped", "", "10.0.0.7", "fake")]


class FakeHosts:
    provider, capabilities, broken = "fake", frozenset(), False

    def list_hosts(self, force=False):
        return list(H)

    def get_host(self, host_id):
        return next(h for h in H if h.id == host_id)

    def find_in_text(self, text):
        if FakeHosts.broken:
            raise RuntimeError("boom")
        return next((h for h in H if h.name in text), None)

    def connect_address(self, host_id):
        h = self.get_host(host_id)
        if h.state != "running":
            raise Unavailable(f"Instance {h.name} is {h.state}")
        return h.address


def _use_fake(monkeypatch):
    registry.register(Manifest(port="hosts", provider="fake", title="Fake"), FakeHosts)
    monkeypatch.setenv("HOSTS_PROVIDER", "fake")
    active.ACTIVE.clear()
    FakeHosts.broken = False


def _cleanup():
    registry._ADAPTERS.pop(("hosts", "fake"), None)
    active.ACTIVE.clear()


def test_resolve_server(monkeypatch):
    _use_fake(monkeypatch)
    try:
        inst, why = hosts.resolve_server("disk full on web01")
        assert why == "" and inst["platform"] == "linux" and inst["name"] == "web01"
        assert set(inst) == {"instance_id", "name", "platform", "state", "public_ip", "private_ip"}
        assert active.ACTIVE == {"linux": "i-1"}
        inst, why = hosts.resolve_server("nothing named here")
        assert inst is None and "No known instance" in why and active.ACTIVE == {}
        FakeHosts.broken = True
        inst, why = hosts.resolve_server("web01")
        assert inst is None and why.startswith("Host lookup failed")
    finally:
        _cleanup()


def test_default_windows_needs_exactly_one_running(monkeypatch):
    _use_fake(monkeypatch)
    try:
        inst = hosts.resolve_default_windows()          # win01 running, win02 stopped -> exactly one
        assert inst["instance_id"] == "w-1" and active.ACTIVE == {"windows": "w-1"}
        H.append(Host("w-3", "win03", "windows", "running", "5.5.5.5", "", "fake"))
        active.ACTIVE.clear()
        assert hosts.resolve_default_windows() is None  # two running -> ambiguous -> no pick
        assert active.ACTIVE == {}
    finally:
        H.pop()
        _cleanup()


def test_current_address(monkeypatch):
    _use_fake(monkeypatch)
    try:
        assert active.current_address("linux") is None
        active.ACTIVE["linux"] = "i-1"
        assert active.current_address("linux") == "3.3.3.3"
        active.ACTIVE["windows"] = "w-2"
        with pytest.raises(Unavailable):
            active.current_address("windows")           # stopped
    finally:
        _cleanup()