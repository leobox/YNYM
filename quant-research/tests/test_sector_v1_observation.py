"""Offline unit tests for T-150 public Sector RS (`RS20`) observation & README panel."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

QUANT_RESEARCH_ROOT = Path(__file__).resolve().parents[1]
if str(QUANT_RESEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANT_RESEARCH_ROOT))

from research.sector_v1.public_observer import (  # noqa: E402
    KST,
    SECTOR_V1_END,
    SECTOR_V1_START,
    build_frozen_public_sector_mapping,
    evaluate_public_sector_snapshot,
    is_confirmed_weekly_rebalance_close,
    render_sector_v1_panel,
    update_sector_v1_readme,
)
from scripts.run_sector_v1_observation import main as sector_obs_main  # noqa: E402


def _synthetic_ohlcv(dates: pd.DatetimeIndex, mult: float = 1.001, base: float = 10_000.0) -> pd.DataFrame:
    close = base * (mult ** np.arange(len(dates)))
    return pd.DataFrame(
        {
            "Open": close * 0.999,
            "High": close * 1.008,
            "Low": close * 0.992,
            "Close": close,
            "Volume": np.full(len(dates), 300_000.0, dtype=float),
        },
        index=dates,
    )


def test_frozen_public_sector_mapping_meets_six_month_lock_and_regional_minimums() -> None:
    mapping = build_frozen_public_sector_mapping()
    mapping.validate_lock_period(require_six_month_lock=True)
    assert set(mapping.benchmarks.keys()) == {"KR", "US"}
    kr_reps = [s for (reg, _), s in mapping.representatives.items() if reg == "KR"]
    us_reps = [s for (reg, _), s in mapping.representatives.items() if reg == "US"]
    assert len(kr_reps) >= 5
    assert len(us_reps) >= 5
    # Distinct ETF codes across the entire frozen mapping
    all_codes = [s.code for s in mapping.representatives.values()] + [
        b.code for b in mapping.benchmarks.values()
    ]
    assert len(all_codes) == len(set(all_codes))


def test_weekly_rebalance_cadence_friday_holiday_and_midweek_distinction() -> None:
    # Regular Friday completed bar -> confirmed even on Friday 16:35 KST or Saturday
    assert is_confirmed_weekly_rebalance_close(
        "2026-11-06", datetime(2026, 11, 6, 16, 35, tzinfo=KST)
    )
    assert is_confirmed_weekly_rebalance_close(
        "2026-11-06", datetime(2026, 11, 7, 20, 0, tzinfo=KST)
    )
    # Mid-week Wednesday completed bar observed Wednesday evening -> NOT confirmed
    assert not is_confirmed_weekly_rebalance_close(
        "2026-11-04", datetime(2026, 11, 4, 18, 0, tzinfo=KST)
    )
    # Thursday completed bar when Friday is a KRX holiday, checked Friday 17:00 KST -> confirmed!
    assert is_confirmed_weekly_rebalance_close(
        "2026-11-05", datetime(2026, 11, 6, 17, 0, tzinfo=KST)
    )


def test_bull_bear_regime_gating_e1_band_and_readme_marker_preservation(tmp_path: Path) -> None:
    mapping = build_frozen_public_sector_mapping()
    dates = pd.bdate_range("2025-11-03", "2026-11-06", freq="B")  # Ends on Friday 2026-11-06
    assert dates[-1].weekday() == 4

    frames: dict[str, pd.DataFrame] = {}
    # KR benchmark is bearish (< SMA120), US benchmark is bullish (>= SMA120)
    frames["069500"] = _synthetic_ohlcv(dates, mult=0.9985, base=35_000.0)
    frames["360750"] = _synthetic_ohlcv(dates, mult=1.0005, base=18_000.0)

    for idx, ((region, _sector), spec) in enumerate(sorted(mapping.representatives.items())):
        if region == "KR":
            # Make KR 반도체 strongly outperform KR benchmark with positive 5d RS acceleration,
            # YET blocked from weekly_candidates_b because KR benchmark < SMA120!
            df = _synthetic_ohlcv(dates, mult=1.0012 if spec.code == "396500" else 0.9980, base=15_000.0 + idx * 100.0)
            if spec.code == "396500":
                boost = np.ones(len(dates))
                boost[-10:] = np.linspace(1.001, 1.015, 10)
                for col in ("Open", "High", "Low", "Close"):
                    df[col] = df[col] * boost
            frames[spec.code] = df
        else:
            # Make US 반도체 & 빅테크 outperform US benchmark with accelerating last 5d RS
            df = _synthetic_ohlcv(dates, mult=1.0004, base=20_000.0 + idx * 100.0)
            if spec.code in ("381180", "381170"):
                boost = np.ones(len(dates))
                boost[-10:] = np.linspace(1.002, 1.022 if spec.code == "381180" else 1.016, 10)
                for col in ("Open", "High", "Low", "Close"):
                    df[col] = df[col] * boost
            frames[spec.code] = df

    fetched_at = datetime(2026, 11, 6, 17, 0, tzinfo=KST)
    res = evaluate_public_sector_snapshot(
        raw_frames=frames,
        as_of="2026-11-06",
        fetched_at=fetched_at,
        mapping=mapping,
    )
    assert res["weekly_rebalance_confirmed"] is True
    assert res["region_status"]["KR"] == "HALTED_BEAR_REGIME"
    assert res["region_status"]["US"] == "ACTIVE"

    # KR 반도체 (`396500`) is still recorded in daily observations despite KR bear regime (§7),
    # while weekly actionable candidates ONLY come from `US`!
    kr_semi = next(r for r in res["observations"] if r["code"] == "396500")
    assert kr_semi["is_observed"] is True
    assert "지역 레짐 차단(<SMA120)" in kr_semi["gate_failure_reasons"]
    cand_codes = [c["code"] for c in res["weekly_candidates_b"]]
    assert "396500" not in cand_codes
    assert cand_codes[0] == "381180"
    assert res["weekly_candidates_b"][0]["e1_check_upper_inclusive"] > res["weekly_candidates_b"][0]["e1_check_lower_exclusive_sma20"]

    record = {
        "spec_task_id": "T-145",
        "spec_version": "1.0",
        "mapping_version": mapping.mapping_version,
        "run_id": "test-run-001",
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "requested_symbol_count": 22,
        "fresh_symbol_count": 22,
        "as_of": "2026-11-06",
        "status": "OK",
        "result": res,
    }
    snap_json = tmp_path / "snapshot.json"
    snap_json.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    readme = tmp_path / "README.md"
    original_stock = "<!-- DUAL_FORWARD:START -->\nSTOCK PANEL\n<!-- DUAL_FORWARD:END -->"
    original_etf = "<!-- ETF_OBSERVATION:START -->\nETF PANEL\n<!-- ETF_OBSERVATION:END -->"
    readme.write_text(f"{original_stock}\n\n{original_etf}\n", encoding="utf-8")

    rc = sector_obs_main(["--render-only", str(snap_json), "--readme", str(readme)])
    assert rc == 0

    updated = readme.read_text(encoding="utf-8")
    assert original_stock in updated
    assert original_etf in updated
    assert updated.count(SECTOR_V1_START) == 1
    assert updated.count(SECTOR_V1_END) == 1
    assert "TIGER 미국필라델피아반도체나스닥" in updated
    assert "분배금 재투자 총수익·과거 상폐 PIT 미검증" in updated

    # Re-rendering updates in place without duplicating markers
    update_sector_v1_readme(readme, render_sector_v1_panel({**record, "status": "STALE", "result": {}}))
    updated2 = readme.read_text(encoding="utf-8")
    assert updated2.count(SECTOR_V1_START) == 1
    assert "자료 확인 후 산출" in updated2
    assert original_stock in updated2
    assert original_etf in updated2


def test_etf_observation_cli_accepts_out_dir_alias(tmp_path: Path) -> None:
    from scripts.run_etf_observation import main as etf_main
    import sys as _sys

    snap = {
        "version": "etf_reference_v2",
        "run_id": "r1",
        "fetched_at": "2026-11-06T17:00:00+09:00",
        "selected_count": 100,
        "fresh_count": 100,
        "as_of": "2026-11-06",
        "status": "OK",
        "result": {
            "stages": {"breakout": [], "watch": []},
            "funnel": {"evaluated": 97, "trend_liquidity": 1, "nonextended": 1, "contraction": 0, "ignition": 0},
            "qualified_before_top5": {"breakout": 0, "watch": 0},
            "breadth_pct": 1.0,
            "breadth_denominator": 97,
        },
    }
    snap_path = tmp_path / "etf_snap.json"
    snap_path.write_text(json.dumps(snap), encoding="utf-8")
    readme_path = tmp_path / "README.md"
    readme_path.write_text("<!-- DUAL_FORWARD:START -->\nX\n<!-- DUAL_FORWARD:END -->\n", encoding="utf-8")

    old_argv = _sys.argv
    try:
        _sys.argv = [
            "run_etf_observation.py",
            "--out-dir",
            str(tmp_path / "etf_out"),
            "--render-only",
            str(snap_path),
            "--readme",
            str(readme_path),
        ]
        etf_main()
    finally:
        _sys.argv = old_argv

    assert "<!-- ETF_OBSERVATION:START -->" in readme_path.read_text(encoding="utf-8")
