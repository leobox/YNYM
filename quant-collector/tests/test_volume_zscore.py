import numpy as np
import pandas as pd
import pytest

from collector import (
    VOLUME_ZSCORE_LOOKBACK_DAYS,
    VOLUME_ZSCORE_MAX_EXTENSION_PCT,
    VOLUME_ZSCORE_MIN,
    VOLUME_ZSCORE_MIN_VALUE_FLOOR,
    volume_zscore_accel_candidate,
)


def _make_mock_df(
    last3_opens=(10000, 10500, 11000),
    last3_closes=(10500, 11000, 11500),
    last3_volumes=(100000, 120000, 150000),
    prior_mean_volume=20000,
    daily_close_trend="up",
):
    """테스트용 OHLCV 24봉(21일치 일봉 생성 가능) 합성 DataFrame 생성"""
    dates = pd.date_range("2026-08-01", periods=24, freq="B", tz="Asia/Seoul")
    n = len(dates)

    opens = np.full(n, 10000.0)
    highs = np.full(n, 10500.0)
    lows = np.full(n, 9500.0)
    closes = np.full(n, 10000.0)
    volumes = np.full(n, prior_mean_volume)

    # 이전 21일의 일별 거래대금 분포가 안정적이도록 설정 (mean ~ 2억, std ~ 0.2억)
    for i in range(n - 3):
        closes[i] = 10000.0 + (i % 3) * 50.0
        opens[i] = 10000.0
        volumes[i] = prior_mean_volume

    # 마지막 3봉 설정
    opens[-3:] = last3_opens
    closes[-3:] = last3_closes
    volumes[-3:] = last3_volumes
    for i in range(n - 3, n):
        highs[i] = max(opens[i], closes[i]) + 200.0
        lows[i] = min(opens[i], closes[i]) - 200.0

    if daily_close_trend == "down":
        # 당일 종가가 전일 종가보다 낮게 설정
        closes[-1] = closes[-4] - 500.0

    df = pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volumes,
        },
        index=dates,
    )
    return df


class TestVolumeZscoreAccelCandidate:
    def test_all_bullish_and_increasing_passes(self):
        """최근 3봉 모두 양봉(Close > Open)이고 연속 종가 상승이면 통과"""
        df = _make_mock_df(
            last3_opens=(10000, 10500, 11000),
            last3_closes=(10500, 11000, 11500),
            last3_volumes=(100000, 120000, 150000),
            prior_mean_volume=20000,
        )
        res = volume_zscore_accel_candidate(df)
        assert res is not None
        assert res["value_z"] >= VOLUME_ZSCORE_MIN
        assert res["support_level"] == 10500.0

    def test_rejects_if_any_of_last3_is_bearish(self):
        """최근 3봉 중 하나라도 음봉(Close <= Open)이면 종가가 상승했더라도 배제 (두산밥캣 유형)"""
        # 첫 번째 봉이 10600 시가, 10500 종가 (음봉)이지만 10500 < 11000 < 11500으로 종가는 상승
        df = _make_mock_df(
            last3_opens=(10600, 10500, 11000),  # 첫 봉이 음봉!
            last3_closes=(10500, 11000, 11500),
            last3_volumes=(100000, 120000, 150000),
            prior_mean_volume=20000,
        )
        res = volume_zscore_accel_candidate(df)
        assert res is None, "3봉 중 음봉이 포함되어 있으면 배제되어야 함"

    def test_rejects_if_doji_in_last3(self):
        """Close == Open(도지형)인 봉이 포함되면 양봉(Close > Open)이 아니므로 배제"""
        df = _make_mock_df(
            last3_opens=(10500, 10500, 11000),  # 첫 봉이 도지 (Open == Close)
            last3_closes=(10500, 11000, 11500),
            last3_volumes=(100000, 120000, 150000),
            prior_mean_volume=20000,
        )
        res = volume_zscore_accel_candidate(df)
        assert res is None

    def test_rejects_if_close_not_strictly_increasing(self):
        """양봉이더라도 종가가 연속 상승하지 않으면 배제"""
        df = _make_mock_df(
            last3_opens=(10000, 10000, 10500),
            last3_closes=(10500, 10500, 11000),  # 10500 == 10500 (동일 종가)
            last3_volumes=(100000, 120000, 150000),
            prior_mean_volume=20000,
        )
        res = volume_zscore_accel_candidate(df)
        assert res is None

    def test_rejects_if_daily_close_down(self):
        """전일 대비 당일 종가 하락(데드캣 바운스) 배제"""
        df = _make_mock_df(
            last3_opens=(10000, 10500, 11000),
            last3_closes=(10500, 11000, 11500),
            last3_volumes=(100000, 120000, 150000),
            prior_mean_volume=20000,
            daily_close_trend="down",
        )
        res = volume_zscore_accel_candidate(df)
        assert res is None
