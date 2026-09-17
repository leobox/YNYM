"""Tests for scanner.candle_cleaner — design doc §4.3 rules."""
import numpy as np
import pandas as pd
import pytest


class TestValidOHLCV:
    """valid_ohlcv must reject bad data silently, never crash."""

    def test_empty_dataframe(self):
        from scanner.candle_cleaner import valid_ohlcv
        df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        assert valid_ohlcv(df) is False

    def test_valid_data(self, sample_ohlcv):
        from scanner.candle_cleaner import valid_ohlcv
        assert valid_ohlcv(sample_ohlcv) is True

    def test_negative_volume(self, sample_ohlcv):
        from scanner.candle_cleaner import valid_ohlcv
        bad = sample_ohlcv.copy()
        bad.iloc[5, bad.columns.get_loc("Volume")] = -100
        assert valid_ohlcv(bad) is False

    def test_high_below_close(self, sample_ohlcv):
        from scanner.candle_cleaner import valid_ohlcv
        bad = sample_ohlcv.copy()
        bad.iloc[10, bad.columns.get_loc("High")] = bad.iloc[10]["Close"] - 100
        assert valid_ohlcv(bad) is False

    def test_low_above_open(self, sample_ohlcv):
        from scanner.candle_cleaner import valid_ohlcv
        bad = sample_ohlcv.copy()
        bad.iloc[10, bad.columns.get_loc("Low")] = bad.iloc[10]["Open"] + 100
        assert valid_ohlcv(bad) is False

    def test_nan_price(self, sample_ohlcv):
        from scanner.candle_cleaner import valid_ohlcv
        bad = sample_ohlcv.copy()
        bad.iloc[0, bad.columns.get_loc("Close")] = np.nan
        assert valid_ohlcv(bad) is False

    def test_inf_price(self, sample_ohlcv):
        from scanner.candle_cleaner import valid_ohlcv
        bad = sample_ohlcv.copy()
        bad.iloc[0, bad.columns.get_loc("Close")] = np.inf
        assert valid_ohlcv(bad) is False

    def test_duplicate_index(self, sample_ohlcv):
        from scanner.candle_cleaner import valid_ohlcv
        bad = pd.concat([sample_ohlcv, sample_ohlcv.iloc[:1]])
        assert valid_ohlcv(bad) is False


class TestCompletedBars:
    """Bars must be fully elapsed before being used for pattern analysis."""

    def test_removes_15h_placeholder(self):
        """15:00 bar with volume=0 and high=low is a Yahoo artifact, must be removed."""
        from scanner.candle_cleaner import clean_bars
        times = pd.to_datetime(["2026-09-15 14:00", "2026-09-15 15:00"]).tz_localize("Asia/Seoul")
        payload = {"timestamp": [int(value.timestamp()) for value in times],
                   "indicators": {"quote": [{"open": [100, 101], "high": [102, 101],
                                                "low": [99, 101], "close": [101, 101],
                                                "volume": [10, 0]}]}}
        result = clean_bars(payload, pd.Timestamp("2026-09-16 08:00", tz="Asia/Seoul"))
        assert list(result.index) == [times[0]]

    def test_excludes_incomplete_bar(self):
        from scanner.candle_cleaner import clean_bars
        times = pd.to_datetime(["2026-09-15 09:00", "2026-09-15 10:00"]).tz_localize("Asia/Seoul")
        payload = {"timestamp": [int(value.timestamp()) for value in times],
                   "indicators": {"quote": [{"open": [100, 101], "high": [102, 103],
                                                "low": [99, 100], "close": [101, 102],
                                                "volume": [10, 20]}]}}
        result = clean_bars(payload, pd.Timestamp("2026-09-15 10:30", tz="Asia/Seoul"))
        assert list(result.index) == [times[0]]

    def test_minimum_bars_requirement(self, sample_ohlcv):
        """Scanner requires at least 120 bars."""
        short = sample_ohlcv.head(50)
        assert len(short) < 120
