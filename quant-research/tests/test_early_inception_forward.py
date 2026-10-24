"""Source freshness and immutable forward decisions for the research observer."""

import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from scripts.run_early_inception_forward import (
    build_record, capture, consecutive_observation_days, inspect, resolve,
    update_readme_panel,
)
import scripts.run_early_inception_forward as forward
from scripts.fetch_daily_history import include_unresolved


@pytest.fixture
def source(tmp_path):
    data = tmp_path / "daily"
    data.mkdir()
    days = pd.bdate_range("2026-04-01", periods=130)
    close = np.full(len(days), 10000.0)
    frame = pd.DataFrame({"Date": days, "Open": close, "High": close * 1.01,
                          "Low": close * .99, "Close": close,
                          "Volume": np.full(len(days), 100000.0)})
    path = data / "000001.csv"
    frame.to_csv(path, index=False)
    now = datetime.combine(days[-1].date(), datetime.min.time(), ZoneInfo("Asia/Seoul"))
    now = now.replace(hour=18)
    manifest = {"fetched_at": now.isoformat(), "succeeded": 1, "failed": [],
                "primary_codes": ["000001"],
                "files": [{"code": "000001", "symbol": "000001.KS", "last": str(days[-1].date()),
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return data, manifest_path, now, tmp_path / "runs"


def test_capture_reuses_frozen_signal_on_same_prices_and_new_fetch_time(source):
    data, manifest_path, now, runs = source
    first = capture(data, manifest_path, runs, now=now)
    assert set(first["record"]["strategies"]) == {"early_inception", "early_contraction"}
    assert all(not block["candidates"] for block in first["record"]["strategies"].values())
    original = (runs / f'{first["record"]["as_of"]}.json').read_bytes()
    manifest = json.loads(manifest_path.read_text())
    manifest["fetched_at"] = (now + timedelta(hours=1)).isoformat()
    manifest_path.write_text(json.dumps(manifest))
    second = capture(data, manifest_path, runs, now=now + timedelta(hours=1))
    assert second["reused"] is True
    assert (runs / f'{first["record"]["as_of"]}.json').read_bytes() == original


def test_stale_or_changed_source_cannot_create_signal(source):
    data, manifest_path, now, runs = source
    with pytest.raises(ValueError, match="PRICE_SNAPSHOT_STALE_OR_FUTURE"):
        capture(data, manifest_path, runs, now=now + timedelta(days=5))
    path = data / "000001.csv"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="SOURCE_HASH_MISMATCH"):
        capture(data, manifest_path, runs, now=now)
    assert not runs.exists()


def test_before_close_and_partial_fetch_are_rejected(source):
    data, manifest_path, now, _ = source
    manifest = json.loads(manifest_path.read_text())
    manifest["fetched_at"] = now.replace(hour=15).isoformat()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="SAME_DAY_CLOSE_UNVERIFIED"):
        inspect(data, manifest_path, now=now)
    manifest["failed"] = [{"code": "000002"}]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="SOURCE_FETCH_PARTIAL"):
        inspect(data, manifest_path, now=now)


def test_partial_market_universe_does_not_look_like_a_complete_scan(source):
    data, manifest_path, now, _ = source
    manifest = json.loads(manifest_path.read_text())
    manifest["task"] = "T-063"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="PRIMARY_UNIVERSE_TOO_SMALL"):
        inspect(data, manifest_path, now=now)


def test_sparse_breadth_denominator_cannot_seal_an_empty_decision(source):
    data, manifest_path, now, _ = source
    finding = inspect(data, manifest_path, now=now)
    complete = finding["universe"]["000001"]
    # Ten valid latest closes, but nine lack one recent session after alignment.
    finding["universe"] = {str(i): (complete.copy() if i == 0 else
                                  complete.drop(complete.index[-2])) for i in range(10)}
    finding["fresh_codes"] = list(finding["universe"])
    with pytest.raises(ValueError, match="MARKET_BREADTH_COVERAGE_INSUFFICIENT"):
        build_record(finding, manifest_path)


