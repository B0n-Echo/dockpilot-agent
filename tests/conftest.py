"""Shared test settings: tests stay offline and quiet whatever is in .env."""

import pytest

from dockpilot import trace


@pytest.fixture(autouse=True)
def no_tracing_by_default(monkeypatch):
    """Tracing off and LangSmith unreachable in every test, even with LANGSMITH_TRACING=true in .env;
    tests/test_trace.py turns recording on where it needs it."""
    trace.set_enabled(False)
    monkeypatch.setattr(trace, "_langsmith_settings", lambda: (None, "dockpilot", None))
    yield
    trace.set_enabled(False)
