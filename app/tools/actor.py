"""The person on whose behalf a change tool is running outside an agent run (approver chat).

Inside an agent run the tools fetch credentials through pam.checkout(), which needs a run. A person who confirmed an
action in the chat has no run, so infra._win/_linux ask this module who is acting and use the separately limited and
audited pam.checkout_for_action() instead. pam.checkout() itself stays strict."""
import contextlib
import contextvars

_ACTOR: contextvars.ContextVar = contextvars.ContextVar("acting_as", default=None)


@contextlib.contextmanager
def acting_as(user: str, ticket: str):
    """with acting_as("eng@lab.local", "INC0010687"): tool.invoke(...)   (always reset on exit)"""
    token = _ACTOR.set((user, ticket))
    try:
        yield
    finally:
        _ACTOR.reset(token)


_INQUIRER: contextvars.ContextVar = contextvars.ContextVar("inquiring_as", default=None)


@contextlib.contextmanager
def inquiring_as(user: str):
    """A READ-ONLY check an approver asked for in the chat (no ticket, no run). Tools that have no inquiry_user
    parameter, such as check_disk_usage, fetch their credential through the inquiry path while this is set."""
    token = _INQUIRER.set(user)
    try:
        yield
    finally:
        _INQUIRER.reset(token)


def inquirer():
    return _INQUIRER.get()


def current():
    """(user, ticket) while a person's confirmed action is running in this context, else None."""
    return _ACTOR.get()