def test_outcomes_stay_pending_until_full_horizon(source):
    data, manifest_path, now, runs = source
    first = capture(data, manifest_path, runs, now=now)
    record = first["record"]
    record["strategies"]["early_contraction"]["candidates"] = [
        {"code": "000001", "market": "KOSPI", "close": 10000.0}]
    path = runs / f'{record["as_of"]}.json'
    path.write_text(json.dumps(record), encoding="utf-8")
    finding = inspect(data, manifest_path, now=now)
    result = resolve(runs, finding["universe"], finding["as_of"])
    assert result["pending_count"] == 1
    assert result["resolved"] == []


def test_two_rankings_are_sealed_separately(source, monkeypatch):
    data, manifest_path, now, _ = source
    finding = inspect(data, manifest_path, now=now)
    finding["name_by_code"] = {"000001": "테스트종목"}
    day = pd.Timestamp(finding["as_of"])
    monkeypatch.setattr(forward, "calculate_market_breadth",
                        lambda universe: pd.Series({day: 60.0}))
    monkeypatch.setattr(forward, "compute_early_contraction_rankings",
                        lambda universe, as_of: pd.DataFrame([{"code": "000001", "close": 10000,
                            "contraction_score": .9, "cmf20": .1}]))
    monkeypatch.setattr(forward, "compute_early_inception_rankings",
                        lambda universe, as_of: pd.DataFrame([{"code": "000001", "close": 10000,
                            "inception_score": .7, "cmf20": .1}]))
    record = build_record(finding, manifest_path)
    assert record["strategies"]["early_contraction"]["candidates"][0]["contraction_score"] == .9
    assert record["strategies"]["early_inception"]["candidates"][0]["inception_score"] == .7
    assert record["strategies"]["early_contraction"]["candidates"][0]["market"] == "KOSPI"
    assert record["strategies"]["early_contraction"]["candidates"][0]["name"] == "테스트종목"


