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


def test_early_contraction_rankings_and_reference_levels():
    from research.early_inception_engine import compute_early_contraction_rankings

    days = pd.bdate_range("2026-03-01", periods=140)
    eval_date = days[-5]
    idx = list(days).index(eval_date)

    # Stock 1: Confirmed 20d breakout (Close >= prior_h20)
    c1 = np.full(len(days), 10000.0)
    h1 = np.full(len(days), 10200.0)
    l1 = np.full(len(days), 9900.0)
    v1 = np.full(len(days), 100000.0)
    h1[idx - 55 : idx - 25] = 11000.0
    l1[idx - 55 : idx - 25] = 9000.0
    c1[idx - 20 : idx] = 10050.0
    h1[idx - 20 : idx] = 10150.0
    l1[idx - 20 : idx] = 9950.0
    c1[idx] = 10200.0
    h1[idx] = 10220.0
    l1[idx] = 10050.0
    v1[idx] = 250000.0

    # Stock 2: Tight 20d contraction within -3% of 20d high (Close = 10080 > SMA20, prior_h20 = 10150 -> -0.69%)
    c2 = c1.copy()
    h2 = h1.copy()
    l2 = l1.copy()
    v2 = v1.copy()
    c2[idx] = 10080.0
    h2[idx] = 10100.0
    l2[idx] = 9950.0
    # Future corruption after eval_date must not affect rankings
    c2[idx + 1 :] = 50000.0

    def to_df(c, h, l, v):
        return pd.DataFrame({"Open": c * 0.995, "High": h, "Low": l, "Close": c, "Volume": v}, index=days)

    universe = {"BRK": to_df(c1, h1, l1, v1), "CNTR": to_df(c2, h2, l2, v2)}

    brk_ranks = compute_early_inception_rankings(universe, eval_date)
    cntr_ranks = compute_early_contraction_rankings(universe, eval_date)

    assert brk_ranks["code"].tolist() == ["BRK"]
    assert cntr_ranks["code"].tolist() == ["CNTR"]

    brk_row = brk_ranks.iloc[0]
    assert brk_row["prior_h20"] == pytest.approx(10150.0)
    assert brk_row["breakout_ext_pct"] == pytest.approx((10200.0 / 10150.0 - 1.0) * 100.0)
    assert brk_row["stop_price_5pct"] == pytest.approx(round(10200.0 * 0.95, 2))

    cntr_row = cntr_ranks.iloc[0]
    assert cntr_row["prior_h20"] == pytest.approx(10150.0)
    assert -3.0 <= cntr_row["breakout_ext_pct"] < 0.0
    assert cntr_row["stop_price_5pct"] == pytest.approx(round(10080.0 * 0.95, 2))
    assert 0.0 < cntr_row["contraction_score"] <= 1.0

