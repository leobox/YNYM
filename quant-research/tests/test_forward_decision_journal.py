"""T-096: immutable forward observations and freshness gates."""

import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.forward_decision_journal import capture, inspect
import research.forward_decision_journal as journal
from scripts.pure_quant_portfolio_manager import PortfolioManager


@pytest.fixture
def journal_inputs(tmp_path):
    dates = pd.bdate_range("2026-01-01", periods=150)
    close = np.linspace(10000, 16000, len(dates))
    prices = pd.DataFrame({"Date": dates, "Open": close, "High": close * 1.01,
                           "Low": close * .99, "Close": close,
                           "Volume": np.full(len(dates), 100000)})
    data_dir = tmp_path / "daily"
    data_dir.mkdir()
    prices.to_csv(data_dir / "000001.csv", index=False)
    eps_path = tmp_path / "eps.json"
    eps_path.write_text(json.dumps({"000001": {
        "fetched_at": str((dates[-1] - pd.Timedelta(days=2)).date()) + "T12:00:00",
        "quarters": [{"period": "202503", "eps": 100, "is_consensus": False},
                     {"period": "202603", "eps": 120, "is_consensus": False}],
        "disclosures": [{"datetime": "2026-05-15T16:00:00", "title": "분기보고서"}],
    }}), encoding="utf-8")
    manager = PortfolioManager(state_file=str(tmp_path / "state.json"), data_dir=str(data_dir),
                               kb_path=tmp_path / "missing_kb.json")
    today = dates[-1].date() + timedelta(days=1)
    return manager, eps_path, today, tmp_path / "runs"


def test_identical_capture_is_immutable_and_does_not_mutate_portfolio(journal_inputs):
    manager, eps_path, today, runs = journal_inputs
    first = capture(manager, eps_path, runs, today=today)
    path = runs / f"{first['run_id']}.json"
    original = path.read_bytes()
    second = capture(manager, eps_path, runs, today=today)
    assert first["reused"] is False and second["reused"] is True
    assert path.read_bytes() == original
    record = json.loads(original)
    assert record["decision"]["sue_score_applied"] is False
    assert record["source_hashes"]["prices"]["000001.csv"]
    assert not manager.state_file.exists()


def test_source_change_creates_distinct_run(journal_inputs):
    manager, eps_path, today, runs = journal_inputs
    before = capture(manager, eps_path, runs, today=today)
    data = json.loads(eps_path.read_text(encoding="utf-8"))
    data["000001"]["quarters"][1]["eps"] = 125
    eps_path.write_text(json.dumps(data), encoding="utf-8")
    after = capture(manager, eps_path, runs, today=today)
    assert before["run_id"] != after["run_id"]
    assert len(list(runs.glob("*.json"))) == 2


def test_stale_or_future_price_cannot_be_sealed(journal_inputs):
    manager, eps_path, today, runs = journal_inputs
    old = inspect(manager, eps_path, today=today + timedelta(days=8))
    assert old["reasons"] == ["PRICE_SNAPSHOT_STALE"]
    with pytest.raises(ValueError, match="PRICE_SNAPSHOT_STALE"):
        capture(manager, eps_path, runs, today=today + timedelta(days=8))
    future = inspect(manager, eps_path, today=today - timedelta(days=2))
    assert "PRICE_DATE_IN_FUTURE" in future["reasons"]
    same_day = inspect(manager, eps_path, today=today - timedelta(days=1))
    assert "SAME_DAY_CLOSE_UNVERIFIED" in same_day["reasons"]
    assert not runs.exists()


def test_missing_source_csv_rejected_even_if_data_is_loaded(journal_inputs):
    manager, eps_path, today, runs = journal_inputs
    manager.load_data()
    (Path(manager.data_dir) / "000001.csv").unlink()
    finding = inspect(manager, eps_path, today=today)
    assert "PRICE_SOURCE_FILES_MISSING" in finding["reasons"]


def test_source_changed_while_decision_was_built_is_rejected(journal_inputs, monkeypatch):
    manager, eps_path, today, runs = journal_inputs
    original_explore = journal.explore

    def change_source_during_read(*args, **kwargs):
        result = original_explore(*args, **kwargs)
        eps_path.write_text(eps_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        return result

    monkeypatch.setattr(journal, "explore", change_source_during_read)
    with pytest.raises(ValueError, match="SOURCE_CHANGED_DURING_CAPTURE"):
        capture(manager, eps_path, runs, today=today)
    assert not runs.exists()
