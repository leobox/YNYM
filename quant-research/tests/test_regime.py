import datetime

import numpy as np
import pandas as pd

from research.regime import compute_market_breadth


def _panel(n_stocks=40, days=40, drift=0.01, seed=3):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range('2026-06-01', periods=days)
    data = {}
    for k in range(n_stocks):
        close = 10000 * np.exp(np.cumsum(rng.normal(drift, 0.005, days)))
        idx = pd.DatetimeIndex([pd.Timestamp(d, tz='Asia/Seoul') + pd.Timedelta(hours=14) for d in dates])
        data[f'{k:06d}'] = pd.DataFrame({'Close': close}, index=idx)
    return data


def test_insufficient_history_is_nan_not_zero():
    b = compute_market_breadth(_panel())
    assert b.iloc[:19].isna().all()
    assert b.iloc[19:].notna().all()
    assert not (b.iloc[:19] == 0).any()


def test_strong_uptrend_is_near_full_breadth():
    b = compute_market_breadth(_panel(drift=0.03))
    assert b.iloc[19:].min() >= 90.0


def test_prefix_invariance():
    data = _panel()
    full = compute_market_breadth(data)
    cut_day = full.index[30]
    part = compute_market_breadth({c: df[df.index.date <= cut_day] for c, df in data.items()})
    pd.testing.assert_series_equal(full.loc[:cut_day], part, check_names=False)


def test_too_few_valid_stocks_gives_nan():
    b = compute_market_breadth(_panel(n_stocks=10), min_stocks=30)
    assert b.isna().all()
