"""All of DockPilot's agreed numbers, in one place.

Code reads SETTINGS.<field>. A test that needs a different value makes its own copy with
dataclasses.replace(SETTINGS, field=value) and passes it in; SETTINGS itself never changes.

The client's facility rules (no-show grace, heavy dock, last start time) are NOT here: they
live in the client's facility_rules table and differ per facility.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """A read-only set of DockPilot's tunable values. Once created, it cannot be changed."""

    hold_minutes: int = 10
    """How long a slot is held for a driver to decide (D5)."""

    spike_hold_minutes: int = 5
    """How long a slot is held while spike mode is on (D5)."""

    spike_request_count: int = 5
    """Spike mode turns on when a facility gets this many slot requests... (U4 / Q3)"""

    spike_window_minutes: int = 10
    """...within this many minutes (U4 / Q3)."""

    collection_window_seconds: int = 30
    """In spike mode, requests for one slot arriving this close together are ranked before a winner is held (D7)."""

    warehouse_timeout_minutes: int = 30
    """How long to wait for the warehouse to confirm before a coordinator chases (D6).
    Our working default; replace with the client's answer to Q2."""

    eta_buffer_minutes: int = 30
    """Slack added between a low-confidence ETA and the slot start (R7).
    Our working default; replace with the client's answer to Q6."""


SETTINGS = Settings()
"""The shared settings with the agreed defaults."""
