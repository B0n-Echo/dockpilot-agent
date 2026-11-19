"""Tests for the fake and real clocks and the timestamp format."""

from datetime import datetime, timedelta

import pytest

from dockpilot.clock import IST, SNAPSHOT_TIME, FakeClock, SystemClock, to_iso


def test_fake_clock_starts_at_snapshot():
    """A new FakeClock reads 2026-08-04 09:45 +05:30."""
    assert FakeClock().now() == datetime(2026, 8, 4, 9, 45, tzinfo=IST)


def test_advance_11_minutes_reads_0956():
    """The contract: moved forward 11 minutes, the clock reads 09:56."""
    clock = FakeClock()
    clock.advance(minutes=11)
    assert clock.now() == datetime(2026, 8, 4, 9, 56, tzinfo=IST)


def test_advance_in_seconds_and_steps_add_up():
    """Moving 30 s gives 09:45:30; a further 1 min 30 s gives 09:47:00."""
    clock = FakeClock()
    assert clock.advance(seconds=30) == datetime(2026, 8, 4, 9, 45, 30, tzinfo=IST)
    clock.advance(minutes=1, seconds=30)
    assert clock.now() == datetime(2026, 8, 4, 9, 47, tzinfo=IST)


def test_advance_backwards_is_refused():
    """Moving backwards raises an error and leaves the time unchanged."""
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.advance(minutes=-1)
    assert clock.now() == SNAPSHOT_TIME


def test_system_clock_is_ist_and_current():
    """SystemClock gives a +05:30 time close to the real current time."""
    now = SystemClock().now()
    assert now.utcoffset() == timedelta(hours=5, minutes=30)
    assert abs(now - datetime.now(IST)) < timedelta(seconds=5)


def test_to_iso_matches_client_format():
    """to_iso writes the snapshot time exactly as the client's data does."""
    assert to_iso(SNAPSHOT_TIME) == "2026-08-04T09:45:00+05:30"
