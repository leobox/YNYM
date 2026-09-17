"""Shared test fixtures for quant-research.

All fixtures use synthetic data — no network calls required.
Research execution files (e.g. test_stable_pullback.py) are not auto-run as unit tests.
"""
import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def kst_index():
    """Generate Asia/Seoul hourly timestamps for 30 trading days (09:00-14:00)."""
    dates = pd.bdate_range("2026-07-01", periods=30, freq="B")
    hours = [9, 10, 11, 12, 13, 14]
    stamps = []
    for d in dates:
        for h in hours:
            stamps.append(d.replace(hour=h, minute=0, second=0))
    return pd.DatetimeIndex(stamps, tz="Asia/Seoul")


@pytest.fixture
def sample_ohlcv(kst_index):
    """180 bars of synthetic uptrend OHLCV data."""
    n = len(kst_index)
    np.random.seed(42)
    base = 10000 + np.cumsum(np.random.randn(n) * 50 + 5)
    df = pd.DataFrame(
        {
            "Open": base - np.abs(np.random.randn(n) * 30),
            "High": base + np.abs(np.random.randn(n) * 80),
            "Low": base - np.abs(np.random.randn(n) * 60),
            "Close": base + np.random.randn(n) * 20,
            "Volume": np.random.randint(50000, 500000, size=n).astype(float),
        },
        index=kst_index,
    )
    # Enforce OHLCV consistency
    df["High"] = df[["Open", "High", "Close"]].max(axis=1)
    df["Low"] = df[["Open", "Low", "Close"]].min(axis=1)
    return df


@pytest.fixture
def sessions_list(kst_index):
    """Sorted unique trading dates from the kst_index."""
    return sorted(set(kst_index.date))


@pytest.fixture
def sample_universe():
    """Minimal universe records for testing."""
    return [
        {"code": "005930", "name": "삼성전자", "market": "KOSPI", "price": 72000,
         "market_cap": 4_000_000_000_000, "amount": 500_000_000_000, "quote_time": None},
        {"code": "000660", "name": "SK하이닉스", "market": "KOSPI", "price": 150000,
         "market_cap": 1_200_000_000_000, "amount": 300_000_000_000, "quote_time": None},
        {"code": "035420", "name": "NAVER", "market": "KOSPI", "price": 210000,
         "market_cap": 350_000_000_000, "amount": 80_000_000_000, "quote_time": None},
    ]


@pytest.fixture
def breakout_bars():
    """Synthetic bars designed to trigger a breakout + hold confirmation.

    Bar -2 (trigger): strong bullish candle, volume spike, above MAs
    Bar -1 (confirmation): holds above breakout level, close >= trigger close
    """
    np.random.seed(123)
    n = 150  # Enough for MA26 + rolling warmup
    dates = pd.bdate_range("2026-06-01", periods=n // 6 + 1, freq="B")
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

    # Gradual uptrend for MA convergence
    base = 5000 + np.cumsum(np.random.randn(n) * 15 + 3)
    volume = np.random.randint(30000, 100000, size=n).astype(float)

    # Make second-to-last bar a strong breakout
    base[-2] = base[-3] + 200  # Big jump
    volume[-2] = 2_000_000  # Volume spike (will produce est. turnover > 1bn at ~5000 KRW)

    # Confirmation bar holds above
    base[-1] = base[-2] + 50
    volume[-1] = 150_000

    df = pd.DataFrame(
        {
            "Open": base - 20,
            "High": base + np.abs(np.random.randn(n) * 40) + 10,
            "Low": base - np.abs(np.random.randn(n) * 30),
            "Close": base,
            "Volume": volume,
        },
        index=idx,
    )
    df["High"] = df[["Open", "High", "Close"]].max(axis=1)
    df["Low"] = df[["Open", "Low", "Close"]].min(axis=1)
    return df
