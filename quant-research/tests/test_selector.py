"""Tests for scanner.selector — design doc §5.4 rules."""
import numpy as np
import pandas as pd
import pytest


def _make_row(code, name, score, match=True, base=True, waiting=False):
    """Helper to create a scored row matching pipeline output format."""
    return {
        "종목": name, "코드": code, "점수": score,
        "_match": match, "_base": base, "_waiting": waiting,
        "_breakout": match, "_liquid": match, "_trigger": match,
        "_hold_price": match, "_relative": True,
        "_current_amount": 15.0, "_current_ratio": 2.5,
        "_current_level": 10000, "_current_close": 10500,
        "_bar_time": "2026-09-01T11:00:00+09:00",
        "기준봉(KST)": "09-01 11:00",
    }


class TestTopFive:
    def test_empty_input(self):
        from scanner.selector import select_top
        result = select_top([])
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0

    def test_max_five(self):
        from scanner.selector import select_top
        rows = [_make_row(f"00{i}000", f"종목{i}", 80 - i) for i in range(8)]
        result = select_top(rows)
        assert len(result) == 5

    def test_score_descending(self):
        from scanner.selector import select_top
        rows = [
            _make_row("005930", "삼성전자", 75),
            _make_row("000660", "SK하이닉스", 82),
            _make_row("035420", "NAVER", 70),
        ]
        result = select_top(rows)
        scores = result["점수"].tolist()
        assert scores == sorted(scores, reverse=True)

    def test_code_tiebreak(self):
        from scanner.selector import select_top
        rows = [
            _make_row("005930", "삼성전자", 80),
            _make_row("000660", "SK하이닉스", 80),
        ]
        result = select_top(rows)
        codes = result["코드"].tolist()
        assert codes == ["000660", "005930"]  # ascending code for same score

    def test_excludes_non_match(self):
        from scanner.selector import select_top
        rows = [
            _make_row("005930", "삼성전자", 90, match=True),
            _make_row("000660", "SK하이닉스", 95, match=False),
        ]
        result = select_top(rows)
        assert len(result) == 1
        assert result.iloc[0]["코드"] == "005930"

    def test_zero_qualified_is_ok(self):
        from scanner.selector import select_top
        rows = [_make_row("005930", "삼성전자", 90, match=False)]
        result = select_top(rows)
        assert len(result) == 0


class TestWatchTable:
    def test_waiting_items_included(self):
        from scanner.selector import build_watch
        rows = [_make_row("005930", "삼성전자", 80, match=False, base=True, waiting=True)]
        watch = build_watch(rows)
        assert len(watch) == 1

    def test_matched_items_excluded(self):
        from scanner.selector import build_watch
        rows = [_make_row("005930", "삼성전자", 80, match=True, base=True, waiting=True)]
        watch = build_watch(rows)
        assert len(watch) == 0

    def test_non_base_excluded(self):
        from scanner.selector import build_watch
        rows = [_make_row("005930", "삼성전자", 80, match=False, base=False, waiting=True)]
        watch = build_watch(rows)
        assert len(watch) == 0

    def test_non_waiting_excluded(self):
        from scanner.selector import build_watch
        rows = [_make_row("005930", "삼성전자", 80, match=False, base=True, waiting=False)]
        watch = build_watch(rows)
        assert len(watch) == 0
