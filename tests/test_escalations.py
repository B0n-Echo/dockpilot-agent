"""Tests for escalations (D10, M3): SHP1015 at 16:25 has no feasible slot."""

import io
from datetime import datetime

import pytest

from dockpilot import db
from dockpilot.clock import IST, FakeClock
from dockpilot.escalations import escalate_if_no_options

AT_1625 = datetime(2026, 8, 4, 16, 25, tzinfo=IST)
"""The minute SHP1015's 18:30 ETA was logged (its driver wrote at 16:24)."""


@pytest.fixture
def conn(tmp_path):
    """A fresh working copy in a temporary folder."""
    c = db.connect(db.init_work_db(tmp_path / "work.db"))
    yield c
    c.close()


def _rows(conn, sql):
    """Return all rows of a query."""
    return conn.execute(sql).fetchall()


def test_contract_shp1015_escalation_has_every_m3_field_and_is_printed(conn):
    """Contract: one NO_FEASIBLE_SLOT row with every M3 field filled, and the hand-off printed."""
    out = io.StringIO()
    search, esc = escalate_if_no_options(conn, "SHP1015", FakeClock(AT_1625), out=out)
    assert search.options == [] and esc.reason_code == "NO_FEASIBLE_SLOT"
    assert (esc.driver_id, esc.shipment_id, esc.appointment_id) == ("DRV015", "SHP1015", "APT1015A")
    assert esc.latest_eta_ts == "2026-08-04T18:30:00+05:30" and esc.eta_source == "DRIVER_DECLARED"
    assert esc.options_checked and all(o["failed_because"] for o in esc.options_checked)
    assert esc.driver_constraints and esc.driver_constraints != "None recorded"
    printed = out.getvalue()
    for text in ("SHP1015", "DRV015", "APT1015A", "18:30"):
        assert text in printed
    assert len(_rows(conn, "SELECT * FROM escalations")) == 1


def test_delivered_and_logged(conn):
    """The row is DELIVERED and the diary has RAISED then DELIVERED."""
    _, esc = escalate_if_no_options(conn, "SHP1015", FakeClock(AT_1625), out=io.StringIO())
    assert esc.delivery_status == "DELIVERED"
    events = [r["event_type"] for r in _rows(conn, "SELECT event_type FROM events ORDER BY event_id")]
    assert events == ["ESCALATION_RAISED", "ESCALATION_DELIVERED"]


def test_failed_print_is_marked_failed(conn):
    """If the hand-off can't be printed, the row says FAILED, never DELIVERED."""
    class Broken:
        def write(self, _):
            raise OSError("screen unavailable")
    _, esc = escalate_if_no_options(conn, "SHP1015", FakeClock(AT_1625), out=Broken())
    assert esc.delivery_status == "FAILED"


def test_no_escalation_when_options_exist(conn):
    """SHP1014 at 09:44 has options, so nothing is escalated."""
    search, esc = escalate_if_no_options(conn, "SHP1014", FakeClock(datetime(2026, 8, 4, 9, 44, tzinfo=IST)))
    assert search.options and esc is None
    assert _rows(conn, "SELECT * FROM escalations") == []


def test_no_duplicate_escalations(conn):
    """Escalating the same case twice returns the same record."""
    clock = FakeClock(AT_1625)
    _, first = escalate_if_no_options(conn, "SHP1015", clock, out=io.StringIO())
    _, second = escalate_if_no_options(conn, "SHP1015", clock, out=io.StringIO())
    assert first.escalation_id == second.escalation_id
    assert len(_rows(conn, "SELECT * FROM escalations")) == 1


def test_driver_constraints_come_from_the_clients_data(conn):
    """EXC005 says the earliest acceptable time is 18:30."""
    _, esc = escalate_if_no_options(conn, "SHP1015", FakeClock(AT_1625), out=io.StringIO())
    assert "Earliest 18:30" in esc.driver_constraints
