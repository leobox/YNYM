"""Source freshness and immutable forward decisions for the research observer."""

import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from scripts.run_early_inception_forward import build_record, capture, inspect, resolve, update_readme_panel
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
    assert set(first["record"]["strategies"]) == {"mode2", "early_inception"}
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


def test_outcomes_stay_pending_until_full_horizon(source):
    data, manifest_path, now, runs = source
    first = capture(data, manifest_path, runs, now=now)
    record = first["record"]
    record["strategies"]["mode2"]["candidates"] = [
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
    day = pd.Timestamp(finding["as_of"])
    monkeypatch.setattr(forward, "calculate_market_breadth",
                        lambda universe: pd.Series({day: 60.0}))
    monkeypatch.setattr(forward, "compute_factor_rankings",
                        lambda universe, as_of: pd.DataFrame([{"code": "000001", "close": 10000,
                            "composite_score": .9, "cmf20": .1}]))
    monkeypatch.setattr(forward, "compute_early_inception_rankings",
                        lambda universe, as_of: pd.DataFrame([{"code": "000001", "close": 10000,
                            "inception_score": .7, "cmf20": .1}]))
    record = build_record(finding, manifest_path)
    assert record["strategies"]["mode2"]["candidates"][0]["composite_score"] == .9
    assert record["strategies"]["early_inception"]["candidates"][0]["inception_score"] == .7
    assert record["strategies"]["mode2"]["candidates"][0]["market"] == "KOSPI"


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
