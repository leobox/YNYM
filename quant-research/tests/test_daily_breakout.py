"""T-052: scanner.daily_breakout / scanner.signal_board 테스트 (네트워크 없음, 합성 데이터)."""
import numpy as np
import pandas as pd
import pytest

from scanner import daily_breakout as db
from scanner import signal_board as sb
from scanner.vcp import detect_vcp


def _uptrend(n=150, breakout=True, vol_mult=3.0):
    """완만한 상승 추세 + 마지막 봉 60일 최고가 돌파(양봉, 거래량 vol_mult배)."""
    idx = pd.bdate_range("2026-01-02", periods=n)
    close = 10000.0 * (1.003 ** np.arange(n))
    wiggle = 1 + 0.004 * np.sin(np.arange(n) * 1.3)
    close = close * wiggle
    open_ = close * 0.998
    high = np.maximum(open_, close) * 1.004
    low = np.minimum(open_, close) * 0.996
    vol = np.full(n, 100000.0)
    df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx)
    if breakout:
        pivot = df["High"].iloc[-61:-1].max()
        df.iloc[-1, df.columns.get_loc("Open")] = pivot * 0.995
        df.iloc[-1, df.columns.get_loc("Close")] = pivot * 1.012
        df.iloc[-1, df.columns.get_loc("High")] = pivot * 1.015
        df.iloc[-1, df.columns.get_loc("Low")] = pivot * 0.993
        df.iloc[-1, df.columns.get_loc("Volume")] = 100000.0 * vol_mult
    return df


class TestChecks:
    def test_ideal_breakout_is_signal(self):
        out = db.daily_checks(_uptrend())
        last = out.iloc[-1]
        assert last["trend_ok"] and last["breakout_ok"] and last["volume_ok"] and last["calm_ok"]
        assert last["signal"]

    def test_low_volume_is_not_signal(self):
        last = db.daily_checks(_uptrend(vol_mult=1.2)).iloc[-1]
        assert last["breakout_ok"] and not last["volume_ok"] and not last["signal"]

    def test_no_breakout_is_not_signal(self):
        last = db.daily_checks(_uptrend(breakout=False)).iloc[-1]
        assert not last["breakout_ok"] and not last["signal"]

    def test_downtrend_is_not_signal(self):
        df = _uptrend()
        df = df.iloc[::-1].reset_index(drop=True).set_index(_uptrend().index)  # 가격 순서를 뒤집어 하락 추세
        assert not db.daily_checks(df).iloc[-1]["signal"]

    def test_overheated_is_not_signal(self):
        df = _uptrend()
        df.iloc[-1, df.columns.get_loc("Close")] *= 1.25
        df.iloc[-1, df.columns.get_loc("High")] = df["Close"].iloc[-1] * 1.002
        last = db.daily_checks(df).iloc[-1]
        assert last["breakout_ok"] and not last["calm_ok"] and not last["signal"]

    def test_penny_stock_rejected(self):
        df = _uptrend() * 0.1  # 종가 2,000원 미만
        df["Volume"] = _uptrend()["Volume"]
        assert not db.daily_checks(df).iloc[-1]["signal"]

    def test_prefix_invariance(self):
        df = _uptrend()
        full = db.daily_checks(df)
        cols = ["signal", "trend_ok", "breakout_ok", "volume_ok", "calm_ok", "pivot_level", "vol_spike"]
        for cut in (80, 110, 140, len(df) - 1):
            part = db.daily_checks(df.iloc[:cut])
            pd.testing.assert_frame_equal(full.iloc[:cut][cols], part[cols])

    def test_future_bar_does_not_change_past_signal(self):
        df = _uptrend()
        before = db.daily_checks(df).iloc[-1]["signal"]
        extra = df.iloc[[-1]].copy()
        extra.index = [df.index[-1] + pd.Timedelta(days=1)]
        extra[["Open", "High", "Low", "Close"]] *= 0.5
        after = db.daily_checks(pd.concat([df, extra])).iloc[len(df) - 1]["signal"]
        assert before == after

    def test_parity_with_audited_detect_vcp(self):
        """감사 때 검증한 detect_vcp(lookback_pivot=60, max_vcp_ratio=99)와 신호가 같아야 한다."""
        total = 0
        for seed in range(6):
            rng = np.random.default_rng(seed)
            n = 500
            idx = pd.bdate_range("2024-01-02", periods=n)
            close = 10000 * np.exp(np.cumsum(rng.normal(0.002, 0.02, n)))
            open_ = close * (1 + rng.normal(0, 0.006, n))
            high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, n)))
            low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, n)))
            vol = np.exp(rng.normal(11.5, 0.6, n))
            df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx)
            new = db.daily_checks(df)["signal"].values
            old = detect_vcp(df, lookback_pivot=60, max_vcp_ratio=99)["eligible"].values
            assert (new == old).all(), f"seed {seed} 불일치"
            total += new.sum()
        assert total > 0, "패리티 테스트가 신호 없이 통과하면 의미가 없다"


