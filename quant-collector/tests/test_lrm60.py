import copy

import pandas as pd

from collector import parse_multiframe_bars, render_lrm60_panel
from lrm60 import LRM60PaperBook, inspect_signal, regime_at


def signal_feed():
    times = [pd.Timestamp(day) + pd.Timedelta(hours=hour)
             for day in pd.bdate_range("2026-09-14", periods=6)
             for hour in range(9, 16)]
    frame = pd.DataFrame(
        {"Open": 100.0, "High": 108.0, "Low": 99.0,
         "Close": 100.0, "Volume": 100_000.0}, index=pd.DatetimeIndex(times)
    )
    frame.loc[frame.index[-1], ["Open", "High", "Low", "Close", "Volume"]] = [
        100, 110, 100, 109, 10_000_000,
    ]
    return frame


def index_feeds():
    dates = pd.bdate_range("2026-08-17", "2026-09-21")
    close = pd.Series([100.0] * len(dates), index=dates)
    close.iloc[-2:] = 120.0
    return {"KOSPI": pd.DataFrame({"Close": close})}


def test_completed_1500_bar_and_prefix_invariance():
    frame = signal_feed()
    at_close = inspect_signal(frame, frame.index[-1])
    assert at_close["passed"]
    assert at_close["amount_e8"] == 10.9
    assert at_close["r_vol"] >= 2.5
    extended = pd.concat([frame, pd.DataFrame(
        [{"Open": 109, "High": 120, "Low": 109, "Close": 119, "Volume": 2e7}],
        index=[pd.Timestamp("2026-09-22 09:00")])])
    assert inspect_signal(extended, frame.index[-1]) == at_close
    weak = frame.copy()
    weak.loc[weak.index[-1], "Volume"] = 100_000
    assert not inspect_signal(weak, weak.index[-1])["passed"]
    dummy = frame.copy()
    dummy.loc[dummy.index[-1], ["High", "Low", "Volume"]] = [109, 109, 0]
    assert inspect_signal(dummy, dummy.index[-1]) is None


def test_1500_half_hour_excludes_1530_post_close_print():
    times = pd.date_range("2026-09-21 15:00", "2026-09-21 15:30", freq="5min",
                          tz="Asia/Seoul")
    item = {"timestamp": [int(ts.timestamp()) for ts in times],
            "indicators": {"quote": [{
                "open": [100.0] * len(times), "high": [105.0] * 6 + [999.0],
                "low": [99.0] * len(times), "close": [105.0] * 6 + [999.0],
                "volume": [100.0] * 6 + [1_000_000.0],
            }]}}
    early = parse_multiframe_bars(item, pd.Timestamp("2026-09-21 15:20", tz="Asia/Seoul"))
    assert early["60m"].empty
    done = parse_multiframe_bars(item, pd.Timestamp("2026-09-21 15:35", tz="Asia/Seoul"))
    assert len(done["60m"]) == 1
    assert done["60m"].iloc[0]["Close"] == 105.0
    assert done["60m"].iloc[0]["Volume"] == 600.0


def test_regime_uses_only_prior_completed_day_and_fails_closed():
    feeds = index_feeds()
    ts = pd.Timestamp("2026-09-22 10:00")
    assert regime_at(feeds, "KOSPI", ts) is True
    assert regime_at(feeds, "KOSDAQ", ts) is None
    future = feeds["KOSPI"].copy()
    future.loc[pd.Timestamp("2026-09-22"), "Close"] = 1.0
    assert regime_at({"KOSPI": future}, "KOSPI", ts) is True
    assert regime_at(feeds, "KOSPI", pd.Timestamp("2026-10-10 10:00")) is None


def test_forward_fill_experiment_partial_accounting_and_idempotence(tmp_path):
    frame = signal_feed()
    metadata = {"000001": {"name": "테스트", "market": "KOSPI"}}
    official = LRM60PaperBook(tmp_path / "official.json")
    experiment = LRM60PaperBook(tmp_path / "experiment.json", experimental=True)
    for book in (official, experiment):
        assert book.advance({"000001": frame}, metadata, index_feeds())
        assert not book.state["positions"]
        assert len(book.state["pending"]) == 1
        prior = copy.deepcopy(book.state)
        assert not book.advance({"000001": frame}, metadata, index_feeds())
        assert book.state == prior

    next_bar = pd.DataFrame(
        [{"Open": 110.0, "High": 116.0, "Low": 108.0,
          "Close": 112.0, "Volume": 100_000.0}],
        index=[pd.Timestamp("2026-09-22 09:00")],
    )
    feeds = {"000001": pd.concat([frame, next_bar])}
    official.advance(feeds, metadata, index_feeds())
    experiment.advance(feeds, metadata, index_feeds())
    assert not official.state["trades"]
    assert experiment.state["trades"][0]["reason"] == "PARTIAL_TOUCH"
    exp_pos = experiment.state["positions"]["000001"]
    assert exp_pos["target_1_done"]
    assert exp_pos["qty"] < official.state["positions"]["000001"]["qty"]
    assert abs(experiment.state["cash"] + exp_pos["cost"] -
               sum(t["pnl"] for t in experiment.state["trades"]) - 1_000_000) < 0.02
    reloaded = LRM60PaperBook(tmp_path / "experiment.json", experimental=True)
    assert reloaded.state == experiment.state


def test_regime_missing_blocks_only_experiment_new_entry(tmp_path):
    frame = signal_feed()
    book = LRM60PaperBook(tmp_path / "experiment.json", experimental=True)
    book.advance({"000001": frame}, {"000001": {"market": "KOSPI"}}, {})
    assert book.state["coverage"]["passed"] == 1
    assert book.state["regime"]["KOSPI"] is None
    assert book.state["pending"] == []


def test_dashboard_keeps_official_and_experiment_separate():
    official = {"last_bar_ts": "2026-09-21T15:00:00", "coverage": {"passed": 1},
                "gate_counts": {}, "pending": [], "positions": {}, "equity": 1_000_000,
                "last_candidates": []}
    experiment = {"regime": {"KOSPI": True, "KOSDAQ": None}, "pending": [],
                  "positions": {}, "equity": 1_005_000}
    panel = "\n".join(render_lrm60_panel(official, "##", experiment))
    assert "4대 게이트 통과:** `1건`" in panel
    assert "KOSPI 통과" in panel and "KOSDAQ 데이터 없음" in panel
    assert "1,005,000원" in panel and "공식 v2.0과 성과를 합산하지 않습니다" in panel
    panel = "\n".join(render_lrm60_panel(
        {**official, "last_bar_ts": "2026-09-21T14:00:00+09:00"}, "##", experiment,
        "2026-09-21 15:52"))
    assert "15:00~15:30 완료봉이 공급되지 않아" in panel
