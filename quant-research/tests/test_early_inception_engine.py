"""
Tests for Early Inception Factor Model & Trend Weakening Exit Engine.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.early_inception_engine import (
    compute_early_inception_rankings,
    simulate_trade_with_weakening_exit,
)


def make_synthetic_stock(
    n_bars: int = 150,
    base_price: float = 10000.0,
    trend: str = "consolidation_breakout",
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n_bars, freq="B")

    prices = [base_price]
    for i in range(1, n_bars):
        if trend == "overextended":
            # Surges up strongly for 60 bars (+75%)
            step = prices[-1] * (0.01 + rng.normal(0, 0.005))
        elif trend == "consolidation_breakout":
            # Earlier bars wider swings (0.015), consolidation phase (-25 to -5) tight (0.003), breakout last 5 (+0.025)
            if i < n_bars - 25:
                step = prices[-1] * rng.normal(0, 0.015)
            elif i < n_bars - 5:
                step = prices[-1] * rng.normal(0, 0.003)
            else:
                step = prices[-1] * (0.025 + rng.normal(0, 0.002))
        elif trend == "downtrend":
            step = prices[-1] * (-0.005 + rng.normal(0, 0.005))
        else:
            step = prices[-1] * rng.normal(0, 0.01)
        prices.append(max(100.0, prices[-1] + step))

    prices = np.array(prices)
    spread = rng.uniform(0.005, 0.02, size=n_bars)
    if trend == "consolidation_breakout":
        spread[-25:-5] = rng.uniform(0.002, 0.005, size=20)
    high = prices * (1.0 + spread)
    low = prices * (1.0 - spread)
    open_p = (prices + low) / 2.0

    # Volume: surge on breakout, normal otherwise
    vol = rng.uniform(50000, 100000, size=n_bars)
    if trend == "consolidation_breakout":
        vol[-5:] *= 3.0  # Volume ignition

    df = pd.DataFrame(
        {
            "Open": open_p,
            "High": high,
            "Low": low,
            "Close": prices,
            "Volume": vol,
        },
        index=dates,
    )
    return df


def test_early_inception_excludes_overextended_stocks():
    # Overextended stock (+70% over 60 days)
    overext = make_synthetic_stock(150, 10000.0, trend="overextended", seed=10)
    # Consolidation breakout stock (early inception)
    early = make_synthetic_stock(150, 10000.0, trend="consolidation_breakout", seed=20)

    universe = {
        "OVEREXT": overext,
        "EARLY": early,
    }

    eval_date = overext.index[-1]
    rankings = compute_early_inception_rankings(universe, eval_date)

    assert not rankings.empty
    # The overextended stock should be filtered out!
    codes = rankings["code"].tolist()
    assert "OVEREXT" not in codes
    assert "EARLY" in codes


def test_early_inception_zero_lookahead():
    early = make_synthetic_stock(150, 10000.0, trend="consolidation_breakout", seed=30)
    universe = {"STOCK1": early}
    eval_date = early.index[130]

    rank1 = compute_early_inception_rankings(universe, eval_date)

    # Corrupt future bars after eval_date
    corrupted_early = early.copy()
    corrupted_early.iloc[131:, :] *= 10.0
    universe_corrupted = {"STOCK1": corrupted_early}

    rank2 = compute_early_inception_rankings(universe_corrupted, eval_date)

    assert len(rank1) == len(rank2)
    if not rank1.empty:
        pd.testing.assert_frame_equal(rank1, rank2)


def test_trend_weakening_exit_sma():
    # Create a stock that enters, moves up 2 bars, then drops sharply below SMA10
    dates = pd.date_range("2024-01-01", periods=30, freq="B")
    prices = [10000.0] * 30
    for i in range(1, 15):
        prices[i] = 10000.0 + i * 200.0  # Runs up
    for i in range(15, 30):
        prices[i] = prices[14] - (i - 14) * 500.0  # Plunges below SMA10

    prices = np.array(prices)
    df = pd.DataFrame(
        {
            "Open": prices,
            "High": prices * 1.01,
            "Low": prices * 0.99,
            "Close": prices,
            "Volume": [100000.0] * 30,
        },
        index=dates,
    )

    entry_signal_date = dates[10]
    res = simulate_trade_with_weakening_exit(
        df,
        entry_signal_date,
        max_horizon=20,
        peak_trailing_pct=0.20,  # Isolate SMA weakening test
        use_sma_exit=True,
        sma_exit_window=10,
    )

    assert res["status"] == "COMPLETED"
    assert "SMA" in res["exit_reason"]
    assert res["holding_days"] < 20


def test_trend_weakening_exit_trailing_stop():
    # Stock runs up +20%, then drops 7% from peak
    dates = pd.date_range("2024-01-01", periods=20, freq="B")
    prices = [10000.0]
    # Day 1-5 runs up to 12000 (+20%)
    for i in range(1, 6):
        prices.append(10000.0 + i * 400.0)
    # Day 6 pulls back by 8% (to 11040)
    prices.append(12000.0 * 0.92)
    # Day 7-19 flat
    for _ in range(7, 20):
        prices.append(11000.0)

    prices = np.array(prices)
    df = pd.DataFrame(
        {
            "Open": prices,
            "High": prices * 1.01,
            "Low": prices * 0.99,
            "Close": prices,
            "Volume": [100000.0] * 20,
        },
        index=dates,
    )

    entry_signal_date = dates[0]
    res = simulate_trade_with_weakening_exit(
        df,
        entry_signal_date,
        max_horizon=20,
        peak_trailing_pct=0.06,
        use_sma_exit=False,  # Isolate trailing stop test
    )

    assert res["status"] == "COMPLETED"
    assert res["exit_reason"] == "PEAK_TRAILING_STOP"
    assert res["win"] is True  # Locked in positive gain!


def test_trend_weakening_exit_protective_stop():
    # False breakout: immediately falls -6% below entry
    dates = pd.date_range("2024-01-01", periods=10, freq="B")
    prices = [10000.0, 10000.0, 9300.0, 9200.0, 9100.0, 9000.0, 9000.0, 9000.0, 9000.0, 9000.0]
    df = pd.DataFrame(
        {
            "Open": prices,
            "High": prices,
            "Low": prices,
            "Close": prices,
            "Volume": [100000.0] * 10,
        },
        index=dates,
    )

    entry_signal_date = dates[0]
    res = simulate_trade_with_weakening_exit(
        df,
        entry_signal_date,
        max_horizon=20,
        stop_loss_pct=0.05,
    )

    assert res["status"] == "COMPLETED"
    assert res["exit_reason"] == "STOP_LOSS"
    assert res["holding_days"] <= 3


def test_comparative_backtest_runner():
    from scripts.run_early_inception_comparison import run_comparative_backtest, scan_latest_early_inception

    s1 = make_synthetic_stock(150, 10000.0, trend="consolidation_breakout", seed=50)
    s2 = make_synthetic_stock(150, 10000.0, trend="overextended", seed=60)
    universe = {"S1": s1, "S2": s2}

    eval_dates = [s1.index[130]]
    res = run_comparative_backtest(universe, eval_dates, top_n=5)

    assert "mode2_baseline" in res
    assert "early_inception" in res
    assert isinstance(res["mode2_baseline"]["trades_count"], int)
    assert isinstance(res["early_inception"]["trades_count"], int)

    latest_picks = scan_latest_early_inception(universe, s1.index[-1], top_n=5)
    assert isinstance(latest_picks, pd.DataFrame)


def test_compute_early_inception_3d_hybrid_gate():
    from research.early_inception_engine import compute_early_inception_3d_rankings

    dates = pd.date_range("2024-01-01", periods=130, freq="B")
    eval_date = dates[-1]

    # Helper to generate stock with specific metrics
    def make_stock(close_series, high_series, low_series, open_series, vol_series):
        return pd.DataFrame({
            "Open": open_series,
            "High": high_series,
            "Low": low_series,
            "Close": close_series,
            "Volume": vol_series,
        }, index=dates)

    base = np.full(130, 10000.0)
    # 20d breakout setup: 20d high prior was 10500, 60d high was 12000, 60d low was 9000
    # Range 60 = 3000

    # Stock 1: Quiet Compression (삼천당제약/고영 모델)
    # Range 20 = 1000 (cont = 1000/3000 = 0.33 <= 0.45), vol_spike = 2.0x, upper_wick = 10%
    c1 = base.copy()
    c1[-1] = 11000.0  # Breaks out above 10500
    h1 = c1.copy()
    h1[-1] = 11100.0  # Upper wick = (11100 - 11000) / (11100 - 10000) = 100 / 1100 = 9%
    l1 = c1.copy()
    l1[-1] = 10000.0
    o1 = c1.copy()
    o1[-1] = 10200.0
    v1 = np.full(130, 100000.0)
    v1[-1] = 200000.0  # 2.0x vol
    # Set 60d base first, then tighten 20d window
    h1[-61:-1] = 12000.0
    l1[-61:-1] = 9000.0
    h1[-21:-1] = 10500.0
    l1[-21:-1] = 9500.0
    stock_quiet = make_stock(c1, h1, l1, o1, v1)

    # Stock 2: Leader Volume Ignition (카페24 모델)
    # Range 20 = 1650 (cont = 1650/3000 = 0.55 > 0.45 but <= 0.70), vol_spike = 15.0x, upper_wick = 15%
    c2 = base.copy()
    c2[-1] = 11200.0
    h2 = c2.copy()
    h2[-1] = 11400.0  # Upper wick = 200 / 1500 = 13.3%
    l2 = c2.copy()
    l2[-1] = 9900.0
    o2 = c2.copy()
    o2[-1] = 10100.0
    v2 = np.full(130, 100000.0)
    v2[-1] = 1500000.0  # 15.0x vol ignition!
    h2[-61:-1] = 12000.0
    l2[-61:-1] = 9000.0
    h2[-21:-1] = 10800.0
    l2[-21:-1] = 9150.0  # Range 20 = 1650 (0.55)
    stock_leader = make_stock(c2, h2, l2, o2, v2)

    # Stock 3: High contraction without volume (Should be excluded!)
    # Cont = 0.55, but vol_spike = 1.5x (< 5.0x)
    stock_failed = stock_leader.copy()
    stock_failed_vol = np.full(130, 100000.0)
    stock_failed_vol[-1] = 150000.0
    stock_failed["Volume"] = stock_failed_vol

    universe = {
        "QUIET": stock_quiet,
        "LEADER": stock_leader,
        "FAILED": stock_failed,
    }

    ranks = compute_early_inception_3d_rankings(universe, eval_date)
    assert not ranks.empty
    codes = ranks["code"].tolist()
    assert "QUIET" in codes
    assert "LEADER" in codes
    assert "FAILED" not in codes

    quiet_row = ranks[ranks["code"] == "QUIET"].iloc[0]
    leader_row = ranks[ranks["code"] == "LEADER"].iloc[0]
    assert quiet_row["setup_type"] == "압축돌파"
    assert leader_row["setup_type"] == "거래량폭발"

