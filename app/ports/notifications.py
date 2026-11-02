"""Notifications port: send one message to people (SMTP, SES, SNS, later Teams or Slack)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

CAP_RECIPIENTS = "recipients"     # the To and Cc lists are used
CAP_BROADCAST = "broadcast"       # delivers to a fixed audience (an SNS topic); To and Cc are ignored


@runtime_checkable
class NotificationsPort(Protocol):
    provider: str
    capabilities: frozenset

    def send(self, to: list, cc: list, subject: str, body: str) -> str:
        """Return a message id. Raises Unsupported when not configured, Unavailable when the send fails."""
        ...

    def check(self) -> str:
        """Cheap readiness check, no message sent. Returns a short detail; raises like send()."""
        ...