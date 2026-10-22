"""Hosts port: which servers exist and how to reach them (today: AWS EC2)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.ports.models import Host

CAP_FIND_IN_TEXT = "find_in_text"
CAP_FRESH_LOOKUP = "fresh_lookup"      # get_host bypasses caches (addresses change on restart)


@runtime_checkable
class HostsPort(Protocol):
    provider: str
    capabilities: frozenset

    def list_hosts(self, force: bool = False) -> list[Host]: ...
    def get_host(self, host_id: str) -> Host: ...
    def find_in_text(self, text: str) -> Host | None: ...       # exactly one match, else None
    def connect_address(self, host_id: str) -> str: ...         # raises Unavailable if not reachable