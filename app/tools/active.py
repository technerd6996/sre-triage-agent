"""The server the current ticket is about. Same behaviour as the old aws.ACTIVE / aws.STRICT,
but vendor-neutral: the address comes from whichever Hosts adapter is active."""
from app.ports import registry

import contextvars

# "windows" / "linux" -> host id, for the run the current thread is working. A ContextVar, so each
# run thread (and the LangGraph tool workers it starts, which copy its context) sees only its own server.
_HOSTS: contextvars.ContextVar = contextvars.ContextVar("run_hosts", default=None)
STRICT = False         # the agent sets this True: no fallback to the static hosts in .env


def bind(hosts: dict | None) -> None:
    """Set the servers for the run this thread is working. Replaces any earlier binding."""
    _HOSTS.set(dict(hosts or {}))


def bind_run(run: dict) -> None:
    """Bind from a stored run's `host`. Used on resume, including after a restart."""
    h = run.get("host") or {}
    bind({h["os"]: h["id"]} if h.get("os") and h.get("id") else {})


def host_id(target: str):
    return (_HOSTS.get() or {}).get(target)


def snapshot() -> dict:
    return dict(_HOSTS.get() or {})


def current_address(target: str):
    """Address of the active host for this platform, or None if no host was resolved.
    Raises Unavailable (a RuntimeError) if the host is stopped or has no address."""
    hid = host_id(target)
    if not hid:
        return None
    return registry.get_hosts().connect_address(hid)