class TestWatch:
    def test_watch_requirements(self):
        df = _uptrend(breakout=False)
        last = db.daily_checks(df).iloc[-1]
        # 마지막 종가를 돌파선 2% 아래로 맞춘다
        pivot = df["High"].iloc[-60:].max()
        df.iloc[-1, df.columns.get_loc("Close")] = pivot / 1.02
        df.iloc[-1, df.columns.get_loc("High")] = min(df["High"].iloc[-1], pivot)
        df.iloc[-1, df.columns.get_loc("Open")] = df["Close"].iloc[-1] * 0.998
        df.iloc[-1, df.columns.get_loc("Low")] = df["Close"].iloc[-1] * 0.995
        last = db.daily_checks(df).iloc[-1]
        need = db.next_session_requirements(df, last)
        assert need is not None
        assert need["pivot_next"] == pytest.approx(df["High"].iloc[-60:].max())
        assert 0 <= need["gap_pct"] <= 4.0
        assert need["req_volume"] == pytest.approx(df["Volume"].iloc[-20:].mean() * 1.5)

    def test_signal_day_is_not_watch(self):
        df = _uptrend()
        last = db.daily_checks(df).iloc[-1]
        assert last["signal"]
        assert db.next_session_requirements(df, last) is None

    def test_far_from_pivot_is_not_watch(self):
        df = _uptrend(breakout=False)
        pivot = df["High"].iloc[-60:].max()
        df.iloc[-1, df.columns.get_loc("Close")] = pivot / 1.10
        last = db.daily_checks(df).iloc[-1]
        assert db.next_session_requirements(df, last) is None

    def test_zero_volume_last_bar_not_watch(self):
        df = _uptrend(breakout=False)
        last = db.daily_checks(df).iloc[-1]
        df.iloc[-1, df.columns.get_loc("Volume")] = 0.0
        assert db.next_session_requirements(df, last) is None


class TestCompletedBars:
    def _df(self):
        idx = pd.to_datetime(["2026-09-17", "2026-09-18", "2026-09-21"])
        return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0}, index=idx)

    def test_today_bar_dropped_before_close(self):
        now = pd.Timestamp("2026-09-21 11:30", tz="Asia/Seoul")
        assert list(db.drop_incomplete_today(self._df(), now).index) == list(pd.to_datetime(["2026-09-17", "2026-09-18"]))

    def test_today_bar_kept_after_close(self):
        now = pd.Timestamp("2026-09-21 16:10", tz="Asia/Seoul")
        assert len(db.drop_incomplete_today(self._df(), now)) == 3

    def test_utc_now_is_converted_to_kst(self):
        now = pd.Timestamp("2026-09-21 02:30", tz="UTC")  # KST 11:30
        assert len(db.drop_incomplete_today(self._df(), now)) == 2

    def test_clean_daily_bars_drops_bad_rows(self):
        idx = pd.to_datetime(["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-17", "2026-09-18"])
        df = pd.DataFrame({
            "Open": [10, 10, 10, 10, np.nan], "High": [11, 9, 11, 12, 11],
            "Low": [9, 8, 9, 9, 9], "Close": [10, 10, 10, 11, 10], "Volume": [5, 5, -1, 5, 5],
        }, index=idx, dtype=float)
        out = db.clean_daily_bars(df)
        # 09-16: High<Open/Close 모순, 09-17 첫 행(음수 거래량)은 중복 후 마지막 행 채택, 09-18: NaN
        assert list(out.index) == list(pd.to_datetime(["2026-09-15", "2026-09-17"]))
        assert out.loc["2026-09-17", "Close"] == 11


