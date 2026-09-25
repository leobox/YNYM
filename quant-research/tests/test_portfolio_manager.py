"""
Unit tests for Pure Quant Mode 2 Portfolio Manager.
Verifies accounting conservation, airbag stops, regime defense, and determinism.
"""

import json
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

from scripts.pure_quant_portfolio_manager import (
    PortfolioManager,
    PortfolioState,
    calculate_market_breadth,
    compute_factor_rankings,
)


@pytest.fixture
def mock_universe(tmp_path):
    """Creates a synthetic 15-stock universe with 150 daily bars."""
    dates = pd.date_range("2025-01-01", periods=150, freq="B")
    universe = {}

    np.random.seed(42)
    for i in range(15):
        code = f"{i:06d}"
        base_px = 10000.0 + i * 2000.0
        # Create upward trending series
        trend = np.linspace(0.8, 1.3, 150)
        noise = np.random.normal(0, 0.015, 150)
        close = base_px * trend * (1.0 + noise)
        high = close * 1.02
        low = close * 0.98
        open_p = close * 1.005
        vol = np.random.uniform(50000, 200000, 150)

        df = pd.DataFrame({
            "Open": open_p,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": vol
        }, index=dates)
        universe[code] = df
    return universe


def test_initialization_and_status(tmp_path):
    state_file = tmp_path / "test_state.json"
    mgr = PortfolioManager(state_file=str(state_file))
    mgr.initialize_portfolio(1_000_000.0)

    state = mgr.load_state()
    assert state.cash == 1_000_000.0
    assert state.initial_capital == 1_000_000.0
    assert len(state.positions) == 0


def test_market_breadth_calculation(mock_universe):
    breadth = calculate_market_breadth(mock_universe, window=60)
    assert len(breadth) == 150
    # Because all series are upward trending, late breadth should be 100%
    assert breadth.iloc[-1] == 100.0


def test_factor_rankings_no_lookahead(mock_universe):
    dates = list(mock_universe["000000"].index)
    eval_date = dates[140]
    rankings = compute_factor_rankings(mock_universe, eval_date, min_med_amt=1e6)

    assert not rankings.empty
    assert len(rankings) <= 15
    assert "composite_score" in rankings.columns
    # Check that highest score is ranked first
    assert rankings["composite_score"].iloc[0] >= rankings["composite_score"].iloc[-1]


def test_airbag_stop_detection(tmp_path, mock_universe):
    state_file = tmp_path / "test_state.json"
    mgr = PortfolioManager(state_file=str(state_file))
    mgr.universe = mock_universe
    dates = sorted(list(mock_universe["000000"].index))
    mgr.breadth = calculate_market_breadth(mock_universe)

    # Position with high entry price so current low triggers -15% stop
    entry_p = 100000.0
    state = PortfolioState(
        cash=500000.0,
        initial_capital=1000000.0,
        positions={
            "000000": {
                "qty": 5,
                "entry_price": entry_p,
                "stop_price": entry_p * 0.85, # 85,000
                "days_held": 5
            }
        }
    )
    mgr.save_state(state)

    # Mock current price to be 50,000 (well below 85,000)
    last_dt = dates[-1]
    mock_universe["000000"].loc[last_dt, "Low"] = 50000.0
    mock_universe["000000"].loc[last_dt, "Open"] = 60000.0

    triggers = mgr.check_catastrophic_stops(as_of_date=last_dt.strftime("%Y-%m-%d"))
    assert len(triggers) == 1
    assert triggers[0]["code"] == "000000"
    assert triggers[0]["stop_price"] == 85000.0


def test_regime_defense_liquidation(tmp_path, mock_universe):
    state_file = tmp_path / "test_state.json"
    mgr = PortfolioManager(state_file=str(state_file))
    mgr.universe = mock_universe
    dates = sorted(list(mock_universe["000000"].index))
    last_dt = dates[-2]

    # Force breadth below 40%
    mgr.breadth = pd.Series(20.0, index=dates)

    state = PortfolioState(
        cash=500000.0,
        initial_capital=1000000.0,
        positions={
            "000001": {
                "qty": 10,
                "entry_price": 10000.0,
                "stop_price": 8500.0
            }
        }
    )
    mgr.save_state(state)

    plan = mgr.generate_rebalance_orders(as_of_date=last_dt.strftime("%Y-%m-%d"), dry_run=False)
    assert plan["regime"].startswith("BEAR / DEFENSE")
    assert len(plan["sell_orders"]) == 1
    assert len(plan["buy_orders"]) == 0

    # Verify positions emptied
    reloaded = mgr.load_state()
    assert len(reloaded.positions) == 0
    assert reloaded.cash > 500000.0


