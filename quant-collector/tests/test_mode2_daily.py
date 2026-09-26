from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from mode2_daily import (END, START, chart_to_frame, choose_session,
                         render_panel, update_readme)
from pure_quant_portfolio_manager import compute_factor_rankings
from factor_evidence import explain_rank, profile_fact


KST = ZoneInfo("Asia/Seoul")


def daily_frame(last="2026-09-25", rows=150, scale=1.0):
    dates = pd.bdate_range(end=last, periods=rows)
    close = np.linspace(80.0, 160.0, rows) * scale
    return pd.DataFrame({"Open": close * .995, "High": close * 1.02,
                         "Low": close * .98, "Close": close,
                         "Volume": np.full(rows, 10_000_000)}, index=dates)


def test_chart_requires_matching_symbol_and_valid_ohlcv():
    stamp = int(pd.Timestamp("2026-09-23 09:00", tz=KST).timestamp())
    payload = {"chart": {"error": None, "result": [{
        "meta": {"symbol": "005930.KS", "exchangeTimezoneName": "Asia/Seoul"},
        "timestamp": [stamp],
        "indicators": {"quote": [{"open": [100], "high": [110],
                                  "low": [90], "close": [105], "volume": [1000]}]},
    }]}}
    assert chart_to_frame(payload, "005930.KS").index[0] == pd.Timestamp("2026-09-23")
    with pytest.raises(ValueError, match="symbol"):
        chart_to_frame(payload, "000660.KS")
    payload["chart"]["result"][0]["indicators"]["quote"][0]["high"] = [99]
    with pytest.raises(ValueError, match="OHLCV"):
        chart_to_frame(payload, "005930.KS")


def test_common_session_excludes_incomplete_today_and_accepts_prior_session():
    frames = {f"{i:06d}": daily_frame() for i in range(120)}
    now = datetime(2026, 9, 25, 15, 50, tzinfo=KST)
    date, aligned = choose_session(frames, now)
    assert date == pd.Timestamp("2026-09-24")
    assert len(aligned) == 120
    date, _ = choose_session(frames, datetime(2026, 9, 25, 16, 20, tzinfo=KST))
    assert date == pd.Timestamp("2026-09-25")


def test_factor_momentum_matches_shift_5_and_60():
    frame = daily_frame()
    ranking = compute_factor_rankings({"000001": frame}, frame.index[-1])
    assert len(ranking) == 1
    expected = (frame["Close"].iloc[-5] - frame["Close"].iloc[-60]) / frame["Close"].iloc[-60]
    assert ranking.iloc[0]["mom60_5"] == pytest.approx(expected)


def test_factor_explanation_uses_only_observed_values_and_score_parts():
    row = SimpleNamespace(mom60_5=.42, risk_adj_mom=.81, cmf20=-.12,
                          rank_ramom=.8, rank_mom=.7, rank_cmf=.2,
                          composite_score=.59)
    reason = explain_rank(row)
    assert "+42.0%" in reason and "-0.12" in reason
    assert "0.320 + 추세 0.210 + CMF 0.060" in reason
    assert "기관" not in reason and "샤프" not in reason
    with pytest.raises(ValueError, match="contributions"):
        explain_rank(SimpleNamespace(**{**vars(row), "composite_score": .99}))
    assert "cosmax.com" in profile_fact("192820")
    assert profile_fact("999999") == ""


def test_panel_stale_data_preserves_plan_and_readme_sections(tmp_path):
    frames = {f"{i:06d}": daily_frame(last="2026-09-23", scale=1 + i / 1000)
              for i in range(120)}
    date = pd.Timestamp("2026-09-23")
    state = {"last_plan_date": "2026-09-22", "top_codes": ["000001"],
             "reference_closes": {"000001": 160.0}}
    panel, new_state = render_panel(datetime(2026, 9, 25, 16, 20, tzinfo=KST),
                                    date, frames, {}, tmp_path / "paper.json", state)
    assert "오늘 날짜의 새 완료 일봉이 없습니다" in panel
    assert "2026-09-23 기준 신규 편입 검토 가능" in panel
    assert "2026-09-23 완료 일봉 기준 상위 10종목 · 과거 관찰표" in panel
    assert panel.count("| 10 |") == 1
    assert "SUE 점수와 실적 촉매 핫스왑은 운영 판정에 포함하지 않습니다" in panel
    assert "기관·외국인 순매수를 식별하지 않습니다" in panel
    assert "수신 원본·실패·해시" in panel
    assert new_state == state
    readme = tmp_path / "README.md"
    readme.write_text("before\n" + START + "\nold\n" + END + "\nlegacy\n", encoding="utf-8")
    update_readme(panel, readme)
    text = readme.read_text(encoding="utf-8")
    assert "legacy" in text and "old" not in text and "모드 2" in text


def test_stale_session_shows_dated_rankings_without_creating_plan(tmp_path):
    frames = {f"{i:06d}": daily_frame(last="2026-09-23", scale=1 + i / 1000)
              for i in range(120)}
    panel, state = render_panel(datetime(2026, 9, 26, 16, 52, tzinfo=KST),
                                pd.Timestamp("2026-09-23"), frames, {},
                                tmp_path / "paper.json", {})
    assert state == {}
    assert "2026-09-23 완료 일봉 기준 상위 10종목 · 과거 관찰표" in panel
    assert "다음 거래일 시가나 신규 편입 확정을 뜻하지 않습니다" in panel
    assert panel.count("| 10 |") == 1


def test_panel_current_session_creates_ten_targets_only_once(tmp_path):
    frames = {f"{i:06d}": daily_frame(scale=1 + i / 1000) for i in range(120)}
    now = datetime(2026, 9, 25, 16, 20, tzinfo=KST)
    date = pd.Timestamp("2026-09-25")
    panel, state = render_panel(now, date, frames, {}, tmp_path / "paper.json", {})
    assert len(state["top_codes"]) == 10
    assert "20거래일 리밸런싱 검토표" in panel
    _, again = render_panel(now + timedelta(minutes=1), date, frames, {},
                            tmp_path / "paper.json", state)
    assert again == state
