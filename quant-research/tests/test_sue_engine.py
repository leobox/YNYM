"""
Unit tests for SueEngine (Zero-auth point-in-time time-series SUE).
"""

import pandas as pd
import pytest
import json
from pathlib import Path
from research.sue_engine import SueEngine, SueEvent


@pytest.fixture
def engine(tmp_path):
    raw = tmp_path / "eps.json"
    raw.write_text(json.dumps({"001040": {
        "quarters": [
            {"period": "202506", "eps": 500, "is_consensus": False},
            {"period": "202606", "eps": 1000, "is_consensus": False},
        ],
        "disclosures": [{"datetime": "2026-07-15T16:00:00", "title": "반기보고서"}],
    }}), encoding="utf-8")
    return SueEngine(raw)


def test_prior_year_period():
    assert SueEngine._prior_year_period("202606") == "202506"
    assert SueEngine._prior_year_period("202512") == "202412"
    assert SueEngine._prior_year_period("202403") == "202303"
    assert SueEngine._prior_year_period("invalid") is None


def test_engine_load_and_events(engine):
    assert len(engine.raw_data) == 1
    assert len(engine.events_by_code) == 1

    # Check CJ 001040
    events_cj = engine.get_events_for_stock("001040")
    assert len(events_cj) > 0, "CJ 001040 should have SUE events"
    ev = events_cj[0]
    assert isinstance(ev, SueEvent)
    assert ev.code == "001040"
    assert ev.actual_eps != 0.0


def test_point_in_time_filter(engine):
    """Ensure future announcements cannot leak into past evaluation dates."""

    # 2024-01-01 is prior to any 2025/2026 disclosures in our dataset
    past_date = pd.Timestamp("2024-01-01")
    res_past = engine.get_active_event_on("001040", past_date)
    assert res_past["status"] == "UNOBSERVED"
    assert res_past["event"] is None

    # Recent date should observe active event
    recent_date = pd.Timestamp("2026-08-30")
    res_recent = engine.get_active_event_on("001040", recent_date)
    assert res_recent["status"] in ("ACTIVE", "EXPIRED")
    assert res_recent["event"] is not None
    assert res_recent["event"].disclosure_date <= "2026-08-30"


def test_cohort_and_elapsed_bars(engine):

    # Create synthetic events
    engine.events_by_code["TEST_CODE"] = [
        SueEvent(
            code="TEST_CODE",
            period="202606",
            prior_period="202506",
            actual_eps=1000.0,
            prior_eps=500.0,
            delta_eps=500.0,
            pct_change=100.0,
            is_turnaround=False,
            disclosure_date="2026-07-15",
            signal_date="2026-07-16",
            entry_date="2026-07-17",
            is_surprise=True
        )
    ]

    calendar = pd.date_range("2026-07-01", "2026-10-30", freq="B")  # Business days

    # 5 business days after disclosure (2026-07-22) -> cohort 0_20d
    res_5d = engine.get_active_event_on("TEST_CODE", pd.Timestamp("2026-07-22"), calendar=calendar)
    assert res_5d["status"] == "ACTIVE"
    assert res_5d["cohort"] == "0_20d"
    assert 0 <= res_5d["days_elapsed"] <= 20

    # 30 business days after disclosure -> cohort 21_40d
    res_30d = engine.get_active_event_on("TEST_CODE", pd.Timestamp("2026-08-26"), calendar=calendar)
    assert res_30d["status"] == "ACTIVE"
    assert res_30d["cohort"] == "21_40d"

    # 50 business days after disclosure -> cohort 41_60d
    res_50d = engine.get_active_event_on("TEST_CODE", pd.Timestamp("2026-09-23"), calendar=calendar)
    assert res_50d["status"] == "ACTIVE"
    assert res_50d["cohort"] == "41_60d"

    # 70 business days after disclosure -> EXPIRED
    res_70d = engine.get_active_event_on("TEST_CODE", pd.Timestamp("2026-10-23"), calendar=calendar)
    assert res_70d["status"] == "EXPIRED"
    assert res_70d["cohort"] == "expired"


def test_evaluate_universe(engine):
    codes = ["001040", "001210", "001450"]
    eval_date = pd.Timestamp("2026-08-15")
    df = engine.evaluate_universe_on(codes, eval_date)
    assert len(df) == 3
    assert set(df.columns) >= {"code", "status", "cohort", "days_elapsed", "is_surprise", "pct_change"}
