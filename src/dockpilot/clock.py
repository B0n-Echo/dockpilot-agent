"""The one place DockPilot asks "what time is it?".

Functions that need the time receive a clock as an argument instead of reading the
computer's time directly. Tests pass a FakeClock they can move forward; live use passes
a SystemClock.
"""

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")
"""India Standard Time (+05:30), the zone of every timestamp in the client's data."""

SNAPSHOT_TIME = datetime(2026, 8, 4, 9, 45, tzinfo=IST)
"""When the client's data snapshot was taken: 2026-08-04 09:45 +05:30. FakeClock starts here."""


class SystemClock:
    """The real clock, used when DockPilot runs live."""

    def now(self):
        """Return the current real time in India time.

        Returns:
            A timezone-aware datetime in IST.
        """
        return datetime.now(IST)


class FakeClock:
    """A clock for tests: it shows a set time and only moves when told to."""

    def __init__(self, start=SNAPSHOT_TIME):
        """Create a fake clock.

        Args:
            start: the time the clock shows at first. Must carry a time zone.
                Defaults to SNAPSHOT_TIME.

        Raises:
            ValueError: if `start` has no time zone.
        """
        if start.tzinfo is None:
            raise ValueError("start must carry a time zone")
        self._now = start

    def now(self):
        """Return the time the clock currently shows.

        Returns:
            A timezone-aware datetime.
        """
        return self._now

    def advance(self, minutes=0, seconds=0):
        """Move the clock forward.

        Args:
            minutes: how many minutes to move forward.
            seconds: how many seconds to move forward (added to `minutes`).

        Returns:
            The new time the clock shows.

        Raises:
            ValueError: if the total step is negative. Real time never goes backwards,
                and a hold that "un-expires" would hide bugs.
        """
        step = timedelta(minutes=minutes, seconds=seconds)
        if step < timedelta(0):
            raise ValueError("a clock cannot go backwards")
        self._now += step
        return self._now


def to_iso(dt):
    """Write a time in the client's text format, e.g. "2026-08-04T09:45:00+05:30".

    Args:
        dt: a timezone-aware datetime (converted to IST if it is in another zone).

    Returns:
        The time as text, to the second, with the +05:30 offset.
    """
    return dt.astimezone(IST).isoformat(timespec="seconds")
