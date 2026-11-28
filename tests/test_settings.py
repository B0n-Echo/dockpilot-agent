"""Tests for the shared settings."""

import dataclasses

import pytest

from dockpilot.settings import SETTINGS


def test_defaults_match_agreed_values():
    """SETTINGS holds the seven agreed values."""
    assert dataclasses.asdict(SETTINGS) == {
        "hold_minutes": 10,
        "spike_hold_minutes": 5,
        "spike_request_count": 5,
        "spike_window_minutes": 10,
        "collection_window_seconds": 30,
        "warehouse_timeout_minutes": 30,
        "eta_buffer_minutes": 30,
        "max_options": 3,
        "login_code_minutes": 5,
        "login_max_tries": 3,
        "login_codes_per_window": 3,
        "login_window_minutes": 15,
        "login_lockout_minutes": 15,
        "session_hours": 12,
        "nudge_after_minutes": 5,
        "close_after_minutes": 5,
        "near_tie_minutes": 15,
    }


def test_settings_are_read_only():
    """Changing a value on SETTINGS raises an error."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        SETTINGS.hold_minutes = 1


def test_replace_makes_a_copy_and_leaves_shared_settings_alone():
    """dataclasses.replace changes the copy only."""
    custom = dataclasses.replace(SETTINGS, warehouse_timeout_minutes=1)
    assert custom.warehouse_timeout_minutes == 1
    assert SETTINGS.warehouse_timeout_minutes == 30
