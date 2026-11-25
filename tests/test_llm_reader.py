"""Tests for the AI reader. No test calls the network: the AI is replaced by a fake."""

import json
from datetime import datetime

import pytest

from dockpilot import llm_reader
from dockpilot.clock import IST
from dockpilot.llm_reader import load_env, read_message_ai
from dockpilot.reader import read_message


def at(hh, mm):
    """A time on 4 Aug 2026, India time."""
    return datetime(2026, 8, 4, hh, mm, tzinfo=IST)


def fake(answer):
    """A stand-in AI that always replies with `answer` (a dict as JSON, or raw text)."""
    return lambda messages: answer if isinstance(answer, str) else json.dumps(answer)


@pytest.fixture(autouse=True)
def no_key(monkeypatch, tmp_path):
    """Every test runs with no key: no real .env, no OPENROUTER_ variables."""
    for k in ("OPENROUTER_API_KEY", "OPENROUTER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm_reader.load_env, "__defaults__", (tmp_path / "none.env",))


def test_contract_no_key_falls_back_to_rules():
    """Without a key, the result is exactly the rules' reading."""
    text = "I am late by one hour."
    assert read_message_ai(text, at(9, 19)) == read_message(text, at(9, 19))
    assert read_message_ai(text, at(9, 19)).read_by == "rules"


def test_contract_ai_answer_gives_the_same_fields():
    """A valid AI answer becomes a Reading with the same fields, marked as read by the AI."""
    answer = {"intent": "REPORT_DELAY", "eta": "11:20", "eta_confidence": "MEDIUM", "delay_minutes": None,
              "delay_reason": "TRAFFIC", "not_before": "12:00", "leave_by": None, "urgent": False, "shipment_ref": None}
    r = read_message_ai("Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", at(9, 34), call=fake(answer))
    assert (r.read_by, r.intent, r.eta, r.not_before, r.eta_confidence) == ("ai", "REPORT_DELAY", at(11, 20), at(12, 0), "MEDIUM")


def test_contract_failed_call_falls_back_without_crashing():
    """A network error gives the rules' reading."""
    def broken(messages):
        raise OSError("network down")
    r = read_message_ai("Origin released me late. ETA 11:25.", at(9, 44), call=broken)
    assert r.read_by == "rules" and r.eta == at(11, 25)


@pytest.mark.parametrize("answer", ["not json at all", {"intent": "BOOK_ME_NOW"}, {"intent": "REPORT_DELAY", "eta": "25:99"}])
def test_bad_ai_answer_falls_back(answer):
    """Broken JSON, an unknown intent or an impossible time: rules' reading."""
    assert read_message_ai("ETA 11:25", at(9, 44), call=fake(answer)).read_by == "rules"


def test_caution_wins_when_rules_say_ask_first():
    """The AI invents a time for 'late by one hour'; the agent still asks first."""
    answer = {"intent": "REPORT_DELAY", "eta": "10:19", "eta_confidence": "HIGH", "urgent": False}
    r = read_message_ai("I am late by one hour.", at(9, 19), call=fake(answer))
    assert r.read_by == "ai" and r.needs_clarification


def test_load_env_reads_a_file(tmp_path):
    """KEY=VALUE lines are read; comments are skipped."""
    env = tmp_path / ".env"
    env.write_text("# comment\nOPENROUTER_MODEL=some/model\nOPENROUTER_API_KEY='abc'\n")
    assert load_env(env) == {"OPENROUTER_MODEL": "some/model", "OPENROUTER_API_KEY": "abc"}
