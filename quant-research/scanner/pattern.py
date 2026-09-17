import pandas as pd
import numpy as np
from scanner.indicators import _atr

def hourly_pattern(df: pd.DataFrame, require_volume: bool = True, volume_weight: float = 0.2, max_extension_atr: float | None = None) -> pd.DataFrame:
    """Causal MA/volume pattern scores; research hypothesis, not calibrated odds."""
    c, v = df['Close'], df['Volume']
    f, m = c.rolling(12).mean(), c.rolling(26).mean()
    atr = _atr(df)
    gap = (f - m) / atr
    fs, ms = (f - f.shift(3)) / atr, (m - m.shift(3)) / atr
    # Compare volume per minute because the final regular-session bar is 30 min.
    minutes = pd.Series(np.where(df.index.hour == 15, 30, 60), index=df.index)
    rate = v / minutes
    vr = rate.rolling(3).mean() / rate.shift(3).rolling(20).mean().replace(0, np.nan)
    clip = lambda x: x.clip(0, 100)
    proximity = clip(100 - gap.abs() * 40)
    direction = clip(50 + fs * 60 + ms * 30)
    volume = clip((vr - 0.7) / 1.8 * 100)
    room = clip(100 - ((c - m) / atr - 1).clip(lower=0) * 25)
    contraction = clip(50 + (gap.shift(6).abs() - gap.abs()) * 30)
    rebound = (proximity + direction + volume + room + contraction) / 5
    prior_width = (df.High.shift(1).rolling(6).max() - df.Low.shift(1).rolling(6).min()) / atr
    base = clip(100 - prior_width * 15)
    breakout = clip(50 + (c - df.High.shift(1).rolling(6).max()) / atr * 50)
    expansion = (base + breakout + direction + volume + room) / 5
    # A low MA gap alone must never qualify a falling chart.
    eligible = (fs > 0) & (ms > ms.shift(3)) & (c > f) & (c > m)
    if require_volume:
        eligible &= vr >= 1
    eligible &= (gap.shift(1).rolling(20).min() < 0)
    eligible &= c >= 2000
    score = np.maximum(rebound, expansion)
    if volume_weight != 0.2:
        score = (score - 0.2 * volume) * (1 - volume_weight) / 0.8 + volume_weight * volume
    if max_extension_atr is not None:
        eligible &= (c - m) / atr <= max_extension_atr
    out = pd.DataFrame({'score': score, 'volume_ratio': vr,
                        'pattern': np.where(rebound >= expansion, 'rebound', 'base_breakout'),
                        'above60': c > c.rolling(60).mean(),
                        'eligible': eligible}, index=df.index)
    out.loc[~np.isfinite(out.score), 'eligible'] = False
    return out
