"""Prefix invariance test: future data must NOT change past signals.

Design doc §12: 전체 데이터를 계산해 앞부분을 자른 결과와 해당 시점까지만
계산한 결과가 같아야 함(미래정보 불사용).
"""
import numpy as np
import pandas as pd
import pytest


def _make_sinusoidal_bars(days=25):
    """Deterministic test bars matching original test_hourly_pattern.py::bars()."""
    idx = pd.DatetimeIndex(
        [pd.Timestamp(d).tz_localize("Asia/Seoul") + pd.Timedelta(hours=h)
         for d in pd.bdate_range("2026-07-01", periods=days)
         for h in range(9, 16)]
    )
    c = 3000 + np.sin(np.arange(len(idx)) / 8) * 100
    return pd.DataFrame(
        {"Open": c, "High": c + 10, "Low": c - 10,
         "Close": c, "Volume": np.full(len(idx), 1000.0)},
        index=idx,
    )


class TestPrefixInvariance:
    """Past scores must not change when future data is added or modified."""

    def test_pattern_prefix_invariance(self):
        """hourly_pattern scores at bar N must be the same whether computed
        with N bars or N+K bars (no lookahead)."""
        from scanner.pattern import hourly_pattern

        df = _make_sinusoidal_bars(25)
        full_result = hourly_pattern(df)

        # Take first 120 bars, compute
        prefix = df.iloc[:120]
        prefix_result = hourly_pattern(prefix)

        # The first 120 rows of full_result must equal prefix_result
        pd.testing.assert_frame_equal(
            full_result.iloc[:120], prefix_result,
            check_names=False,
        )

    def test_pattern_future_price_change_no_effect(self):
        """Changing prices after bar 120 must not affect scores before bar 120."""
        from scanner.pattern import hourly_pattern

        df = _make_sinusoidal_bars(25)
        original = hourly_pattern(df)

        # Mutate future bars
        modified = df.copy()
        modified.iloc[120:, modified.columns.get_indexer(["Open", "High", "Low", "Close"])] *= 3

        modified_result = hourly_pattern(modified)
        pd.testing.assert_frame_equal(
            original.iloc[:120], modified_result.iloc[:120],
            check_names=False,
        )

    def test_breakout_prefix_invariance(self):
        """confirmed_breakout at bar N must be the same with or without future bars."""
        from scanner.breakout import confirmed_breakout

        df = _make_sinusoidal_bars(25)
        full_result = confirmed_breakout(df)
        prefix_result = confirmed_breakout(df.iloc[:120])

        pd.testing.assert_frame_equal(
            full_result.iloc[:120], prefix_result,
            check_names=False,
        )


class TestCausalOrdering:
    """Entry candidate selection must not use entry bar's final data."""

    def test_entry_bar_volume_change_no_selection_change(self):
        """Changing entry bar volume must not affect which stock was selected.
        Design doc §8.2: 후보 선정에 진입 봉의 최종 거래량·고가·저가 사용 금지."""
        from scanner.pattern import hourly_pattern

        df = _make_sinusoidal_bars(25)
        scores_a = hourly_pattern(df)

        modified = df.copy()
        modified.iloc[-7:, modified.columns.get_loc("Volume")] = 0

        scores_b = hourly_pattern(modified)

        # Scores up to the last signal bar (not the entry bar) must be identical
        # The last 7 bars could be affected, but bars before them must not
        n = len(df) - 7
        pd.testing.assert_frame_equal(
            scores_a.iloc[:n], scores_b.iloc[:n],
            check_names=False,
        )
