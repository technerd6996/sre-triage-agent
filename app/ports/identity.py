"""Identity port: authenticate users and retrieve their directory groups."""
from __future__ import annotations
from typing import Protocol, runtime_checkable

@runtime_checkable
class IdentityPort(Protocol):
    provider: str
    capabilities: frozenset

    def authenticate(self, username: str, password: str) -> bool: ...
    def get_groups(self, username: str) -> list[str]: ...
    def search_groups(self, prefix: str) -> list[dict]: ...