import numpy as np
import pandas as pd

from collector import (
    RESISTANCE_RSI_OVERSOLD,
    RESISTANCE_VOL_RATIO_MIN,
    resistance_breakout_rsi_candidate,
)

TOTAL_BARS = 145
PROFILE_BARS = 100  # 매물대(POC)를 형성하는 구간
DECLINE_BARS = TOTAL_BARS - PROFILE_BARS - 1  # POC 형성 후 breakout 직전까지의 과매도 하락 구간
POC_PRICE = 10080.0
POC_VOLUME = 200000.0
BASELINE_VOLUME = 5000.0


def _make_df(breakout=True, volume_surge=True, oversold=True):
    """
    145봉 합성 OHLCV: 0~99봉은 9800원대 baseline 사이사이 POC_PRICE(10080원)에
    거래량이 몰린 매물대를 형성하고, 100~143봉은 9120원까지 꾸준히 하락(과매도 유도),
    마지막 144봉이 돌파봉이다. 각 플래그로 개별 조건을 끄고 켤 수 있다.
    """
    closes = np.empty(TOTAL_BARS)
    volumes = np.empty(TOTAL_BARS)

    for i in range(PROFILE_BARS):
        if i % 5 == 4:
            closes[i] = POC_PRICE
            volumes[i] = POC_VOLUME
        else:
            closes[i] = 9800.0 + (i % 4) * 50.0
            volumes[i] = 3000.0

    decline_start = closes[PROFILE_BARS - 1]
    decline_end = 9120.0 if oversold else decline_start  # oversold=False면 하락 없이 평탄 유지
    decline = np.linspace(decline_start, decline_end, DECLINE_BARS)
    closes[PROFILE_BARS:PROFILE_BARS + DECLINE_BARS] = decline
    volumes[PROFILE_BARS:PROFILE_BARS + DECLINE_BARS] = BASELINE_VOLUME

    last_close = closes[PROFILE_BARS + DECLINE_BARS - 1]
    breakout_close = (POC_PRICE + 500.0) if breakout else (POC_PRICE - 500.0)
    closes[-1] = breakout_close
    volumes[-1] = (BASELINE_VOLUME * 60) if volume_surge else BASELINE_VOLUME

    dates = pd.date_range("2026-06-01", periods=TOTAL_BARS, freq="h", tz="Asia/Seoul")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": volumes,
        },
        index=dates,
    )


class TestResistanceBreakoutRsiCandidate:
    def test_all_conditions_met_returns_signal(self):
        df = _make_df(breakout=True, volume_surge=True, oversold=True)
        res = resistance_breakout_rsi_candidate(df)
        assert res is not None
        assert res["resistance_level"] == POC_PRICE
        assert res["vol_ratio"] >= RESISTANCE_VOL_RATIO_MIN
        assert res["rsi"] <= RESISTANCE_RSI_OVERSOLD or res["rsi"] > res["rsi_signal"]

    def test_rejects_if_not_breaking_resistance(self):
        df = _make_df(breakout=False, volume_surge=True, oversold=True)
        res = resistance_breakout_rsi_candidate(df)
        assert res is None

    def test_rejects_if_no_volume_surge(self):
        df = _make_df(breakout=True, volume_surge=False, oversold=True)
        res = resistance_breakout_rsi_candidate(df)
        assert res is None

    def test_rejects_if_never_oversold(self):
        df = _make_df(breakout=True, volume_surge=True, oversold=False)
        res = resistance_breakout_rsi_candidate(df)
        assert res is None

    def test_rejects_if_not_enough_bars(self):
        df = _make_df(breakout=True, volume_surge=True, oversold=True).iloc[-100:]
        res = resistance_breakout_rsi_candidate(df)
        assert res is None