def _daily(rows):
    """rows: (open, high, low, close, volume) 리스트, 신호일(2026-09-01) 다음 거래일부터."""
    idx = pd.bdate_range("2026-09-02", periods=len(rows))
    sig = pd.DataFrame([(100, 100, 100, 100, 1)], columns=["Open", "High", "Low", "Close", "Volume"],
                       index=[pd.Timestamp("2026-09-01")])
    body = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)
    return pd.concat([sig, body])


SIG = pd.Timestamp("2026-09-01")


class TestResolveOutcome:
    def test_pending_before_next_session(self):
        assert db.resolve_outcome(_daily([]), SIG)["status"] == "PENDING"

    def test_target_hit(self):
        out = db.resolve_outcome(_daily([(100, 100, 100, 100, 1), (100, 112, 99, 111, 1)]), SIG)
        assert out["status"] == "TARGET"
        assert out["exit_price"] == pytest.approx(100 * 1.0005 * 1.10)
        assert out["net_return_pct"] > 9  # 비용 반영 후에도 약 +9%대

    def test_stop_hit(self):
        out = db.resolve_outcome(_daily([(100, 100, 100, 100, 1), (100, 101, 90, 92, 1)]), SIG)
        assert out["status"] == "STOP" and out["net_return_pct"] < -5

    def test_same_bar_touch_is_stop_first(self):
        out = db.resolve_outcome(_daily([(100, 115, 90, 100, 1)]), SIG)
        assert out["status"] == "STOP"

    def test_gap_down_exits_at_open(self):
        out = db.resolve_outcome(_daily([(100, 100, 100, 100, 1), (80, 82, 78, 80, 1)]), SIG)
        assert out["status"] == "STOP" and out["exit_price"] == 80

    def test_gap_up_fills_target_price_not_open(self):
        out = db.resolve_outcome(_daily([(100, 100, 100, 100, 1), (130, 135, 128, 132, 1)]), SIG)
        assert out["status"] == "TARGET" and out["exit_price"] == pytest.approx(100 * 1.0005 * 1.10)

    def test_time_exit_after_five_sessions(self):
        rows = [(100, 101, 99, 100, 1)] * 5
        out = db.resolve_outcome(_daily(rows), SIG)
        assert out["status"] == "TIME" and out["exit_date"] == _daily(rows).index[5]

    def test_open_before_five_sessions(self):
        assert db.resolve_outcome(_daily([(100, 101, 99, 100, 1)] * 3), SIG)["status"] == "OPEN"

    def test_no_fill_on_zero_volume_entry(self):
        assert db.resolve_outcome(_daily([(100, 101, 99, 100, 0)]), SIG)["status"] == "NO_FILL"

    def test_bars_after_five_sessions_ignored(self):
        rows = [(100, 101, 99, 100, 1)] * 5 + [(100, 200, 100, 200, 1)]
        assert db.resolve_outcome(_daily(rows), SIG)["status"] == "TIME"


def _rec(code, turnover, gap=1.0, signal_date="2026-09-01"):
    return {"code": code, "name": f"종목{code}", "market": "KOSDAQ", "close": 10000.0, "pivot_level": 9900.0,
            "vol_spike": 2.34, "extension_atr": 1.5, "turnover": turnover, "signal_date": signal_date,
            "pivot_next": 10100.0, "gap_pct": gap, "req_volume": 654321.0}


META = {"now_str": "2026-09-21 16:10", "last_session": "2026-09-18", "n_total": 150, "n_ok": 148, "n_fail": 2}
EMPTY = sb.summarize_ledger(sb.new_ledger())


