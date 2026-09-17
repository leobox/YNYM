import importlib.util
import json
from pathlib import Path

import pandas as pd

from scanner.manifest import save_snapshot_run
from tools.sync_check import main as sync_check
from tools.verify_data import verify_data

ROOT = Path(__file__).resolve().parents[1]


def test_empty_candidate_tables_are_still_recorded(tmp_path):
    manifest_path = save_snapshot_run(
        "20260917_120000", tmp_path,
        {"universe": pd.DataFrame([{"code": "000001"}]),
         "scores": pd.DataFrame(), "top5": pd.DataFrame(), "watch": pd.DataFrame()},
        {"top_n": 5}, {"timestamp": "2026-09-17T12:00:00+09:00", "errors": 0})
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {
        "20260917_120000_universe.csv", "20260917_120000_scores.csv",
        "20260917_120000_top5.csv", "20260917_120000_watch.csv"}
    assert manifest["files"]["20260917_120000_top5.csv"]["rows"] == 0
    assert all((tmp_path / name).is_file() for name in manifest["files"])


def test_standalone_colab_has_no_project_imports_and_saves_manifest(tmp_path):
    path = ROOT / "notebooks" / "pattern_colab.py"
    source = path.read_text(encoding="utf-8")
    assert "from scanner" not in source
    assert "from research" not in source
    spec = importlib.util.spec_from_file_location("pattern_colab_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = pd.Timestamp("2026-09-17 12:00", tz="Asia/Seoul")
    manifest_path = module.save_snapshot_run(
        tmp_path, "20260917_120000", now, [], [], pd.DataFrame(), pd.DataFrame(), [])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["code"]["release_id"] == module.RELEASE_ID
    assert len(manifest["files"]) == 4


def test_generated_notebook_and_core_functions_are_synchronized():
    assert sync_check() == 0


def test_imported_hourly_data_integrity():
    summary, issues = verify_data(ROOT / "data" / "imported" / "hourly_pattern")
    assert issues == []
    assert summary["files"] == 215
    assert summary["rows"] == 75647
