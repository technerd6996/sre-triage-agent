import pytest

from app import config


def pytest_configure(config):
    config.addinivalue_line("markers", "real_cc: use the real owner_contacts.cc_for (its ServiceNow call is mocked by the test)")
    config.addinivalue_line("markers", "real_licence: use the real licence check instead of the all-features test plan")


@pytest.fixture(autouse=True)
def _no_cc_lookup(monkeypatch, request):
    """The engineer's Cc address normally comes from their ServiceNow profile; tests must not call ServiceNow."""
    if request.node.get_closest_marker("real_cc"):
        return
    from app import owner_contacts
    monkeypatch.setattr(owner_contacts, "cc_for", lambda login, to: [login] if "@" in login and login not in to else [])


@pytest.fixture(autouse=True)
def _no_saved_settings(monkeypatch):
    """Unit tests see environment variables and defaults only, never the settings table."""
    monkeypatch.setattr(config, "_rows", lambda: {})


@pytest.fixture(autouse=True)
def _full_plan(monkeypatch, request):
    """Tests of the chat and the agent run with every plan feature on; the licence tests use the real check."""
    if request.node.get_closest_marker("real_licence"):
        return
    from app import licence
    monkeypatch.setattr(licence, "current", lambda: {**licence.UNLICENSED, "plan": "Test", "valid": True, "servers": 0, "seats": 0,
                                                      "tickets_month": 0, "features": list(licence.FEATURES)})
