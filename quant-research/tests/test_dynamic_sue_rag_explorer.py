"""The read-only explorer must never leak a later EPS snapshot into a signal."""

import json

import numpy as np
import pandas as pd

from research.dynamic_sue_rag_explorer import earnings_context, explore, render_markdown
from research.sue_engine import SueEngine
from scripts.pure_quant_portfolio_manager import PortfolioManager, PortfolioState


def _raw(path, fetched_at):
    path.write_text(json.dumps({"000001": {
        "fetched_at": fetched_at,
        "quarters": [
            {"period": "202503", "eps": 100, "is_consensus": False},
            {"period": "202603", "eps": 140, "is_consensus": False},
        ],
        "disclosures": [{"datetime": "2026-05-15T16:00:00",
                         "title": "분기보고서", "disclosure_id": "x"}],
    }}, ensure_ascii=False), encoding="utf-8")


def test_later_snapshot_is_not_used_for_historical_signal(tmp_path):
    source = tmp_path / "eps.json"
    _raw(source, "2026-07-30T12:00:00")
    engine = SueEngine(source)
    calendar = pd.bdate_range("2026-05-01", "2026-07-31")
    historical = earnings_context(engine, "000001", pd.Timestamp("2026-07-29").date(), calendar)
    assert historical["reason"] == "SNAPSHOT_AFTER_SIGNAL"
    assert "actual_eps" not in historical
    assert historical["score_delta"] == 0
    later = earnings_context(engine, "000001", pd.Timestamp("2026-07-31").date(), calendar)
    assert later["status"] == "YOY_EPS_PROXY_UNVERIFIED"
    assert later["delta_eps"] == 40
    assert later["standardized_sue"] is None
    assert later["score_eligible"] is False


def test_explorer_preserves_plan_and_state(tmp_path):
    dates = pd.bdate_range("2026-01-01", periods=150)
    close = np.linspace(10000, 16000, len(dates))
    frame = pd.DataFrame({"Open": close, "High": close * 1.01,
                          "Low": close * .99, "Close": close,
                          "Volume": np.full(len(dates), 100000)}, index=dates)
    mgr = PortfolioManager(state_file=str(tmp_path / "state.json"))
    mgr.universe = {"000001": frame}
    mgr.breadth = pd.Series(80.0, index=dates)
    mgr.save_state(PortfolioState(cash=1_000_000, initial_capital=1_000_000))
    before = mgr.state_file.read_bytes()
    signal = str(dates[-1].date())
    base = mgr.generate_daily_plan(signal)
    source = tmp_path / "eps.json"
    _raw(source, str(dates[-1].date()) + "T12:00:00")
    result = explore(mgr, signal, eps_path=source)
    assert result["actions"] == base["actions"]
    assert result["candidates"][0]["earnings"]["reason"] == "SNAPSHOT_AFTER_SIGNAL"
    assert "SNAPSHOT_AFTER_SIGNAL" in render_markdown(result)
    assert mgr.state_file.read_bytes() == before