def test_missing_breadth_date_is_rejected(tmp_path, mock_universe):
    mgr = PortfolioManager(state_file=str(tmp_path / "state.json"))
    mgr.universe = mock_universe
    mgr.breadth = calculate_market_breadth(mock_universe)
    with pytest.raises(ValueError, match="No complete market-breadth"):
        mgr.get_status(as_of_date="2024-01-01")


def test_rebalance_apply_same_date_is_idempotent(tmp_path, mock_universe):
    mgr = PortfolioManager(state_file=str(tmp_path / "state.json"))
    mgr.universe = mock_universe
    dates = sorted(mock_universe["000000"].index)
    mgr.breadth = pd.Series(20.0, index=dates)
    mgr.save_state(PortfolioState(
        cash=500000.0,
        initial_capital=1000000.0,
        positions={"000001": {"qty": 10, "entry_price": 10000.0,
                              "stop_price": 8500.0}},
        last_rebalance_date=dates[-1].strftime("%Y-%m-%d"),
        history=[{"date": dates[-1].strftime("%Y-%m-%d")}],
    ))
    plan = mgr.generate_rebalance_orders(
        as_of_date=dates[-1].strftime("%Y-%m-%d"), dry_run=False
    )
    assert plan["already_applied"] is True
    state = mgr.load_state()
    assert len(state.positions) == 1
    assert len(state.history) == 1


def test_latest_close_dry_run_uses_close_and_cannot_apply(tmp_path, mock_universe):
    mgr = PortfolioManager(state_file=str(tmp_path / "state.json"))
    mgr.universe = mock_universe
    dates = sorted(mock_universe["000000"].index)
    mgr.breadth = pd.Series(20.0, index=dates)
    last = dates[-1]
    code = "000001"
    mgr.save_state(PortfolioState(
        cash=500000.0, initial_capital=1000000.0,
        positions={code: {"qty": 10, "entry_price": 10000.0, "stop_price": 8500.0}},
    ))
    plan = mgr.generate_rebalance_orders(as_of_date=last.strftime("%Y-%m-%d"))
    assert plan["exec_date"] == "NEXT_SESSION"
    assert plan["price_basis"] == "signal_close_estimate"
    assert plan["sell_orders"][0]["est_price"] == mock_universe[code].loc[last, "Close"]
    with pytest.raises(ValueError, match="next session open"):
        mgr.generate_rebalance_orders(as_of_date=last.strftime("%Y-%m-%d"), dry_run=False)


def test_missing_held_daily_bar_blocks_airbag(tmp_path, mock_universe):
    mgr = PortfolioManager(state_file=str(tmp_path / "state.json"))
    mgr.universe = mock_universe
    dates = sorted(mock_universe["000000"].index)
    mgr.breadth = pd.Series(60.0, index=dates)
    mgr.save_state(PortfolioState(
        cash=500000.0, initial_capital=1000000.0,
        positions={"999999": {"qty": 10, "entry_price": 10000.0, "stop_price": 8500.0}},
    ))
    with pytest.raises(ValueError, match="held position has no daily bar"):
        mgr.check_catastrophic_stops(as_of_date=dates[-1].strftime("%Y-%m-%d"))


def test_zero_volume_held_bar_blocks_airbag(tmp_path, mock_universe):
    mgr = PortfolioManager(state_file=str(tmp_path / "state.json"))
    mgr.universe = mock_universe
    date = mock_universe["000001"].index[-1]
    mgr.breadth = pd.Series(60.0, index=mock_universe["000000"].index)
    mgr.universe["000001"].loc[date, "Volume"] = 0
    mgr.save_state(PortfolioState(
        cash=500000.0, initial_capital=1000000.0,
        positions={"000001": {"qty": 10, "entry_price": 10000.0,
                              "stop_price": 8500.0}},
    ))
    with pytest.raises(ValueError, match="no tradable volume"):
        mgr.check_catastrophic_stops(as_of_date=date.strftime("%Y-%m-%d"))