def test_source_universe_names_are_read_only_from_hashed_snapshot(source, monkeypatch, tmp_path):
    data, manifest_path, now, _ = source
    snapshot_dir = tmp_path / "quant-research" / "data" / "vcp_snapshots"
    snapshot_dir.mkdir(parents=True)
    snapshot = snapshot_dir / "universe_test.csv"
    snapshot.write_text("code,name,market\n000001,테스트종목,KOSPI\n", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["universe_file"] = snapshot.name
    manifest["universe_sha256"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(forward, "ROOT", tmp_path)
    assert inspect(data, manifest_path, now=now)["name_by_code"] == {"000001": "테스트종목"}
    snapshot.write_text("code,name,market\n000001,다른종목,KOSPI\n", encoding="utf-8")
    with pytest.raises(ValueError, match="UNIVERSE_HASH_MISMATCH"):
        inspect(data, manifest_path, now=now)


def test_mode2_outcome_waits_for_and_uses_next_20_sessions(source):
    data, manifest_path, now, runs = source
    finding = inspect(data, manifest_path, now=now)
    frame = finding["universe"]["000001"]
    signal_day = frame.index[-21]
    runs.mkdir()
    record = {"schema_version": 2, "as_of": str(signal_day.date()), "run_id": "test",
              "strategies": {"mode2": {"candidates": [{"code": "000001", "market": "KOSPI"}]},
                             "early_inception": {"candidates": []}}}
    (runs / f'{signal_day.date()}.json').write_text(json.dumps(record))
    result = resolve(runs, finding["universe"], finding["as_of"])
    assert result["pending_count"] == 0
    assert len(result["resolved"]) == 1
    trade = result["resolved"][0]
    assert trade["strategy"] == "mode2"
    assert trade["entry_date"] == str(frame.index[-20].date())
    assert trade["exit_date"] == str(frame.index[-1].date())


def test_unresolved_candidate_is_kept_when_daily_universe_rotates(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    record = {"as_of": "2026-09-23", "strategies": {
        "mode2": {"candidates": [{"code": "192820", "market": "KOSPI"}]},
        "early_inception": {"candidates": []}}}
    (runs / "2026-09-23.json").write_text(json.dumps(record), encoding="utf-8")
    today = pd.DataFrame([{"code": "050890", "market": "KOSDAQ"}])
    extended, carried = include_unresolved(today, runs)
    assert carried == ["192820"]
    assert extended["code"].tolist() == ["050890", "192820"]
    (tmp_path / "outcomes.json").write_text(json.dumps({"resolved": [
        {"signal_date": "2026-09-23", "strategy": "mode2", "code": "192820"}]}))
    extended, carried = include_unresolved(today, runs)
    assert carried == []
    assert extended["code"].tolist() == ["050890"]


def test_readme_update_keeps_other_dashboard_sections(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("header\n<!-- DUAL_FORWARD:START -->\nold\n<!-- DUAL_FORWARD:END -->\n"
                    "<!-- QUANT_DASHBOARD:START -->\ncollector\n<!-- QUANT_DASHBOARD:END -->\n",
                    encoding="utf-8")
    update_readme_panel(path, "new dual report")
    content = path.read_text(encoding="utf-8")
    assert "new dual report" in content and "old" not in content
    assert "collector" in content
    path.write_text("no markers", encoding="utf-8")
    with pytest.raises(ValueError, match="README_PANEL_MARKERS"):
        update_readme_panel(path, "x")


def test_consecutive_observation_days_uses_sealed_market_sessions(tmp_path):
    sessions = pd.to_datetime(["2026-09-25", "2026-09-28", "2026-09-29"])
    universe = {"A": pd.DataFrame(index=sessions)}
    prior = lambda code: {"strategies": {"mode2": {"candidates": [{"code": code}]},
                                            "early_inception": {"candidates": []}}}
    (tmp_path / "2026-09-25.json").write_text(json.dumps(prior("A")))
    (tmp_path / "2026-09-28.json").write_text(json.dumps(prior("A")))
    current = {"as_of": "2026-09-29", "strategies": {
        "mode2": {"candidates": [{"code": "A"}]},
        "early_inception": {"candidates": [{"code": "A"}]}}}
    counts = consecutive_observation_days(current, tmp_path, universe)
    assert counts == {("mode2", "A"): 3, ("early_inception", "A"): 1}
    (tmp_path / "2026-09-28.json").unlink()
    assert consecutive_observation_days(current, tmp_path, universe)[("mode2", "A")] == 1


def test_main_renders_stock_details_for_both_strategies(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    readme = tmp_path / "README.md"
    readme.write_text("<!-- DUAL_FORWARD:START -->\nold\n<!-- DUAL_FORWARD:END -->\n",
                      encoding="utf-8")
    record = {
        "as_of": "2026-09-25", "fetched_at": "2026-09-25T09:00:00+00:00",
        "fresh_count": 150, "universe_count": 150, "market_breadth_pct": 55.0,
        "strategies": {
            "mode2": {"score_field": "composite_score", "candidates": [{
                "code": "000001", "name": "모드종목", "close": 10000,
                "composite_score": .8, "mom60_5": .2, "risk_adj_mom": .7, "cmf20": .1}]},
            "early_inception": {"score_field": "inception_score", "candidates": [{
                "code": "000002", "name": "초입종목", "close": 20000,
                "prior_h20": 19800, "breakout_ext_pct": 1.0, "stop_price_5pct": 19000, "sma10": 19500,
                "inception_score": .7, "contraction_ratio": .52, "range_width_ratio": .18,
                "vol_ratio": 1.5, "vol_spike_1d": 2.0, "cmf20": .2}]},
        },
    }
    monkeypatch.setattr(forward, "capture", lambda *args: {
        "finding": {"universe": {}, "as_of": record["as_of"]},
        "record": record, "reused": False})
    monkeypatch.setattr(forward, "resolve", lambda *args: {"resolved": [], "pending_count": 0})
    monkeypatch.setattr(forward.sys, "argv", ["forward", "--manifest", str(tmp_path / "x"),
                                           "--runs", str(runs), "--readme", str(readme)])
    assert forward.main() == 0
    panel = readme.read_text(encoding="utf-8")
    assert panel.index("### 🌐 시장 상황") < panel.index("### 모드 2 · 과거 봉인 기록") < panel.index("### 상승 초입 · 20일 돌파 확정")
    assert "🟢 관찰 가능 · 시장 폭(SMA60) **55.0%** · 기준 완료 일봉 `2026-09-25`" in panel
    assert panel.count("갱신: `2026-09-25 18:00 KST`") == 2
    assert "모드종목 (`000001`) | 1거래일째 | 10,000원 | - | -5% 9,500원 | 0.800 |" in panel
    assert "19,800원 (+1.0%) | -5% 19,000원 · SMA10 19,500원 | 0.700 |" in panel
    assert "수축비 0.52 (변동폭 18.0%) · 5일 1.50x (당일 2.00x) · CMF20 +0.20" in panel


def test_backfill_missing_recovers_unsealed_interim_sessions(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    days = pd.bdate_range("2026-04-01", periods=130)
    close = np.full(len(days), 10000.0)
    frame = pd.DataFrame({"Date": days, "Open": close, "High": close * 1.01,
                          "Low": close * .99, "Close": close,
                          "Volume": np.full(len(days), 100000.0),
                          "AdjClose": close}).set_index("Date")
    universe = {"000001": frame}
    finding = {
        "as_of": str(days[-1].date()),
        "primary_codes": ["000001"],
        "fresh_codes": ["000001"],
        "universe": universe,
        "hashes": {"000001.csv": "abc"},
        "rejected": {"missing_ohlcv": 0, "invalid_ohlcv": 0},
        "fetched_at": f"{days[-1].date()}T18:00:00+09:00",
        "symbol_by_code": {"000001": "000001.KS"},
        "name_by_code": {"000001": "테스트"},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"primary_codes": ["000001"]}))

    missing_day = str(days[-2].date())
    prior_day = str(days[-3].date())
    (runs / f"{prior_day}.json").write_text(json.dumps({
        "as_of": prior_day,
        "source": {"manifest_sha256": "fake_sha"},
        "strategies": {"mode2": {"candidates": []}, "early_inception": {"candidates": []}},
    }))

    recovered = forward.backfill_missing(finding, manifest_path, runs, lookback=5)
    assert recovered == [missing_day]
    assert (runs / f"{missing_day}.json").exists()
    sealed = json.loads((runs / f"{missing_day}.json").read_text(encoding="utf-8"))
    assert sealed["as_of"] == missing_day
    assert sealed["backfill"]["recovered_from_as_of"] == str(days[-1].date())


@pytest.fixture
def panel_record():
    basic = {"code": "000001", "name": "돌파종목", "close": 10000,
             "prior_h20": 9900, "breakout_ext_pct": 1.01, "stop_price_5pct": 9500, "sma10": 9750,
             "inception_score": .7, "contraction_ratio": .55, "range_width_ratio": .18,
             "vol_ratio": 1.5, "vol_spike_1d": 2.0, "cmf20": .2}
    contraction = {"code": "000003", "name": "수축종목", "close": 410000,
                   "prior_h20": 419000, "breakout_ext_pct": -2.15, "stop_price_5pct": 389500, "sma10": 404950,
                   "contraction_score": .48, "contraction_ratio": .26, "range_width_ratio": .062,
                   "vol_ratio": 1.49, "vol_spike_1d": 2.06, "cmf20": .01}
    return {"as_of": "2026-10-08", "fetched_at": "2026-10-08T18:00:00+09:00",
            "market_breadth_pct": 55.0, "strategies": {
                "early_inception": {"score_field": "inception_score", "candidates": [
                    basic, {**basic, "code": "000002", "name": "기본종목"}]},
                "early_contraction": {"score_field": "contraction_score", "candidates": [contraction]}}}


def test_panel_renders_final_early_inception_and_retires_3d(panel_record):
    before = json.dumps(panel_record, ensure_ascii=False)
    panel = forward.render_panel(panel_record, {("early_inception", "000001"): 3})
    rows = [line for line in panel.splitlines() if line.startswith("| 1 |") or line.startswith("| 2 |")]
    assert "3거래일째" in rows[0]
    assert "9,900원 (+1.0%)" in rows[0]
    assert "-5% 9,500원 · SMA10 9,750원" in rows[0]
    assert "수축종목 (`000003`)" in rows[2]
    assert "419,000원 (-2.1%)" in rows[2]
    assert "-5% 389,500원 · SMA10 404,950원" in rows[2]
    assert panel.index("### 🧭 최종 상승 초입 관찰 안내") < panel.index("### 상승 초입 · 20일 돌파 확정")
    assert panel.index("### 상승 초입 · 20일 돌파 확정") < panel.index("### 상승 초입 · 20일 수축 돌파임박")
    assert "### 상승초입 3일 이내" not in panel
    assert "🎯 3일 목록 포함" not in panel
    assert all(line.count("|") == 9 for line in rows)
    assert json.dumps(panel_record, ensure_ascii=False) == before


def test_panel_empty_candidates_and_market_gate(panel_record):
    panel_record["market_breadth_pct"] = 30
    for block in panel_record["strategies"].values():
        block["candidates"] = []
    panel = forward.render_panel(panel_record, {})
    assert "🔴 후보 선별 중단" in panel
    assert panel.count("| - | 조건 충족 없음 | - | - | - | - | - | - |") == 2
    assert "### 🧭 최종 상승 초입 관찰 안내" in panel
    assert "### 상승초입 3일 이내" not in panel


def test_panel_legacy_mode2_and_retired_3d_records_render_cleanly(panel_record):
    strategies = panel_record["strategies"]
    strategies.pop("early_contraction")
    strategies["early_inception_3d"] = {"score_field": "inception_3d_score", "candidates": [{
        "code": "000099", "name": "폐기된3일종목", "close": 10000, "inception_3d_score": .9}]}
    panel_3d = forward.render_panel(panel_record, {})
    assert "### 상승초입 3일 이내" not in panel_3d
    assert "폐기된3일종목" not in panel_3d
    assert "### 상승 초입 · 20일 수축 돌파임박" in panel_3d

    strategies.pop("early_inception_3d")
    strategies["mode2"] = {"score_field": "composite_score", "candidates": [{
        "code": "000001", "name": "과거종목", "close": 10000,
        "composite_score": .8, "mom60_5": .2, "risk_adj_mom": .7, "cmf20": .1}]}
    panel = forward.render_panel(panel_record, {})
    assert "### 모드 2 · 과거 봉인 기록" in panel
    assert "60일 모멘텀 +20.0%" in panel
    assert "### 상승초입 3일 이내" not in panel


def test_main_reused_record_refreshes_panel_without_rewriting_seal(tmp_path, monkeypatch, panel_record, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    seal = runs / "2026-10-08.json"
    seal.write_text(json.dumps(panel_record), encoding="utf-8")
    sealed_bytes = seal.read_bytes()
    readme = tmp_path / "README.md"
    prefix = "header\n<!-- DUAL_FORWARD:START -->"
    suffix = "<!-- DUAL_FORWARD:END -->\n<!-- QUANT_DASHBOARD:START -->collector content"
    readme.write_text(prefix + "\nold\n" + suffix, encoding="utf-8")
    def changed_capture(*args):
        raise ValueError("SIGNAL_DATE_ALREADY_SEALED_WITH_DIFFERENT_INPUT")
    monkeypatch.setattr(forward, "capture", changed_capture)
    monkeypatch.setattr(forward, "inspect", lambda *args: {"as_of": "2026-10-08", "universe": {}})
    monkeypatch.setattr(forward, "backfill_missing", lambda *args: [])
    monkeypatch.setattr(forward, "resolve", lambda *args: {"resolved": [], "pending_count": 3})
    monkeypatch.setattr(forward.sys, "argv", ["forward", "--manifest", str(tmp_path / "manifest"),
                                           "--runs", str(runs), "--readme", str(readme)])
    assert forward.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "REUSED"
    panel = readme.read_text(encoding="utf-8")
    assert panel.startswith(prefix) and panel.endswith(suffix)
    assert "🧭 최종 상승 초입 관찰 안내" in panel and "수축종목 (`000003`)" in panel
    assert seal.read_bytes() == sealed_bytes
    assert (runs.parent / "latest.md").read_text(encoding="utf-8") in panel


def test_to_frame_strips_unclosed_same_day_bar_before_16_kst():
    from scripts.fetch_daily_history import to_frame
    ts_prev = int(datetime(2026, 10, 15, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp())
    ts_curr = int(datetime(2026, 10, 16, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp())
    item = {
        "timestamp": [ts_prev, ts_curr],
        "indicators": {
            "quote": [{
                "open": [10000.0, 10100.0],
                "high": [10200.0, 10100.0],
                "low": [9900.0, 10100.0],
                "close": [10050.0, 10100.0],
                "volume": [100000, 0],
            }],
            "adjclose": [{"adjclose": [10050.0, 10100.0]}],
        },
    }
    fetched_early = datetime(2026, 10, 16, 3, 20, tzinfo=ZoneInfo("Asia/Seoul"))
    df_early = to_frame(item, fetched_at=fetched_early)
    assert len(df_early) == 1
    assert str(df_early.index[-1].date()) == "2026-10-15"

    fetched_after_close = datetime(2026, 10, 16, 18, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    df_after = to_frame(item, fetched_at=fetched_after_close)
    assert len(df_after) == 2
    assert str(df_after.index[-1].date()) == "2026-10-16"


def test_inspect_allows_short_history_ipos_in_primary_codes(source):
    data, manifest_path, now, _ = source
    short_days = pd.bdate_range("2026-08-01", periods=30)
    short_close = np.full(len(short_days), 5000.0)
    short_df = pd.DataFrame({
        "Date": short_days, "Open": short_close, "High": short_close * 1.01,
        "Low": short_close * 0.99, "Close": short_close, "Volume": np.full(len(short_days), 50000.0)
    })
    short_path = data / "000002.csv"
    short_df.to_csv(short_path, index=False)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["succeeded"] = 2
    manifest["primary_codes"] = ["000001", "000002"]
    manifest["files"].append({
        "code": "000002", "symbol": "000002.KQ",
        "last": str(short_days[-1].date()),
        "sha256": hashlib.sha256(short_path.read_bytes()).hexdigest(),
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # Even though 000002 has <125 rows (50% of primary_codes), 000001 (100% of eligible_primary) is fresh.
    finding = inspect(data, manifest_path, now=now)
    assert finding["fresh_codes"] == ["000001"]


def test_non_destructive_correction_overlay_is_used_without_modifying_seal(source):
    data, manifest_path, now, runs = source
    first = capture(data, manifest_path, runs, now=now)
    as_of = first["record"]["as_of"]
    seal_path = runs / f"{as_of}.json"
    orig_bytes = seal_path.read_bytes()

    corr_dir = runs / "corrections"
    corr_dir.mkdir(parents=True, exist_ok=True)
    corrected = json.loads(orig_bytes.decode("utf-8"))
    corrected["market_breadth_pct"] = 65.97
    corrected["strategies"]["early_inception"]["candidates"] = [
        {"code": "000001", "name": "보정종목", "market": "KOSPI", "close": 10000.0}
    ]
    (corr_dir / f"{as_of}.json").write_text(json.dumps(corrected), encoding="utf-8")

    eff = forward.load_effective_record(runs, as_of)
    assert eff["market_breadth_pct"] == 65.97
    assert len(eff["strategies"]["early_inception"]["candidates"]) == 1
    assert seal_path.read_bytes() == orig_bytes

    finding = inspect(data, manifest_path, now=now)
    res = resolve(runs, finding["universe"], finding["as_of"])
    assert res["pending_count"] == 1

    today_univ = pd.DataFrame([{"code": "999999", "market": "KOSPI"}])
    _, carried = include_unresolved(today_univ, runs)
    assert carried == ["000001"]


def test_regime_context_detects_weakening_and_ignores_future_records(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    sessions = [
        ("2026-10-01", 89.2),
        ("2026-10-02", 88.7),
        ("2026-10-06", 87.9),
        ("2026-10-07", 85.9),
        ("2026-10-08", 80.9),
        ("2026-10-12", 95.0),  # Future relative to 10-07 / 10-08
    ]
    for day, b in sessions:
        (runs / f"{day}.json").write_text(
            json.dumps({"as_of": day, "market_breadth_pct": b}), encoding="utf-8"
        )

    # On 2026-10-07: 3 consecutive drops (89.2 -> 88.7 -> 87.9 -> 85.9) triggers WEAKENING
    ctx_07 = forward.compute_regime_context({"as_of": "2026-10-07", "market_breadth_pct": 85.9}, runs)
    assert ctx_07 is not None
    assert ctx_07["status"] == "WEAKENING"
    assert "🟡 레짐 둔화 주의" in ctx_07["badge"]
    assert ctx_07["consecutive_drops"] == 3
    assert [item["date"] for item in ctx_07["history"]] == [
        "2026-10-01", "2026-10-02", "2026-10-06", "2026-10-07"
    ]

    # On 2026-10-08: drawdown from peak is -8.3%p, ignores 2026-10-12
    ctx_08 = forward.compute_regime_context({"as_of": "2026-10-08", "market_breadth_pct": 80.9}, runs)
    assert ctx_08["status"] == "WEAKENING"
    assert ctx_08["drawdown_from_peak_pct"] == pytest.approx(-8.3)
    assert all(item["date"] <= "2026-10-08" for item in ctx_08["history"])

    # Stable/rising regime
    (runs / "2026-10-07.json").write_text(json.dumps({"as_of": "2026-10-07", "market_breadth_pct": 88.5}))
    (runs / "2026-10-08.json").write_text(json.dumps({"as_of": "2026-10-08", "market_breadth_pct": 89.5}))
    ctx_stable = forward.compute_regime_context({"as_of": "2026-10-08", "market_breadth_pct": 89.5}, runs)
    assert ctx_stable["status"] == "STABLE"
    assert "🟢 레짐 안정/확장" in ctx_stable["badge"]


def test_gate_funnel_monotonicity_and_prefix_invariance():
    from research.early_inception_engine import (
        compute_early_contraction_rankings,
        compute_early_inception_rankings,
    )
    days = pd.bdate_range("2026-03-01", periods=140)
    eval_date = days[-5]

    # Stock 1: passes all basic breakout gates on eval_date, then crashes in future bars
    c1 = np.full(len(days), 10000.0)
    h1 = np.full(len(days), 10200.0)
    l1 = np.full(len(days), 9900.0)
    v1 = np.full(len(days), 100000.0)
    idx = list(days).index(eval_date)
    # Tighten 20-day range relative to 60-day range
    h1[idx - 55 : idx - 25] = 11000.0
    l1[idx - 55 : idx - 25] = 9000.0
    c1[idx - 20 : idx] = 10050.0
    h1[idx - 20 : idx] = 10100.0
    l1[idx - 20 : idx] = 9950.0
    c1[idx] = 10150.0
    h1[idx] = 10160.0
    l1[idx] = 10000.0
    v1[idx] = 250000.0
    # Future crash after eval_date (must be ignored by prefix slice)
    c1[idx + 1 :] = 5000.0

    # Stock 2: tight contraction within -3% of 20d high (10100) with volume ignition and Close >= SMA20
    c2 = c1.copy()
    h2 = h1.copy()
    l2 = l1.copy()
    v2 = v1.copy()
    c2[idx] = 10080.0
    h2[idx] = 10090.0
    l2[idx] = 9950.0

    # Stock 3: below SMA20/SMA60 (downtrend)
    c3 = np.linspace(15000.0, 8000.0, len(days))
    h3 = c3 * 1.01
    l3 = c3 * 0.99
    v3 = np.full(len(days), 100000.0)

    def make_df(c, h, l, v):
        return pd.DataFrame(
            {"Open": c * 0.995, "High": h, "Low": l, "Close": c, "Volume": v},
            index=days,
        )

    full_univ = {
        "000001": make_df(c1, h1, l1, v1),
        "000002": make_df(c2, h2, l2, v2),
        "000003": make_df(c3, h3, l3, v3),
    }
    prefix_univ = {k: df.loc[:eval_date].copy() for k, df in full_univ.items()}

    f_full = forward.compute_gate_funnel(full_univ, eval_date)
    f_prefix = forward.compute_gate_funnel(prefix_univ, eval_date)
    assert f_full == f_prefix
    assert f_full["eligible"] == 3
    assert (
        f_full["eligible"]
        >= f_full["trend_liquidity"]
        >= f_full["not_overextended"]
        >= f_full["contraction"]
        >= f_full["breakout_20d"]
        >= f_full["final_basic"]
        >= 1
    )
    assert f_full["final_basic"] == len(compute_early_inception_rankings(prefix_univ, eval_date))
    assert f_full["final_contraction"] == len(compute_early_contraction_rankings(prefix_univ, eval_date))
    assert f_full["final_contraction"] == 1

    f_halted = forward.compute_gate_funnel(prefix_univ, eval_date, market_breadth_pct=30.0)
    assert f_halted["breakout_20d"] == f_prefix["breakout_20d"]
    assert f_halted["final_basic"] == 0 and f_halted["final_contraction"] == 0


def test_render_panel_includes_regime_and_gate_funnel_and_survives_heartbeat(panel_record, tmp_path):
    from scripts.refresh_forward_status import refresh
    regime_ctx = {
        "status": "WEAKENING",
        "badge": "🟡 레짐 둔화 주의",
        "history": [
            {"date": "2026-10-06", "breadth_pct": 87.9},
            {"date": "2026-10-07", "breadth_pct": 85.9},
            {"date": "2026-10-08", "breadth_pct": 55.0},
        ],
        "delta_pct": -32.9,
        "drawdown_from_peak_pct": -32.9,
        "consecutive_drops": 2,
    }
    funnel = {
        "eligible": 144,
        "trend_liquidity": 53,
        "not_overextended": 7,
        "contraction": 4,
        "breakout_20d": 0,
        "final_basic": 0,
        "final_contraction": 1,
    }
    panel = forward.render_panel(panel_record, {}, regime_context=regime_ctx, gate_funnel=funnel)
    assert "🟡 레짐 둔화 주의 · **최근 3거래일 추이:** 87.9% (`10-06`) → 85.9% (`10-07`) → 55.0% (`10-08`)" in panel
    assert "**게이트별 생존 (`144`종목 평가):** 추세·대금 `53` → 비과열 `7` → 20일 수축 `4` → **최종 통과: 돌파 확정 `0`건 (고점돌파 `0`) / 수축 돌파임박 `1`건**" in panel

    readme = tmp_path / "README.md"
    latest = tmp_path / "latest.md"
    readme.write_text(f"<!-- DUAL_FORWARD:START -->\n{panel}\n<!-- DUAL_FORWARD:END -->\n", encoding="utf-8")
    refresh(readme, latest, "CAPTURED", now=datetime(2026, 10, 8, 20, 10, tzinfo=ZoneInfo("Asia/Seoul")))
    refreshed = readme.read_text(encoding="utf-8")
    assert "🟡 레짐 둔화 주의" in refreshed
    assert "게이트별 생존 (`144`종목 평가)" in refreshed





def test_to_frame_strips_trailing_partial_nan_close_row_during_midnight_settlement():
    from scripts.fetch_daily_history import to_frame
    ts_prev = int(datetime(2026, 10, 22, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp())
    ts_curr = int(datetime(2026, 10, 23, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp())
    item = {
        "timestamp": [ts_prev, ts_curr],
        "indicators": {
            "quote": [{
                "open": [10000.0, 10100.0],
                "high": [10200.0, 10300.0],
                "low": [9900.0, 10000.0],
                "close": [10050.0, None],
                "volume": [100000, 120000],
            }],
            "adjclose": [{"adjclose": [10050.0, None]}],
        },
    }
    fetched_night = datetime(2026, 10, 24, 1, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    df_night = to_frame(item, fetched_at=fetched_night)
    assert len(df_night) == 1
    assert str(df_night.index[-1].date()) == "2026-10-22"
    assert int(df_night["Close"].isna().sum()) == 0