class TestBoard:
    def test_top5_cap_and_deterministic_order(self):
        recs = [_rec(f"{i:06d}", turnover=100.0) for i in range(8)]  # 전부 동점 -> 코드 오름차순
        top, more = sb.pick_greens(recs)
        assert [r["code"] for r in top] == [f"{i:06d}" for i in range(5)] and more == 3

    def test_yellow_sorted_by_gap_then_code(self):
        top, more = sb.pick_yellows([_rec("000003", 1, 2.0), _rec("000001", 1, 0.5), _rec("000002", 1, 0.5)])
        assert [r["code"] for r in top] == ["000001", "000002", "000003"] and more == 0

    def test_no_signal_day_text(self):
        text = sb.render_board(META, [], 0, [], 0, EMPTY)
        assert "🟢 0개" in text and "쉬는 날" in text and "통과한 종목이 없습니다" in text
        assert "불안정" not in text

    def test_market_context_line(self):
        text = sb.render_board({**META, "n_trend": 10}, [], 0, [], 0, EMPTY)
        assert "상승 추세인 종목: 10개" in text
        assert "상승 추세인 종목" not in sb.render_board(META, [], 0, [], 0, EMPTY)  # 값이 없으면 만들지 않음

    def test_degraded_run_never_says_no_signal(self):
        meta = {**META, "n_fail": 60}
        text = sb.render_board(meta, [], 0, [], 0, EMPTY)
        assert "데이터 수신이 불안정" in text and "통과한 종목이 없습니다" not in text

    def test_green_and_yellow_cards(self):
        text = sb.render_board(META, [_rec("089970", 5.0)], 0, [_rec("347700", 1, 3.3)], 0, EMPTY)
        assert "🟢 종목089970 (089970)" in text and "거래량 평소의 **2.3배**" in text
        assert "🟡 종목347700 (347700)" in text and "+3.3%" in text and "65.4만 주 이상" in text

    def test_overflow_note(self):
        top, more = sb.pick_greens([_rec(f"{i:06d}", float(i)) for i in range(7)])
        assert "외 2개" in sb.render_board(META, top, more, [], 0, EMPTY)

    def test_forbidden_wording_absent(self):
        text = sb.render_board(META, [_rec("000001", 1.0)], 0, [_rec("000002", 1, 1.0)], 0, EMPTY)
        for banned in ("매수 검토", "추천 매수", "매수가", "확률 90", "승률 9"):
            assert banned not in text
        assert "검증이 끝나지 않은" in text and "이기는 횟수보다 지는 횟수가 훨씬 많습니다" in text

    def test_no_zero_price_placeholder(self):
        import re
        text = sb.render_board(META, [_rec("000001", 1.0)], 0, [_rec("000002", 1.0)], 0, EMPTY)
        assert not re.search(r"(?<![\d,])0원", text)


class TestLedger:
    def test_append_is_idempotent_and_keeps_code_zero_padding(self):
        ledger = sb.append_signals(sb.new_ledger(), [_rec("000660", 1.0)], "run1")
        ledger = sb.append_signals(ledger, [_rec("000660", 1.0), _rec("005930", 1.0)], "run2")
        assert list(ledger["code"]) == ["000660", "005930"]
        assert list(ledger["first_run_id"]) == ["run1", "run2"]

    def test_resolve_updates_and_leaves_missing_untouched(self):
        ledger = sb.append_signals(sb.new_ledger(), [_rec("000001", 1.0), _rec("000002", 1.0)], "r")
        daily = _daily([(100, 100, 100, 100, 1), (100, 112, 99, 111, 1)])
        out = sb.resolve_ledger(ledger, {"000001": daily}, db.resolve_outcome)
        assert out.loc[out.code == "000001", "status"].iloc[0] == "TARGET"
        assert out.loc[out.code == "000002", "status"].iloc[0] == "PENDING"  # 일봉 없음 -> 값을 만들지 않음
        assert sb.unresolved_codes(out) == [("000002", "KOSDAQ")]

    def test_resolved_rows_are_not_re_resolved(self):
        ledger = sb.append_signals(sb.new_ledger(), [_rec("000001", 1.0)], "r")
        daily = _daily([(100, 100, 100, 100, 1), (100, 112, 99, 111, 1)])
        once = sb.resolve_ledger(ledger, {"000001": daily}, db.resolve_outcome)
        twice = sb.resolve_ledger(once, {"000001": daily.iloc[:2]}, db.resolve_outcome)
        assert twice.loc[0, "status"] == "TARGET"

    def test_summary_and_small_sample_wording(self):
        ledger = sb.append_signals(sb.new_ledger(), [_rec("000001", 1.0), _rec("000002", 1.0)], "r")
        ledger = sb.resolve_ledger(ledger, {"000001": _daily([(100, 100, 100, 100, 1), (100, 112, 99, 111, 1)])},
                                   db.resolve_outcome)
        s = sb.summarize_ledger(ledger)
        assert s["total"] == 2 and s["target"] == 1 and s["open"] == 1 and s["resolved"] == 1
        text = sb.render_board(META, [], 0, [], 0, s)
        assert "판단하기엔 아직 너무 적습니다" in text

    def test_empty_ledger_wording(self):
        assert "아직 기록된 신호가 없습니다" in sb.render_board(META, [], 0, [], 0, EMPTY)
