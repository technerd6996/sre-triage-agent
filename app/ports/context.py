"""Context port: which business services depend on a host, and how critical they are (today: CMDB)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ContextPort(Protocol):
    provider: str
    capabilities: frozenset

    def service_context(self, host: str, service: str = "") -> dict: ...   # never raises
    def is_critical(self, host: str, service: str) -> tuple[bool, str]: ...