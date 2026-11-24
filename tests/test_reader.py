"""Tests for the rule-based message reader."""

import sqlite3
from datetime import datetime

import pytest

from dockpilot import paths
from dockpilot.clock import IST
from dockpilot.reader import read_message


def at(hh, mm):
    """A time on 4 Aug 2026, India time."""
    return datetime(2026, 8, 4, hh, mm, tzinfo=IST)


def _driver_messages():
    """All driver messages with the client's own labels, read from the original data."""
    conn = sqlite3.connect(f"file:{paths.SOURCE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM chat_messages WHERE sender_type = 'DRIVER' ORDER BY message_ts").fetchall()
    conn.close()
    return rows


def test_contract_msg003_msg005_msg007():
    """Contract: intent, ETA and confidence for MSG003, MSG005 (LOW) and MSG007."""
    r = read_message("Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 for another pickup.", at(9, 29))
    assert (r.intent, r.eta, r.eta_confidence, r.leave_by) == ("REPORT_DELAY", at(11, 10), "HIGH", at(13, 30))
    r = read_message("I am late by one hour.", at(9, 19))
    assert (r.intent, r.eta, r.eta_confidence, r.delay_minutes) == ("REPORT_DELAY", None, "LOW", 60)
    assert r.needs_clarification
    r = read_message("Origin released me late. ETA 11:25. This load is urgent. Can I get the first possible dock?", at(9, 44))
    assert (r.intent, r.eta, r.eta_confidence, r.urgent) == ("REPORT_DELAY", at(11, 25), "HIGH", True)


@pytest.mark.parametrize("row", _driver_messages(), ids=lambda r: r["chat_message_id"])
def test_agrees_with_the_clients_labels(row):
    """Each driver message gets the client's intent, ETA (where labelled) and human-review flag."""
    r = read_message(row["message_text"], datetime.fromisoformat(row["message_ts"]))
    assert r.intent == row["parsed_intent"]
    if row["extracted_eta_ts"]:
        assert r.eta == datetime.fromisoformat(row["extracted_eta_ts"])
    assert r.needs_clarification == bool(row["requires_human_review"])


def test_evening_time_without_pm():
    """'ETA 6:30' sent at 16:24 means 18:30."""
    assert read_message("Evening traffic. ETA 6:30.", at(16, 24)).eta == at(18, 30)


def test_delay_reasons():
    """Reasons come from keywords; 'Original' is not 'origin'."""
    assert read_message("Origin released me late. ETA 11:25.", at(9, 44)).delay_reason == "LOADING_DELAY"
    assert read_message("Heavy trailer ETA 12:00. Original heavy-bay slot will be missed.", at(9, 39)).delay_reason is None


def test_yes_and_no():
    """Short replies accept or decline."""
    assert read_message("Yes, book it", at(9, 50)).intent == "CONFIRM"
    assert read_message("no thanks", at(9, 50)).intent == "DECLINE"
