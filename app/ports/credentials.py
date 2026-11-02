"""Credentials port: where host logins are stored (AWS Secrets Manager, OpenBao, ...)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CredentialsPort(Protocol):
    provider: str
    capabilities: frozenset

    def fetch(self, *keys: str) -> tuple[str, dict]:
        """Return (the key that matched, the stored secret) for the first key that has one.
        Raises NotFound when none do, Unavailable when the store cannot be reached.
        The secret must never be logged."""
        ...