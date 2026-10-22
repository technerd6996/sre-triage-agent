"""Vendor-neutral data shapes shared by the core and all adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TicketState(str, Enum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    RESOLVED = "resolved"
    CLOSED = "closed"
    UNKNOWN = "unknown"      # adapter could not map it; raw value is in extras["raw_state"]


@dataclass(frozen=True)
class TicketRef:
    """What runs and audit rows store instead of a bare incident number."""
    provider: str
    external_id: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.external_id}"

    @classmethod
    def parse(cls, text: str, default_provider: str = "servicenow") -> "TicketRef":
        # Old rows hold a bare number such as INC0010001; treat those as the default provider.
        if ":" in text:
            provider, ext = text.split(":", 1)
            return cls(provider, ext)
        return cls(default_provider, text)


@dataclass
class Ticket:
    id: str
    title: str = ""
    description: str = ""
    state: TicketState = TicketState.UNKNOWN
    priority: str = ""
    group: str = ""
    assignee: str = ""
    caller: str = ""
    host: str = ""
    work_notes: str = ""
    provider: str = ""
    extras: dict = field(default_factory=dict)   # vendor fields pass through here
    raw: dict = field(default_factory=dict, repr=False)   # vendor record, for legacy callers
    
    @property
    def ref(self) -> TicketRef:
        return TicketRef(self.provider, self.id)


@dataclass
class Host:
    id: str
    name: str = ""
    os: str = ""                # "windows" | "linux"
    state: str = ""             # "running", "stopped", ...
    address: str = ""           # what to connect to (public IP today)
    private_address: str = ""
    provider: str = ""
    extras: dict = field(default_factory=dict)


@dataclass
class CredentialLease:
    username: str
    secret: str = ""            # password; never log or show to the model
    private_key: str = ""
    protocol: str = ""
    port: int = 0
    expires_at: float = 0.0