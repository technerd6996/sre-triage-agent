import pytest

from app import config


@pytest.fixture(autouse=True)
def _no_saved_settings(monkeypatch):
    """Unit tests see environment variables and defaults only, never the settings table."""
    monkeypatch.setattr(config, "_rows", lambda: {})