"""Tests for research.virtual_execution — design doc §8.2 trade rules."""
import numpy as np
import pandas as pd
import pytest


def _make_bars(prices, volumes=None, start_date="2026-08-01"):
    """Create simple hourly bars from close prices.
    
    Generates 6 bars per day (09:00-14:00), matching the common
    data pattern where final_hour=14.
    """
    n = len(prices)
    dates = pd.bdate_range(start_date, periods=n // 6 + 2, freq="B")
    hours = [9, 10, 11, 12, 13, 14]
    stamps = []
    for d in dates:
        for h in hours:
            stamps.append(d.replace(hour=h))
            if len(stamps) >= n:
                break
        if len(stamps) >= n:
            break
    idx = pd.DatetimeIndex(stamps[:n], tz="Asia/Seoul")
    if volumes is None:
        volumes = [100000] * n
    df = pd.DataFrame({
        "Open": [p * 0.99 for p in prices],
        "High": [p * 1.01 for p in prices],
        "Low": [p * 0.98 for p in prices],
        "Close": prices,
        "Volume": volumes,
    }, index=idx)
    return df


class TestVirtualExecution:
    """Verify trade simulation rules from design doc §8.2."""

    def test_target_hit(self):
        """Target +10% should result in TARGET status."""
        from research.virtual_execution import simulate_trade
        # Price goes up 12% over several bars
        prices = [10000] + [10000 + i * 400 for i in range(1, 30)]
        df = _make_bars(prices)
        sessions = sorted(set(df.index.date))
        entry_ts = df.index[1]  # Enter at bar 1
        # Use final_hour=14 to match our 6-bar-per-day data
        result = simulate_trade(df, entry_ts, sessions[-1], sessions, horizon=5,
                                final_hour=14, target_pct=10.0, stop_pct=5.0)
        assert result is not None
        assert result["status"] == "TARGET"
        assert result["return_pct"] == pytest.approx(10.0, abs=0.1)

    def test_stop_loss(self):
        """Price dropping should trigger STOP."""
        from research.virtual_execution import simulate_trade
        # Start at 10000, immediately drop below stop (9500)
        prices = [10000, 10000, 9400] + [9300 + i * 10 for i in range(27)]
        df = _make_bars(prices)
        sessions = sorted(set(df.index.date))
        entry_ts = df.index[1]
        result = simulate_trade(df, entry_ts, sessions[-1], sessions, horizon=5,
                                final_hour=14, target_pct=10.0, stop_pct=5.0)
        assert result is not None
        assert result["status"] == "STOP"

    def test_same_bar_stop_and_target_stop_wins(self):
        """When both stop and target are hit in same bar, stop takes priority."""
        from research.virtual_execution import simulate_trade
        prices = [10000] * 30
        df = _make_bars(prices)
        sessions = sorted(set(df.index.date))
        # Manipulate bar to have both stop and target range
        entry_price = df.iloc[1]["Open"]
        stop_price = entry_price * 0.95
        target_price = entry_price * 1.10
        # Bar 2: low hits stop, high hits target
        df.iloc[2, df.columns.get_loc("Open")] = entry_price  # Normal open
        df.iloc[2, df.columns.get_loc("Low")] = stop_price - 100
        df.iloc[2, df.columns.get_loc("High")] = target_price + 100
        df.iloc[2, df.columns.get_loc("Close")] = entry_price
        entry_ts = df.index[1]
        result = simulate_trade(df, entry_ts, sessions[-1], sessions, horizon=5,
                                final_hour=14, target_pct=10.0, stop_pct=5.0)
        assert result is not None
        assert result["status"] == "STOP"

    def test_gap_up_at_target(self):
        """Open >= target fills at target (limit order), not at open."""
        from research.virtual_execution import simulate_trade
        prices = [10000] * 30
        df = _make_bars(prices)
        sessions = sorted(set(df.index.date))
        entry_price = df.iloc[1]["Open"]
        target_price = entry_price * 1.10
        # Gap open above target
        df.iloc[2, df.columns.get_loc("Open")] = target_price + 500
        df.iloc[2, df.columns.get_loc("High")] = target_price + 800
        df.iloc[2, df.columns.get_loc("Low")] = target_price + 200
        df.iloc[2, df.columns.get_loc("Close")] = target_price + 600
        entry_ts = df.index[1]
        result = simulate_trade(df, entry_ts, sessions[-1], sessions, horizon=5,
                                final_hour=14, target_pct=10.0, stop_pct=5.0)
        assert result is not None
        assert result["status"] == "TARGET"
        assert result["exit_price"] == pytest.approx(target_price, rel=1e-6)

    def test_gap_down_below_stop(self):
        """Gap below stop fills at open price, loss can exceed -5%."""
        from research.virtual_execution import simulate_trade
        prices = [10000] * 30
        df = _make_bars(prices)
        sessions = sorted(set(df.index.date))
        entry_price = df.iloc[1]["Open"]
        stop_price = entry_price * 0.95
        # Gap open well below stop
        gap_open = stop_price - 500
        df.iloc[2, df.columns.get_loc("Open")] = gap_open
        df.iloc[2, df.columns.get_loc("Low")] = gap_open - 100
        df.iloc[2, df.columns.get_loc("High")] = gap_open + 100
        df.iloc[2, df.columns.get_loc("Close")] = gap_open + 50
        entry_ts = df.index[1]
        result = simulate_trade(df, entry_ts, sessions[-1], sessions, horizon=5,
                                final_hour=14, target_pct=10.0, stop_pct=5.0)
        assert result is not None
        assert result["status"] == "STOP"
        assert result["exit_price"] == pytest.approx(gap_open, rel=1e-6)
        assert result["return_pct"] < -5.0  # Loss exceeds stop percentage

    def test_zero_volume_no_fill(self):
        """Volume 0 at entry bar means no execution."""
        from research.virtual_execution import simulate_trade
        prices = [10000] * 30
        volumes = [100000] * 30
        volumes[1] = 0  # No volume at entry
        df = _make_bars(prices, volumes)
        sessions = sorted(set(df.index.date))
        entry_ts = df.index[1]
        result = simulate_trade(df, entry_ts, sessions[-1], sessions, horizon=5,
                                final_hour=14, target_pct=10.0, stop_pct=5.0)
        # Implementation returns {'status': 'NO_FILL'} for zero volume
        assert result is not None
        assert result["status"] == "NO_FILL"

    def test_mark_to_market_no_exit(self):
        """Flat price within horizon results in OPEN_MARK_TO_MARKET."""
        from research.virtual_execution import simulate_trade
        # Prices stay flat - neither target nor stop hit
        prices = [10000] * 30
        df = _make_bars(prices)
        sessions = sorted(set(df.index.date))
        entry_ts = df.index[1]
        result = simulate_trade(df, entry_ts, sessions[-1], sessions, horizon=5,
                                final_hour=14, target_pct=10.0, stop_pct=5.0)
        assert result is not None
        assert result["status"] == "OPEN_MARK_TO_MARKET"
        assert abs(result["return_pct"]) < 2.0  # Roughly flat
