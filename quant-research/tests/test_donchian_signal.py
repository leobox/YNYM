import importlib.util
import os

import numpy as np
import pandas as pd

_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'donchian_sweep.py')
_spec = importlib.util.spec_from_file_location('donchian_sweep', _path)
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)


def _synthetic(days=40, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.DatetimeIndex([
        pd.Timestamp(d, tz='Asia/Seoul') + pd.Timedelta(hours=h)
        for d in pd.bdate_range('2026-06-01', periods=days) for h in range(9, 15)
    ])
    close = 10000 * np.exp(np.cumsum(rng.normal(0.002, 0.01, len(idx))))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, len(idx)))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, len(idx)))
    vol = rng.integers(50_000, 400_000, len(idx)).astype(float)
    vol[rng.random(len(idx)) < 0.15] *= 4  # 거래량 급증 봉
    return pd.DataFrame({'Open': open_, 'High': high, 'Low': low, 'Close': close, 'Volume': vol}, index=idx)


def test_prefix_invariance_all_filters():
    df = _synthetic()
    for trend in (0, 1):
        for ext in (0, 1):
            params = dict(N=20, mult=1.5, trend=trend, ext=ext)
            full = ds.donchian_signal(df, **params)
            if not trend and not ext:
                assert full.eligible.sum() >= 3, 'signals needed, else the test is vacuous'
            for cut in (90, 130, 170, 200):
                part = ds.donchian_signal(df.iloc[:cut], **params)
                pd.testing.assert_frame_equal(full.iloc[:cut], part, check_dtype=False)


def test_breakout_needs_close_above_prior_high_excluding_current_bar():
    df = _synthetic()
    sig = ds.donchian_signal(df, N=20, mult=1.0, trend=0, ext=0)
    prior_high = df.High.rolling(20).max().shift(1)
    hit = sig.eligible
    assert hit.sum() >= 3, 'signals needed, else the test is vacuous'
    assert (df.Close[hit] > prior_high[hit]).all()
    assert (df.Close[hit] > df.Open[hit]).all()


def test_zero_or_missing_volume_median_is_not_eligible():
    df = _synthetic()
    df.loc[df.index.hour == 10, 'Volume'] = 0.0
    sig = ds.donchian_signal(df, N=20, mult=1.0, trend=0, ext=0)
    assert not sig.eligible[df.index.hour == 10].any()
    assert np.isfinite(sig.score).all()
