"""Shared test settings: tests stay offline and quiet whatever is in .env."""

import pytest

from dockpilot import trace


@pytest.fixture(autouse=True)
def no_tracing_by_default(monkeypatch):
    """Tracing off, LangSmith unreachable and no warehouse password in every test, whatever .env or the shell say;
    tests/test_trace.py turns recording on where it needs it."""
    trace.set_enabled(False)
    monkeypatch.setattr(trace, "_langsmith_settings", lambda: (None, "dockpilot", None))
    monkeypatch.delenv("WAREHOUSE_PASSWORD", raising=False)  # staff pages open unless a test sets it (dp-23)
    yield
    trace.set_enabled(False)
