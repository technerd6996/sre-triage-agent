"""Uniform errors for every port. The agent maps any of them to:
add a work note, then escalate."""


class PortError(RuntimeError):
    """Base class. Anything an adapter cannot classify is wrapped in this."""

class NotFound(PortError):
    pass

class AuthFailed(PortError):
    pass


class Unavailable(PortError):
    pass


class Unsupported(PortError):
    """The adapter lacks this capability, or it is not configured."""