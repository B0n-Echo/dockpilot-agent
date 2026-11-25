"""Tests for the scenario replay (dp-16): the client's seeded cases on one timeline, rule reader."""

import pytest

from dockpilot.scenarios import CASES, run_scenarios


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One full replay into a temporary folder; (measures, output folder)."""
    out = tmp_path_factory.mktemp("scenarios")
    return run_scenarios(out_dir=out), out


def test_contract_hard_targets(run):
    """M1 = 0, M2 = 0, M4 = 0, and M3: every no-slot reply escalated with a complete hand-off (100% / 100%)."""
    m, _ = run
    assert m["M1"] == [] and m["M2"] == [] and m["M4"] == []
    m3 = m["M3"]
    assert m3["no_slot_replies"] >= 1 and m3["escalated"] == m3["no_slot_replies"] == m3["complete"]


def test_contract_every_case_has_a_transcript_and_the_report_exists(run):
    """14 transcripts plus report.md, each transcript naming its seed reference."""
    _, out = run
    for case, c in CASES.items():
        assert c.seed in (out / f"{case}.md").read_text()
    assert "M1" in (out / "report.md").read_text()


def test_key_outcomes(run):
    """The cases end as the replay is designed to show."""
    m, out = run
    outcome = {c: v["outcome"] for c, v in m["cases"].items()}
    assert outcome == {
        "S01": "CONFIRMED", "S02": "CONFIRMED", "S03": "CONFIRMED", "S04": "CONFIRMED", "S05": "ESCALATED",
        "S06": "ESCALATED", "S07": "BOOKING_STANDS", "S08": "ANSWERED", "S09": "RESEND_RECOGNISED", "S10": "CONFIRMED",
        "S11": "DECLINED", "S12": "REFUSED", "S13": "CONFIRMED", "S14": "HANDLED"}
    assert m["cases"]["S03"]["coordinator"]  # silent at 09:19 -> closed with ETA_UNRESOLVED (team rule)
    assert "Confirmed: D1 at 20:00" in (out / "S10.md").read_text()  # B1 + B4: the delay reaches SHP1020
    assert "The warehouse hasn't answered about D6 12:30" in (out / "S06.md").read_text()  # timeout, driver told
    assert "No slot fits an arrival at 18:30" in (out / "S05.md").read_text()
    assert "### Coordinator hand-off (NO_FEASIBLE_SLOT" in (out / "S05.md").read_text()  # D10's filled record
    assert sum(v["defects"] for v in m["cases"].values()) == 0
