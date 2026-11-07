"""Comprehensive unit & adversarial tests for T-145 v1.0 Frozen Sector RS Research (§§1-10).

All tests are 100% offline using deterministic synthetic data and temporary directories.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

QUANT_RESEARCH_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = QUANT_RESEARCH_ROOT.parent
if str(QUANT_RESEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANT_RESEARCH_ROOT))

from research.sector_v1 import verify_spec_freeze  # noqa: E402
from research.sector_v1.evaluator import (  # noqa: E402
    DEFAULT_RANDOM_POLICIES,
    FIXED_RESEARCH_SEED,
    classify_v1_verdict,
    compute_episode_metrics,
    moving_block_bootstrap_ci,
    replay_sealed_evaluation_run,
    run_full_v1_evaluation,
    seal_and_register_evaluation_run,
    seal_pre_run_attempt,
    summarize_portfolio_result,
)
from research.sector_v1.execution_e0 import (  # noqa: E402
    ClosedEpisode,
    compute_target_qty,
    run_e0_simulation,
    verify_accounting_conservation,
)
from research.sector_v1.journal_e1 import (  # noqa: E402
    E1JournalRecord,
    append_e1_journal_record,
    compute_e1_entry_check_band,
    is_e1_entry_price_in_band,
    load_e1_journal,
    reconcile_e1_position_ledger,
    summarize_e1_journal,
)
from research.sector_v1.models import (  # noqa: E402
    ConstituentItem,
    CostSchedule,
    DataProvenanceManifest,
    FrozenSectorMapping,
    PITConstituentTable,
    RegionalBenchmarkSpec,
    SectorETFSpec,
    TaxRuleEntry,
    TradingCalendar,
    clean_and_validate_bars,
    default_krx_research_cost_schedule,
    select_frozen_representatives,
    validate_research_inputs,
    verify_total_return_reconciliation,
)
from research.sector_v1.signals import (  # noqa: E402
    classify_rs_transition,
    evaluate_daily_snapshot,
    select_weekly_etf_candidates_b,
    select_weekly_stock_candidates_a_matched,
    select_weekly_stock_candidates_c,
)
from scripts.run_t145_sector_research import load_offline_bundle, main as cli_main  # noqa: E402


def _make_daily_bars(
    dates: pd.DatetimeIndex,
    drift: float = 0.001,
    vol: float = 0.008,
    start_price: float = 10_000.0,
    volume: float = 300_000.0,
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(dates)
    rets = drift + rng.normal(0.0, vol, size=n)
    rets[0] = 0.0
    close = start_price * np.cumprod(1.0 + rets)
    open_px = close * (1.0 + rng.normal(0.0, 0.002, size=n))
    high = np.maximum(open_px, close) * (1.0 + np.abs(rng.normal(0.004, 0.002, size=n)))
    low = np.minimum(open_px, close) * (1.0 - np.abs(rng.normal(0.004, 0.002, size=n)))
    df = pd.DataFrame(
        {
            "Open": open_px,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": np.full(n, volume, dtype=float),
        },
        index=dates,
    )
    return clean_and_validate_bars(df, code=f"SYN_{seed}")


def _build_multi_sector_fixture(
    n_sessions: int = 240,
    start_date: str = "2024-01-02",
) -> tuple[TradingCalendar, FrozenSectorMapping, dict[str, pd.DataFrame]]:
    all_dates = pd.bdate_range(start_date, periods=n_sessions + 20, freq="B")
    cal = TradingCalendar(all_dates)
    bar_dates = all_dates[:n_sessions]

    benchmarks = {
        "KR": RegionalBenchmarkSpec(
            region="KR", code="BM_KR", name="KR Broad Equity", currency="KRW", fx_hedged=False
        ),
        "US": RegionalBenchmarkSpec(
            region="US", code="BM_US", name="US Broad Equity Unhedged", currency="KRW", fx_hedged=False
        ),
    }

    bars: dict[str, pd.DataFrame] = {
        "BM_KR": _make_daily_bars(bar_dates, drift=0.0006, vol=0.005, start_price=30_000.0, seed=1),
        "BM_US": _make_daily_bars(bar_dates, drift=0.0007, vol=0.005, start_price=20_000.0, seed=2),
    }

    reps: dict[tuple[str, str], SectorETFSpec] = {}
    kr_drifts = [0.0022, 0.0019, 0.0016, 0.0004, 0.0002, -0.0002]
    us_drifts = [0.0024, 0.0018, 0.0014, 0.0005, 0.0001]

    for idx, dr in enumerate(kr_drifts, start=1):
        code = f"KR_SEC_{idx:02d}"
        sec = f"SECTOR_KR_{idx}"
        reps[("KR", sec)] = SectorETFSpec(
            code=code,
            name=f"KR Sector {idx} ETF",
            region="KR",
            sector=sec,
            underlying_index=f"KRX Sector Index {idx}",
            currency="KRW",
            fx_hedged=False,
            mapping_reviewed=True,
        )
        bars[code] = _make_daily_bars(
            bar_dates, drift=dr, vol=0.006, start_price=12_000.0 + idx * 500, seed=100 + idx
        )

    for idx, dr in enumerate(us_drifts, start=1):
        code = f"US_SEC_{idx:02d}"
        sec = f"SECTOR_US_{idx}"
        reps[("US", sec)] = SectorETFSpec(
            code=code,
            name=f"US Sector {idx} ETF",
            region="US",
            sector=sec,
            underlying_index=f"S&P Sector Index {idx}",
            currency="KRW",
            fx_hedged=False,
            mapping_reviewed=True,
        )
        bars[code] = _make_daily_bars(
            bar_dates, drift=dr, vol=0.006, start_price=15_000.0 + idx * 500, seed=200 + idx
        )

    freeze_dt = bar_dates[130]
    valid_from = bar_dates[130]
    valid_until = bar_dates[130] + pd.Timedelta(days=400)
    mapping = FrozenSectorMapping(
        mapping_version="v1.0-test",
        freeze_date=freeze_dt,
        valid_from=valid_from,
        valid_until=valid_until,
        benchmarks=benchmarks,
        representatives=reps,
        is_pit_verified=True,
    )
    mapping.validate_lock_period(require_six_month_lock=True)
    return cal, mapping, bars


def test_freeze_integrity_and_tamper_detection(tmp_path: Path) -> None:
    info = verify_spec_freeze(WORKSPACE_ROOT)
    assert info["task_id"] == "T-145"
    assert info["spec_version"] == "1.0"
    assert len(info["verified_files"]) == 5

    # Copy frozen tree to tmp_path and mutate 1 byte to confirm tamper rejection
    tasks_dir = tmp_path / "docs" / "tasks"
    tasks_dir.mkdir(parents=True)
    shutil.copy2(WORKSPACE_ROOT / "docs/tasks/T-145-freeze.json", tasks_dir / "T-145-freeze.json")
    for rel_path in info["verified_files"]:
        shutil.copy2(WORKSPACE_ROOT / rel_path, tmp_path / rel_path)

    spec_copy = tmp_path / "docs/tasks/T-145-spec-v1.md"
    spec_copy.write_bytes(spec_copy.read_bytes() + b" ")

    with pytest.raises(ValueError, match="FROZEN_HASH_MISMATCH"):
        verify_spec_freeze(tmp_path)


def test_sector_representative_selection_exclusions_and_coverage_gates() -> None:
    dates = pd.bdate_range("2024-01-02", periods=150, freq="B")
    bench = {
        "KR": RegionalBenchmarkSpec(
            region="KR", code="BM_KR", name="KR Bench", currency="KRW", fx_hedged=False
        )
    }
    bars: dict[str, pd.DataFrame] = {
        "BM_KR": _make_daily_bars(dates, start_price=30_000.0, volume=500_000.0, seed=10),
        # Tie on turnover in SEMI: 10,000 * 200,000 == 2.0B KRW for both A and B -> code asc wins
        "ETF_B": clean_and_validate_bars(
            pd.DataFrame(
                {
                    "Open": 10_000.0,
                    "High": 10_100.0,
                    "Low": 9_900.0,
                    "Close": 10_000.0,
                    "Volume": 200_000.0,
                },
                index=dates,
            ),
            "ETF_B",
        ),
        "ETF_A": clean_and_validate_bars(
            pd.DataFrame(
                {
                    "Open": 10_000.0,
                    "High": 10_100.0,
                    "Low": 9_900.0,
                    "Close": 10_000.0,
                    "Volume": 200_000.0,
                },
                index=dates,
            ),
            "ETF_A",
        ),
        # Low liquidity (< 1.0B KRW): 10,000 * 50,000 = 0.5B -> excluded
        "ETF_LOW_LIQ": clean_and_validate_bars(
            pd.DataFrame(
                {
                    "Open": 10_000.0,
                    "High": 10_100.0,
                    "Low": 9_900.0,
                    "Close": 10_000.0,
                    "Volume": 50_000.0,
                },
                index=dates,
            ),
            "ETF_LOW_LIQ",
        ),
        # Short history (< 120 bars) -> excluded
        "ETF_SHORT": clean_and_validate_bars(
            pd.DataFrame(
                {
                    "Open": 10_000.0,
                    "High": 10_100.0,
                    "Low": 9_900.0,
                    "Close": 10_000.0,
                    "Volume": 500_000.0,
                },
                index=dates[-90:],
            ),
            "ETF_SHORT",
        ),
    }

    candidates = [
        SectorETFSpec("ETF_B", "Semi B", "KR", "SEMI", "KRX Semi", "KRW", False, mapping_reviewed=True),
        SectorETFSpec("ETF_A", "Semi A", "KR", "SEMI", "KRX Semi", "KRW", False, mapping_reviewed=True),
        SectorETFSpec("ETF_LOW_LIQ", "Bio Low", "KR", "BIO", "KRX Bio", "KRW", False, mapping_reviewed=True),
        SectorETFSpec("ETF_SHORT", "Bank Short", "KR", "BANK", "KRX Bank", "KRW", False, mapping_reviewed=True),
        SectorETFSpec("ETF_LEV", "2X Semi", "KR", "SEMI", "KRX Semi 2X", "KRW", False, asset_class="LEVERAGED", mapping_reviewed=True),
        SectorETFSpec("ETF_GOLD", "Gold", "KR", "GOLD", "Gold Spot", "KRW", False, asset_class="GOLD", mapping_reviewed=True),
        SectorETFSpec("ETF_MIX", "Global Mix", "MIXED", "TECH", "Global Tech", "KRW", False, mapping_reviewed=True),
    ]

    f_date = dates[-1]
    mapping = select_frozen_representatives(
        candidates=candidates,
        bars_by_code=bars,
        benchmarks=bench,
        freeze_date=f_date,
        valid_from=f_date,
        valid_until=f_date + pd.Timedelta(days=200),
    )
    assert list(mapping.representatives.keys()) == [("KR", "SEMI")]
    assert mapping.representatives[("KR", "SEMI")].code == "ETF_A"
    assert set(mapping.excluded_non_equity_codes) == {"ETF_GOLD", "ETF_LEV"}
    assert mapping.pending_mixed_codes == ("ETF_MIX",)

    # < 5 valid regional representatives -> HALTED_INSUFFICIENT_COVERAGE
    snap = evaluate_daily_snapshot(f_date, mapping, bars)
    assert snap.region_status["KR"] == "HALTED_INSUFFICIENT_COVERAGE"

    # Shorter than 6 months lock must be rejected
    with pytest.raises(ValueError, match="MAPPING_LOCK_SHORTER_THAN_6_MONTHS"):
        select_frozen_representatives(
            candidates=candidates[:2],
            bars_by_code=bars,
            benchmarks=bench,
            freeze_date=f_date,
            valid_from=f_date,
            valid_until=f_date + pd.Timedelta(days=60),
        )


def test_strict_prefix_invariance_with_future_splits_dividends_and_midweek_truncation() -> None:
    cal, mapping, bars_full = _build_multi_sector_fixture(n_sessions=210)
    eval_start = mapping.valid_from

    # Pick a Wednesday inside the evaluation window so mid-week truncation is tested
    window_sessions = cal.sessions_in_range(eval_start, bars_full["BM_KR"].index[-25])
    wednesdays = [d for d in window_sessions[30:] if d.weekday() == 2]
    assert wednesdays, "Expected at least one Wednesday in synthetic window"
    cutoff_wed = wednesdays[0]
    assert not cal.is_week_end_session(cutoff_wed)

    # Inject massive future 5:1 split, cash dividend, and +-30% price shock strictly AFTER cutoff_wed
    bars_mutated_future: dict[str, pd.DataFrame] = {}
    bars_truncated: dict[str, pd.DataFrame] = {}

    for code, df in bars_full.items():
        bars_truncated[code] = clean_and_validate_bars(
            df.loc[:cutoff_wed, ["Open", "High", "Low", "Close", "Volume"]].copy(),
            code=code,
        )
        mut = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        mut["SplitFactor"] = 1.0
        mut["CashDividend"] = 0.0
        after_mask = mut.index > cutoff_wed
        mut.loc[after_mask, "SplitFactor"] = 5.0
        mut.loc[after_mask, ["Open", "High", "Low", "Close"]] = (
            mut.loc[after_mask, ["Open", "High", "Low", "Close"]] / 5.0 * 1.25
        )
        mut.loc[after_mask, "CashDividend"] = 500.0
        bars_mutated_future[code] = clean_and_validate_bars(mut, code=code)

    snap_trunc = evaluate_daily_snapshot(cutoff_wed, mapping, bars_truncated)
    snap_future = evaluate_daily_snapshot(cutoff_wed, mapping, bars_mutated_future)
    assert snap_trunc == snap_future

    cost_sched = default_krx_research_cost_schedule()

    def _sel_b(dt, snap, free_slots, excl_sec, _excl_codes):
        return select_weekly_etf_candidates_b(snap, free_slots, excl_sec), {}

    res_trunc = run_e0_simulation(
        policy_name="B_trunc",
        calendar=cal,
        mapping=mapping,
        bars_by_code=bars_truncated,
        cost_schedule=cost_sched,
        eval_start=eval_start,
        eval_end=cutoff_wed,
        candidate_selector=_sel_b,
    )
    res_full = run_e0_simulation(
        policy_name="B_full",
        calendar=cal,
        mapping=mapping,
        bars_by_code=bars_mutated_future,
        cost_schedule=cost_sched,
        eval_start=eval_start,
        eval_end=bars_mutated_future["BM_KR"].index[-1],
        candidate_selector=_sel_b,
    )

    # Prefix ledger up to cutoff_wed must match to machine precision
    pd.testing.assert_frame_equal(
        res_trunc.daily_ledger,
        res_full.daily_ledger.loc[:cutoff_wed],
        check_exact=False,
        rtol=1e-12,
        atol=1e-9,
    )


def test_rs20_transition_labels_and_bear_regime_uninterrupted_observation() -> None:
    s_flip = pd.Series([-0.02, -0.01, 0.005, 0.01, 0.012, 0.015, 0.02, 0.022, 0.025, 0.03, 0.035])
    assert classify_rs_transition(s_flip, lookback_sessions=10) == "최근 상대강도 전환"

    s_persist = pd.Series([0.01] * 11)
    assert classify_rs_transition(s_persist, lookback_sessions=10) == "강세 지속"

    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=180)
    eval_day = bars["BM_KR"].index[-1]

    # Force KR benchmark below SMA120 while keeping US benchmark bullish (>= SMA120)
    kr_bm = bars["BM_KR"][["Open", "High", "Low", "Close", "Volume"]].copy()
    kr_bm.iloc[-1, kr_bm.columns.get_loc("Close")] = kr_bm["Close"].iloc[-1] * 0.60
    kr_bm.iloc[-1, kr_bm.columns.get_loc("Low")] = kr_bm.iloc[-1]["Close"] * 0.99
    bars["BM_KR"] = clean_and_validate_bars(kr_bm, "BM_KR")

    snap = evaluate_daily_snapshot(eval_day, mapping, bars)
    assert snap.region_status["KR"] == "HALTED_BEAR_REGIME"
    assert snap.region_status["US"] == "ACTIVE"

    # KR observations and RS transition labels MUST still be recorded during bear regime (§7 line 115)
    kr_obs = [r for r in snap.observations if r.region == "KR"]
    assert len(kr_obs) == 6
    assert all(not r.regime_bullish for r in kr_obs)

    # New entries must exclude KR while independently allowing US
    picks = select_weekly_etf_candidates_b(snap, free_slots=3, excluded_sector_keys=set())
    assert all(p["region"] == "US" for p in picks)
    assert len(picks) >= 1


def test_stock_c_and_a_matched_and_missing_overseas_pit_handling() -> None:
    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=180)
    eval_day = bars["BM_KR"].index[-1]
    bar_dates = bars["BM_KR"].index

    # Give KR_SEC_01 a clean, non-overextended 20d relative strength lead so top-3 B picks include BOTH KR and US
    k1 = bars["KR_SEC_01"][["Open", "High", "Low", "Close", "Volume"]].copy()
    boost = np.ones(len(bar_dates), dtype=float)
    boost[-20:] = np.linspace(1.0, 1.07, 20)
    for col in ["Open", "High", "Low", "Close"]:
        k1[col] = k1[col] * boost
    k1["High"] = np.maximum(k1["High"], k1["Close"] * 1.03)
    k1["Low"] = np.minimum(k1["Low"], k1["Close"] * 0.97)
    bars["KR_SEC_01"] = clean_and_validate_bars(k1, "KR_SEC_01")

    snap_pre = evaluate_daily_snapshot(eval_day, mapping, bars)
    b_top = select_weekly_etf_candidates_b(snap_pre, free_slots=3, excluded_sector_keys=set())
    assert any(p["region"] == "US" for p in b_top)
    assert any(p["region"] == "KR" and p["sector"] == "SECTOR_KR_1" for p in b_top)
    target_kr_etf = "KR_SEC_01"

    # Construct 6 deterministic domestic stocks relative to KR_SEC_01:
    # - STK_06 has highest RS20 vs sector, but rank-6 20d median turnover -> excluded by top-5 filter!
    # - STK_01..STK_05 are top-5 liquid; STK_02 has highest RS20 > 0 within 2*ATR20 -> must win!
    etf_close = bars[target_kr_etf]["Close"].to_numpy(dtype=float)
    members: list[ConstituentItem] = []
    rel_multipliers = {
        1: 1.02,
        2: 1.04,  # Winner among top-5 liquid
        3: 1.01,
        4: 0.98,
        5: 1.005,
        6: 1.10,  # Highest RS20, but rank-6 turnover -> excluded!
    }
    for i in range(1, 7):
        scode = f"STK_{i:02d}"
        members.append(ConstituentItem(code=scode, name=f"Stock {i}", market="KOSPI"))
        ramp = np.ones(len(bar_dates), dtype=float)
        ramp[-20:] = np.linspace(1.0, rel_multipliers[i], 20)
        c_arr = etf_close * ramp * 4.0  # ~50,000+ KRW (no market-cap cap)
        vol = 20_000.0 if i == 6 else float(600_000 - i * 20_000)
        bars[scode] = clean_and_validate_bars(
            pd.DataFrame(
                {
                    "Open": c_arr * 0.995,
                    "High": c_arr * 1.045,
                    "Low": c_arr * 0.955,
                    "Close": c_arr,
                    "Volume": np.full(len(bar_dates), vol, dtype=float),
                },
                index=bar_dates,
            ),
            code=scode,
        )

    pit_table = PITConstituentTable(
        snapshots={
            ("KR", sec): [(bar_dates[0], tuple(members))]
            for (reg, sec) in mapping.representatives
            if reg == "KR"
        },
        is_pit_verified=True,
    )
    snap = evaluate_daily_snapshot(eval_day, mapping, bars)
    c_picks, reg_notes = select_weekly_stock_candidates_c(
        eval_date=eval_day,
        snapshot=snap,
        pit_constituents=pit_table,
        bars_by_code=bars,
        free_slots=3,
        excluded_sector_keys=set(),
        excluded_codes=set(),
        pit_verified_regions=("KR",),  # US intentionally omitted -> must flag NOT_IMPLEMENTED
    )
    assert reg_notes.get("US") == "NOT_IMPLEMENTED_NO_PIT_CONSTITUENTS"
    assert len(c_picks) >= 1
    assert all(p["code"] != "STK_06" for p in c_picks)  # Rank 6 turnover excluded by top-5 filter
    assert c_picks[0]["code"] == "STK_02"

    a_picks = select_weekly_stock_candidates_a_matched(
        eval_date=eval_day,
        snapshot=snap,
        mapping=mapping,
        pit_constituents=pit_table,
        bars_by_code=bars,
        free_slots=3,
        excluded_codes=set(),
        pit_verified_regions=("KR",),
    )
    assert len(a_picks) >= 1
    assert all(p["region"] == "KR" for p in a_picks)


def test_same_bar_reentry_ban_and_period_end_open_position_preservation() -> None:
    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=190)
    sessions = cal.sessions_in_range(mapping.valid_from, bars["BM_KR"].index[-1])
    week_ends = [d for d in sessions[:-5] if cal.is_week_end_session(d)]
    fri_1, fri_2 = week_ends[0], week_ends[1]
    idx_fri2 = sessions.index(fri_2)
    thu_before_fri2 = sessions[idx_fri2 - 1]

    # Force KR_SEC_01 to trigger a 2*ATR stop on thu_before_fri2 Close so it liquidates at fri_2 Open,
    # then bounces strongly on fri_2 Close! Even though fri_2 is weekly selection day,
    # KR_SEC_01 / SECTOR_KR_1 MUST be blocked from re-entering on fri_2 (same-bar re-entry ban, §6 line 90).
    df_k1 = bars["KR_SEC_01"][["Open", "High", "Low", "Close", "Volume"]].copy()
    base_px = float(df_k1.loc[fri_1, "Close"])
    df_k1.loc[thu_before_fri2, "Close"] = base_px * 0.75
    df_k1.loc[thu_before_fri2, "Low"] = min(float(df_k1.loc[thu_before_fri2, "Low"]), base_px * 0.74)
    df_k1.loc[thu_before_fri2, "High"] = max(float(df_k1.loc[thu_before_fri2, "Open"]), base_px * 0.76)
    df_k1.loc[fri_2, "Open"] = base_px * 0.76
    df_k1.loc[fri_2, "Close"] = base_px * 1.02
    df_k1.loc[fri_2, "Low"] = min(float(df_k1.loc[fri_2, "Low"]), base_px * 0.75)
    df_k1.loc[fri_2, "High"] = max(float(df_k1.loc[fri_2, "High"]), base_px * 1.03)
    bars["KR_SEC_01"] = clean_and_validate_bars(df_k1, "KR_SEC_01")

    seen_exclusions_on_fri2: dict[str, object] = {}

    def _probe_selector(dt, _snap, free_slots, excl_sec, excl_codes):
        if dt == fri_1:
            c_px = float(bars["KR_SEC_01"].loc[dt, "Close"])
            c_px2 = float(bars["US_SEC_01"].loc[dt, "Close"])
            return [
                {
                    "code": "KR_SEC_01",
                    "name": "KR 1",
                    "region": "KR",
                    "sector": "SECTOR_KR_1",
                    "instrument_kind": "EQUITY_ETF",
                    "signal_close": c_px,
                    "signal_sma20": c_px * 0.98,
                    "signal_sma60": c_px * 0.95,
                    "signal_atr20": c_px * 0.02,
                    "score": 0.2,
                },
                {
                    "code": "US_SEC_01",
                    "name": "US 1",
                    "region": "US",
                    "sector": "SECTOR_US_1",
                    "instrument_kind": "EQUITY_ETF",
                    "signal_close": c_px2,
                    "signal_sma20": c_px2 * 0.98,
                    "signal_sma60": c_px2 * 0.95,
                    "signal_atr20": c_px2 * 0.02,
                    "score": 0.1,
                },
            ][:free_slots], {}
        if dt == fri_2:
            seen_exclusions_on_fri2["sectors"] = set(excl_sec)
            seen_exclusions_on_fri2["codes"] = set(excl_codes)
        return [], {}

    res = run_e0_simulation(
        policy_name="SameBarBan_OpenEnd",
        calendar=cal,
        mapping=mapping,
        bars_by_code=bars,
        cost_schedule=default_krx_research_cost_schedule(),
        eval_start=fri_1,
        eval_end=fri_2,
        candidate_selector=_probe_selector,
    )
    assert ("KR", "SECTOR_KR_1") in seen_exclusions_on_fri2["sectors"]
    assert "KR_SEC_01" in seen_exclusions_on_fri2["codes"]

    # US_SEC_01 remains open at eval_end (fri_2): must NOT be force-closed into closed_episodes (§7 line 117)
    assert len(res.closed_episodes) == 1
    assert res.closed_episodes[0].code == "KR_SEC_01"
    assert len(res.open_positions_at_end) == 1
    assert res.open_positions_at_end[0]["code"] == "US_SEC_01"
    verify_accounting_conservation(res.daily_ledger, initial_capital=30_000_000.0)


def test_sizing_0_5_pct_risk_and_zero_qty_drop_without_backup_substitution() -> None:
    # 30,000,000 KRW account: 25% weight = 7,500,000; 0.5% risk = 150,000 -> 150,000 / (2 * 300) = 250 shares
    q_30m = compute_target_qty(nav_t=30_000_000.0, signal_close=20_000.0, signal_atr20=300.0)
    assert q_30m == 250  # 250 < floor(7,500,000 / 20,000)=375; proves 0.5% (not 1.5%) risk budget

    # 1,000,000 KRW small account with expensive ETF (300,000 KRW > 250,000 cap) -> 0 shares
    q_1m = compute_target_qty(nav_t=1_000_000.0, signal_close=300_000.0, signal_atr20=2_000.0)
    assert q_1m == 0

    # Verify rank-4 candidate is NEVER substituted when rank-1 drops with 0 shares
    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=160)
    # Rescale KR_SEC_01 price to 500,000 KRW so in 1M account (25% = 250,000) it yields 0 shares
    expensive = bars["KR_SEC_01"][["Open", "High", "Low", "Close", "Volume"]].copy() * 40.0
    expensive["Volume"] = 100_000.0
    bars["KR_SEC_01"] = clean_and_validate_bars(expensive, "KR_SEC_01")

    week_ends = [d for d in cal.sessions_in_range(mapping.valid_from, bars["BM_KR"].index[-2]) if cal.is_week_end_session(d)]
    first_fri = week_ends[0]
    next_mon = cal.next_session_after(first_fri, 1)
    assert next_mon is not None

    def _sel_b(dt, snap, free_slots, excl_sec, _excl_codes):
        return select_weekly_etf_candidates_b(snap, free_slots, excl_sec), {}

    res = run_e0_simulation(
        policy_name="B_1m_zero_drop",
        calendar=cal,
        mapping=mapping,
        bars_by_code=bars,
        cost_schedule=default_krx_research_cost_schedule(),
        eval_start=first_fri,
        eval_end=next_mon,
        candidate_selector=_sel_b,
        initial_capital=1_000_000.0,
    )
    queued_or_dropped = [
        ev for ev in res.order_events if ev["event"] in {"ENTRY_QUEUED", "ZERO_QTY_DROPPED"}
    ]
    assert len(queued_or_dropped) <= 3  # Strictly <= 3 candidates attempted; rank 4 never pulled in


def test_e0_gaps_budget_halts_no_proceeds_preuse_and_corporate_actions() -> None:
    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=190)
    sessions = cal.sessions_in_range(mapping.valid_from, bars["BM_KR"].index[-1])
    week_ends = [d for d in sessions[:-5] if cal.is_week_end_session(d)]
    fri_1, fri_2 = week_ends[0], week_ends[1]
    mon_1 = cal.next_session_after(fri_1, 1)
    tue_1 = cal.next_session_after(fri_1, 2)
    wed_1 = cal.next_session_after(fri_1, 3)
    mon_2 = cal.next_session_after(fri_2, 1)
    assert mon_1 and tue_1 and wed_1 and mon_2

    # 1. Halt US_SEC_01 on mon_1 Open -> entry must cancel as UNFILLED_HALTED_OR_NO_TRADE_AT_OPEN
    df_u1 = bars["US_SEC_01"][["Open", "High", "Low", "Close", "Volume"]].copy()
    df_u1["Halted"] = False
    df_u1.loc[mon_1, "Halted"] = True
    df_u1.loc[mon_1, "Volume"] = 0.0
    bars["US_SEC_01"] = clean_and_validate_bars(df_u1, "US_SEC_01")

    # 2. Give KR_SEC_01 a +6% upward opening gap on mon_1 (must fill at raw Open — no M1 cap!),
    #    then a 2-for-1 split + cash dividend on tue_1, then crash below entry-2*ATR on tue_1 Close,
    #    halt on wed_1 Open (exit delayed!), and resume on thu_1 Open!
    thu_1 = cal.next_session_after(fri_1, 4)
    assert thu_1 is not None
    df_k1 = bars["KR_SEC_01"][["Open", "High", "Low", "Close", "Volume"]].copy()
    df_k1["SplitFactor"] = 1.0
    df_k1["CashDividend"] = 0.0
    df_k1["Halted"] = False

    fri_close = float(df_k1.loc[fri_1, "Close"])
    df_k1.loc[mon_1, "Open"] = fri_close * 1.06
    df_k1.loc[mon_1, "High"] = max(float(df_k1.loc[mon_1, "High"]), fri_close * 1.07)
    df_k1.loc[mon_1, "Low"] = min(float(df_k1.loc[mon_1, "Low"]), fri_close * 1.04)
    df_k1.loc[mon_1, "Close"] = fri_close * 1.05

    # 2:1 split from tue_1 onward + 100 KRW/share dividend on tue_1 + severe drop below 2*ATR stop
    df_k1.loc[df_k1.index >= tue_1, "SplitFactor"] = 2.0
    df_k1.loc[df_k1.index >= tue_1, ["Open", "High", "Low", "Close"]] *= 0.5
    df_k1.loc[tue_1, "CashDividend"] = 100.0
    df_k1.loc[tue_1, "Close"] = (fri_close * 0.5) * 0.80
    df_k1.loc[tue_1, "Low"] = df_k1.loc[tue_1, "Close"] * 0.99

    # Halt on wed_1 so scheduled exit is delayed to thu_1
    df_k1.loc[wed_1, "Halted"] = True
    df_k1.loc[wed_1, "Volume"] = 0.0
    bars["KR_SEC_01"] = clean_and_validate_bars(df_k1, "KR_SEC_01")

    # 3. On mon_2, inject +50% uncapped relisting jump on KR_SEC_02 to trigger BUDGET_EXCEEDED_AT_OPEN
    df_k2 = bars["KR_SEC_02"][["Open", "High", "Low", "Close", "Volume"]].copy()
    fri2_c = float(df_k2.loc[fri_2, "Close"])
    df_k2.loc[mon_2, "Open"] = fri2_c * 1.50
    df_k2.loc[mon_2, "High"] = fri2_c * 1.52
    df_k2.loc[mon_2, "Close"] = fri2_c * 1.48
    bars["KR_SEC_02"] = clean_and_validate_bars(df_k2, "KR_SEC_02")

    def _custom_selector(dt, _snap, free_slots, excl_sec, excl_codes):
        picks = []
        if dt == fri_1:
            for code, reg, sec in [("US_SEC_01", "US", "SECTOR_US_1"), ("KR_SEC_01", "KR", "SECTOR_KR_1")]:
                if (reg, sec) not in excl_sec and code not in excl_codes and len(picks) < free_slots:
                    c_px = float(bars[code].loc[dt, "Close"])
                    picks.append(
                        {
                            "code": code,
                            "name": code,
                            "region": reg,
                            "sector": sec,
                            "instrument_kind": "EQUITY_ETF",
                            "signal_close": c_px,
                            "signal_sma20": c_px * 0.98,
                            "signal_sma60": c_px * 0.95,
                            "signal_atr20": c_px * 0.02,
                            "score": 0.1,
                        }
                    )
        elif dt == fri_2:
            code, reg, sec = "KR_SEC_02", "KR", "SECTOR_KR_2"
            if (reg, sec) not in excl_sec and len(picks) < free_slots:
                c_px = float(bars[code].loc[dt, "Close"])
                picks.append(
                    {
                        "code": code,
                        "name": code,
                        "region": reg,
                        "sector": sec,
                        "instrument_kind": "EQUITY_ETF",
                        "signal_close": c_px,
                        "signal_sma20": c_px * 0.98,
                        "signal_sma60": c_px * 0.95,
                        "signal_atr20": c_px * 0.02,
                        "score": 0.1,
                    }
                )
        return picks, {}

    res = run_e0_simulation(
        policy_name="E0_Adversarial",
        calendar=cal,
        mapping=mapping,
        bars_by_code=bars,
        cost_schedule=default_krx_research_cost_schedule(),
        eval_start=fri_1,
        eval_end=sessions[-1],
        candidate_selector=_custom_selector,
        initial_capital=30_000_000.0,
    )
    verify_accounting_conservation(res.daily_ledger, initial_capital=30_000_000.0)

    ev_names = [ev["event"] for ev in res.order_events]
    assert "UNFILLED_HALTED_OR_NO_TRADE_AT_OPEN" in ev_names
    assert "CORPORATE_ACTION_SPLIT_ADJUST" in ev_names
    assert "DIVIDEND_CREDIT" in ev_names
    assert "EXIT_DELAYED_HALT_OR_NO_TRADE" in ev_names
    assert "BUDGET_EXCEEDED_AT_OPEN" in ev_names

    assert len(res.closed_episodes) == 1
    ep = res.closed_episodes[0]
    assert ep.code == "KR_SEC_01"
    assert ep.exit_delay_halts == 1
    assert ep.exit_date == thu_1.strftime("%Y-%m-%d")
    assert ep.dividends_received > 0.0


def test_cost_schedule_statutory_years_etf_zero_tax_and_ter_ban() -> None:
    sched = default_krx_research_cost_schedule()
    assert sched.sell_tax_rate_for("EQUITY_ETF", pd.Timestamp("2024-06-01")) == 0.0
    assert sched.sell_tax_rate_for("KOSPI_STOCK", pd.Timestamp("2022-06-01")) == pytest.approx(0.0023)
    assert sched.sell_tax_rate_for("KOSPI_STOCK", pd.Timestamp("2023-06-01")) == pytest.approx(0.0020)
    assert sched.sell_tax_rate_for("KOSPI_STOCK", pd.Timestamp("2024-06-01")) == pytest.approx(0.0018)
    assert sched.sell_tax_rate_for("KOSPI_STOCK", pd.Timestamp("2025-06-01")) == pytest.approx(0.0015)
    assert sched.sell_tax_rate_for("KOSPI_STOCK", pd.Timestamp("2026-06-01")) == pytest.approx(0.0020)

    # Prohibits ETF TER double-deduction
    bad_ter = CostSchedule(
        schedule_id="bad_ter",
        account_type="GENERAL_RESEARCH_SCENARIO",
        buy_fee_rate=0.00015,
        sell_fee_rate=0.00015,
        entry_slippage_rate=0.0005,
        exit_slippage_rate=0.0005,
        tax_rules=(TaxRuleEntry("2024-01-01", "2026-12-31", "EQUITY_ETF", 0.0),),
        etf_ter_extra_deduction_rate=0.0045,
    )
    with pytest.raises(ValueError, match="ETF_MANAGEMENT_FEE_DOUBLE_DEDUCTION_PROHIBITED"):
        bad_ter.validate()

    # Prohibits copying stock sell transaction tax onto Equity ETF
    bad_etf_tax = CostSchedule(
        schedule_id="bad_etf_tax",
        account_type="GENERAL_RESEARCH_SCENARIO",
        buy_fee_rate=0.00015,
        sell_fee_rate=0.00015,
        entry_slippage_rate=0.0005,
        exit_slippage_rate=0.0005,
        tax_rules=(TaxRuleEntry("2024-01-01", "2026-12-31", "EQUITY_ETF", 0.0018),),
    )
    with pytest.raises(ValueError, match="EQUITY_ETF_MUST_NOT_COPY_STOCK_SELL_TRANSACTION_TAX"):
        bad_etf_tax.validate()


def test_e1_manual_journal_single_check_band_and_12w_anti_overclaim(tmp_path: Path) -> None:
    band = compute_e1_entry_check_band(signal_sma20=10_000.0, signal_close=10_400.0, signal_atr20=200.0)
    assert band["lower_exclusive_sma20"] == 10_000.0
    assert band["upper_inclusive_close_plus_half_atr"] == 10_500.0
    assert not is_e1_entry_price_in_band(10_000.0, 10_000.0, 10_400.0, 200.0)  # Strict > SMA20
    assert is_e1_entry_price_in_band(10_500.0, 10_000.0, 10_400.0, 200.0)      # Inclusive <= upper
    assert not is_e1_entry_price_in_band(10_505.0, 10_000.0, 10_400.0, 200.0)

    journal_file = tmp_path / "e1_journal.jsonl"
    valid_fill = E1JournalRecord(
        record_id="E1-0001",
        action_type="ENTRY",
        signal_date="2026-03-06",
        check_session_date="2026-03-09",
        code="KR_SEC_01",
        region="KR",
        sector="SEMI",
        signal_close=10_400.0,
        signal_sma20=10_000.0,
        signal_atr20=200.0,
        disposition="FILLED",
        check_index_for_signal=1,
        observed_at="2026-03-09T10:32:00+09:00",
        observed_price=10_450.0,
        ordered_at="2026-03-09T10:33:00+09:00",
        order_qty=20,
        order_limit_or_type="LIMIT_10450",
        filled_at="2026-03-09T10:33:05+09:00",
        filled_qty=20,
        filled_price=10_450.0,
        fee_tax_krw=31.0,
    )
    append_e1_journal_record(journal_file, valid_fill)

    # Second intraday check / retroactive attempt on same (signal_date, code) MUST fail
    second_attempt = E1JournalRecord(
        record_id="E1-0002",
        action_type="ENTRY",
        signal_date="2026-03-06",
        check_session_date="2026-03-09",
        code="KR_SEC_01",
        region="KR",
        sector="SEMI",
        signal_close=10_400.0,
        signal_sma20=10_000.0,
        signal_atr20=200.0,
        disposition="VIRTUAL_OBSERVATION_NO_ORDER",
        check_index_for_signal=1,
        observed_at="2026-03-09T14:00:00+09:00",
        observed_price=10_300.0,
    )
    with pytest.raises(ValueError, match="SECOND_ENTRY_ATTEMPT_AFTER_SINGLE_CHECK_PROHIBITED"):
        append_e1_journal_record(journal_file, second_attempt)

    # Out-of-band fill attempt MUST fail
    out_of_band_fill = E1JournalRecord(
        record_id="E1-0003",
        action_type="ENTRY",
        signal_date="2026-03-13",
        check_session_date="2026-03-16",
        code="KR_SEC_02",
        region="KR",
        sector="BIO",
        signal_close=10_400.0,
        signal_sma20=10_000.0,
        signal_atr20=200.0,
        disposition="FILLED",
        observed_at="2026-03-16T10:00:00+09:00",
        observed_price=10_600.0,  # > 10,500 upper limit
        ordered_at="2026-03-16T10:01:00+09:00",
        order_qty=10,
        filled_at="2026-03-16T10:01:05+09:00",
        filled_qty=10,
        filled_price=10_600.0,
    )
    with pytest.raises(ValueError, match="RETROACTIVE_OR_OUT_OF_BAND_ENTRY_FILL_PROHIBITED"):
        out_of_band_fill.validate()

    # 11:00 KST research comparison record cannot claim real user order/fill
    ref_1100_fake_fill = E1JournalRecord(
        record_id="E1-0004",
        action_type="ENTRY",
        signal_date="2026-03-20",
        check_session_date="2026-03-23",
        code="KR_SEC_03",
        region="KR",
        sector="BANK",
        signal_close=10_400.0,
        signal_sma20=10_000.0,
        signal_atr20=200.0,
        disposition="FILLED",
        observed_at="2026-03-23T11:00:00+09:00",
        observed_price=10_420.0,
        ordered_at="2026-03-23T11:00:00+09:00",
        order_qty=10,
        filled_at="2026-03-23T11:00:00+09:00",
        filled_qty=10,
        filled_price=10_420.0,
        is_research_1100_comparison_only=True,
    )
    with pytest.raises(ValueError, match="RESEARCH_1100_REFERENCE_CANNOT_CLAIM_REAL_USER_ORDER_OR_FILL"):
        ref_1100_fake_fill.validate()

    # 8-week journal (< 24m / < 60 episodes) must prohibit profitability claims
    virt_rec = E1JournalRecord(
        record_id="E1-0005",
        action_type="ENTRY",
        signal_date="2026-05-01",
        check_session_date="2026-05-04",
        code="KR_SEC_02",
        region="KR",
        sector="BIO",
        signal_close=10_400.0,
        signal_sma20=10_000.0,
        signal_atr20=200.0,
        disposition="VIRTUAL_OBSERVATION_NO_ORDER",
        observed_at="2026-05-04T11:10:00+09:00",
        observed_price=10_420.0,
    )
    append_e1_journal_record(journal_file, virt_rec)
    summary = summarize_e1_journal([valid_fill, virt_rec])
    assert summary["real_entry_fills"] == 1
    assert summary["virtual_observations"] == 1
    assert summary["evaluation_stage"] == "CONSISTENCY_CHECK_ONLY_NO_PROFITABILITY_CLAIM"
    assert summary["profitability_claim_allowed"] is False


def test_evaluator_mbb_zero_loss_pf_verdict_branches_and_cli_registry(tmp_path: Path) -> None:
    # 1. Deterministic 20d Moving Block Bootstrap (seed=20261107)
    rng = np.random.default_rng(7)
    diffs = 0.0015 + rng.normal(0.0, 0.002, size=500)
    mbb1 = moving_block_bootstrap_ci(diffs, block_len=20, n_boot=10_000, seed=FIXED_RESEARCH_SEED)
    mbb2 = moving_block_bootstrap_ci(diffs, block_len=20, n_boot=10_000, seed=FIXED_RESEARCH_SEED)
    assert mbb1 == mbb2
    assert mbb1["valid"] is True
    assert mbb1["ci_lower"] > 0.0

    # 2. Zero-loss Profit Factor guard -> UNDEFINED_ZERO_LOSS_WITHHOLD
    win_ep = ClosedEpisode(
        episode_id=1,
        code="KR_SEC_01",
        name="KR 1",
        region="KR",
        sector="S1",
        instrument_kind="EQUITY_ETF",
        signal_date="2025-01-03",
        entry_date="2025-01-06",
        exit_signal_date="2025-01-17",
        exit_date="2025-01-20",
        holding_sessions=10,
        exit_delay_halts=0,
        exit_reason="BELOW_SMA20_2D",
        entry_qty_post_adjust=100,
        entry_raw_open_adj=10_000.0,
        exit_raw_open=11_000.0,
        initial_cost_basis=1_000_650.0,
        net_exit_proceeds=1_099_285.0,
        dividends_received=0.0,
        fractional_cash_received=0.0,
        entry_fee=150.0,
        exit_fee=165.0,
        exit_tax=0.0,
        net_pnl_krw=98_635.0,
        net_return_pct=0.09857,
    )
    ep_metrics = compute_episode_metrics([win_ep])
    assert ep_metrics["profit_factor"] is None
    assert ep_metrics["profit_factor_status"] == "UNDEFINED_ZERO_LOSS_WITHHOLD"

    prov_ok = DataProvenanceManifest(
        manifest_id="prov-ok",
        fetched_at="2026-11-07T00:00:00+09:00",
        provider="synthetic_verified",
        total_return_verified=True,
        corporate_actions_verified=True,
        mapping_pit_verified=True,
        constituents_pit_verified_regions=("KR",),
    )
    frozen_rand_meta = {
        "n_policies": DEFAULT_RANDOM_POLICIES,
        "seed": FIXED_RESEARCH_SEED,
        "copies_actual_holding_periods": False,
        "p95_cumulative_net_return": 0.18,
    }

    # Branch A: Zero-loss PF or <60 episodes -> INSUFFICIENT_EVIDENCE
    v_insuff = classify_v1_verdict(
        is_uninspected_oos=True,
        meets_24_months=True,
        closed_episodes_count=65,
        provenance=prov_ok,
        b_cum_net_return=0.25,
        b_ann_net_return=0.118,
        simple60_ann_net_return=0.08,
        mbb_result=mbb1,
        b_mdd=0.09,
        b_profit_factor=None,
        b_profit_factor_status="UNDEFINED_ZERO_LOSS_WITHHOLD",
        b_cost2x_cum_net_return=0.20,
        simple60_cost2x_cum_net_return=0.15,
        b_delay1d_cum_net_return=0.19,
        simple60_delay1d_cum_net_return=0.14,
        random_p95_cum_net_return=0.18,
        random_benchmark=frozen_rand_meta,
    )
    assert v_insuff["verdict_code"] == "INSUFFICIENT_EVIDENCE"

    # Branch B: Positive point improvement & viable risk/stress, but 0 inside 95% CI -> ADVANTAGE_UNPROVEN
    mbb_wide = {**mbb1, "mean_daily_diff": 0.00012, "ci_lower": -0.00005, "ci_upper": 0.00030}
    v_unproven = classify_v1_verdict(
        is_uninspected_oos=True,
        meets_24_months=True,
        closed_episodes_count=65,
        provenance=prov_ok,
        b_cum_net_return=0.25,
        b_ann_net_return=0.118,
        simple60_ann_net_return=0.090,
        mbb_result=mbb_wide,
        b_mdd=0.09,
        b_profit_factor=1.55,
        b_profit_factor_status="DEFINED",
        b_cost2x_cum_net_return=0.20,
        simple60_cost2x_cum_net_return=0.15,
        b_delay1d_cum_net_return=0.19,
        simple60_delay1d_cum_net_return=0.14,
        random_p95_cum_net_return=0.18,
        random_benchmark=frozen_rand_meta,
    )
    assert v_unproven["verdict_code"] == "ADVANTAGE_UNPROVEN"

    # Branch C: MDD > 15% with sufficient sample -> REJECTED_V1
    v_rej = classify_v1_verdict(
        is_uninspected_oos=True,
        meets_24_months=True,
        closed_episodes_count=65,
        provenance=prov_ok,
        b_cum_net_return=0.25,
        b_ann_net_return=0.118,
        simple60_ann_net_return=0.080,
        mbb_result=mbb1,
        b_mdd=0.19,  # > 15%
        b_profit_factor=1.55,
        b_profit_factor_status="DEFINED",
        b_cost2x_cum_net_return=0.20,
        simple60_cost2x_cum_net_return=0.15,
        b_delay1d_cum_net_return=0.19,
        simple60_delay1d_cum_net_return=0.14,
        random_p95_cum_net_return=0.18,
        random_benchmark=frozen_rand_meta,
    )
    assert v_rej["verdict_code"] == "REJECTED_V1"

    # Branch D: All 8 hurdles & prerequisites met -> ELIGIBLE_FOR_REVIEW_E0_ONLY (still E1 incomplete!)
    v_elig = classify_v1_verdict(
        is_uninspected_oos=True,
        meets_24_months=True,
        closed_episodes_count=65,
        provenance=prov_ok,
        b_cum_net_return=0.25,
        b_ann_net_return=0.118,
        simple60_ann_net_return=0.080,
        mbb_result=mbb1,
        b_mdd=0.11,
        b_profit_factor=1.55,
        b_profit_factor_status="DEFINED",
        b_cost2x_cum_net_return=0.20,
        simple60_cost2x_cum_net_return=0.15,
        b_delay1d_cum_net_return=0.19,
        simple60_delay1d_cum_net_return=0.14,
        random_p95_cum_net_return=0.18,
        random_benchmark=frozen_rand_meta,
    )
    assert v_elig["verdict_code"] == "ELIGIBLE_FOR_REVIEW_E0_ONLY"
    assert v_elig["operational_promotion_allowed"] is False
    assert v_elig["e1_manual_validation_complete"] is False

    # 3. End-to-end bundle CLI + non-destructive attempt registry test
    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=180)
    bundle_dir = tmp_path / "bundle"
    bars_dir = bundle_dir / "bars"
    bars_dir.mkdir(parents=True)

    bar_files_rel: dict[str, str] = {}
    for code, df in bars.items():
        rel = f"bars/{code}.csv"
        df[["Open", "High", "Low", "Close", "Volume"]].to_csv(bundle_dir / rel)
        bar_files_rel[code] = rel

    bundle_doc = {
        "scheduled_sessions": [d.strftime("%Y-%m-%d") for d in cal.sessions],
        "benchmarks": {k: v.__dict__ for k, v in mapping.benchmarks.items()},
        "representatives": [spec.__dict__ for spec in mapping.representatives.values()],
        "mapping_meta": {
            "mapping_version": mapping.mapping_version,
            "freeze_date": mapping.freeze_date.strftime("%Y-%m-%d"),
            "valid_from": mapping.valid_from.strftime("%Y-%m-%d"),
            "valid_until": mapping.valid_until.strftime("%Y-%m-%d"),
            "is_pit_verified": False,
        },
        "bar_files": bar_files_rel,
        "provenance": {
            "manifest_id": "test-bundle-001",
            "fetched_at": "2026-11-07T12:00:00+09:00",
            "provider": "synthetic_offline",
            "total_return_verified": False,
            "corporate_actions_verified": True,
            "mapping_pit_verified": False,
            "constituents_pit_verified_regions": [],
        },
        "eval_start": mapping.valid_from.strftime("%Y-%m-%d"),
        "eval_end": bars["BM_KR"].index[-1].strftime("%Y-%m-%d"),
        "is_uninspected_oos": False,
    }
    bundle_path = bundle_dir / "bundle.json"
    bundle_path.write_text(json.dumps(bundle_doc, indent=2), encoding="utf-8")

    out_dir = tmp_path / "runs"
    rc1 = cli_main(
        [
            "--repo-root",
            str(WORKSPACE_ROOT),
            "--bundle",
            str(bundle_path),
            "--out-dir",
            str(out_dir),
            "--n-random",
            "25",
        ]
    )
    rc2 = cli_main(
        [
            "--repo-root",
            str(WORKSPACE_ROOT),
            "--bundle",
            str(bundle_path),
            "--out-dir",
            str(out_dir),
            "--n-random",
            "25",
        ]
    )
    assert rc1 == 0 and rc2 == 0

    registry = json.loads((out_dir / "attempt_registry.json").read_text(encoding="utf-8"))
    assert registry["total_attempts"] == 2
    assert registry["attempts"][0]["attempt_number"] == 1
    assert registry["attempts"][1]["attempt_number"] == 2
    assert registry["attempts"][0]["run_dir"] != registry["attempts"][1]["run_dir"]
    assert registry["attempts"][0]["verdict_code"] == "INSUFFICIENT_EVIDENCE"


def test_total_return_reconciliation_and_pending_exit_proceeds_non_preuse() -> None:
    # 1. Total-return reconciliation catches unadjusted/mismatched vendor AdjClose (§2 line 29)
    dates = pd.bdate_range("2025-01-02", periods=5, freq="B")
    raw = pd.DataFrame(
        {
            "Open": [10_000.0, 10_000.0, 5_000.0, 4_900.0, 5_000.0],
            "High": [10_100.0, 10_100.0, 5_100.0, 5_000.0, 5_100.0],
            "Low": [9_900.0, 9_900.0, 4_900.0, 4_850.0, 4_950.0],
            "Close": [10_000.0, 10_000.0, 5_000.0, 4_900.0, 5_000.0],
            "Volume": [100_000.0] * 5,
            "SplitFactor": [1.0, 1.0, 2.0, 2.0, 2.0],
            "CashDividend": [0.0, 0.0, 0.0, 100.0, 0.0],
        },
        index=dates,
    )
    cleaned_ok = clean_and_validate_bars(raw, "TR_OK")
    assert verify_total_return_reconciliation(cleaned_ok) is True

    corrupted = raw.copy()
    corrupted["TotalReturnClose"] = corrupted["Close"]  # Forgot split & dividend reinvestment!
    cleaned_bad = clean_and_validate_bars(corrupted, "TR_BAD")
    assert verify_total_return_reconciliation(cleaned_bad) is False

    # 2. Pending exit proceeds on Friday Close MUST NOT fund a same-timestamp Monday Open buy (§6 line 90)
    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=190)
    sessions = cal.sessions_in_range(mapping.valid_from, bars["BM_KR"].index[-1])
    week_ends = [d for d in sessions[:-5] if cal.is_week_end_session(d)]
    fri_1, fri_2 = week_ends[0], week_ends[1]

    # Crash KR_SEC_01 on fri_2 Close so an exit is queued on fri_2 Close for mon_2 Open
    df_k1 = bars["KR_SEC_01"][["Open", "High", "Low", "Close", "Volume"]].copy()
    base_px = float(df_k1.loc[fri_1, "Close"])
    df_k1.loc[fri_2, "Close"] = base_px * 0.70
    df_k1.loc[fri_2, "Low"] = min(float(df_k1.loc[fri_2, "Low"]), base_px * 0.69)
    bars["KR_SEC_01"] = clean_and_validate_bars(df_k1, "KR_SEC_01")

    def _tight_selector(dt, _snap, free_slots, excl_sec, _excl_codes):
        if dt == fri_1:
            # Fill 2 slots at ~48% weight each (using custom max_slot_weight=0.49 in simulation)
            out = []
            for code, reg, sec in [("KR_SEC_01", "KR", "SECTOR_KR_1"), ("US_SEC_01", "US", "SECTOR_US_1")]:
                c_px = float(bars[code].loc[dt, "Close"])
                out.append(
                    {
                        "code": code,
                        "name": code,
                        "region": reg,
                        "sector": sec,
                        "instrument_kind": "EQUITY_ETF",
                        "signal_close": c_px,
                        "signal_sma20": c_px * 0.98,
                        "signal_sma60": c_px * 0.95,
                        "signal_atr20": c_px * 0.05,
                        "score": 0.2,
                    }
                )
            return out[:free_slots], {}
        if dt == fri_2:
            # KR_SEC_01 just queued an exit for mon_2 Open, so nearly all cash is still tied up on fri_2 Close!
            # Attempting to buy an expensive unit ETF on fri_2 Close MUST NOT borrow KR_SEC_01's pending exit proceeds.
            c_px = float(bars["US_SEC_02"].loc[dt, "Close"])
            return [
                {
                    "code": "US_SEC_02",
                    "name": "US 2",
                    "region": "US",
                    "sector": "SECTOR_US_2",
                    "instrument_kind": "EQUITY_ETF",
                    "signal_close": c_px,
                    "signal_sma20": c_px * 0.98,
                    "signal_sma60": c_px * 0.95,
                    "signal_atr20": c_px * 0.05,
                    "score": 0.1,
                }
            ][:free_slots], {}
        return [], {}

    # Exact initial capital = 1 share of KR_SEC_01 + 1 share of US_SEC_01 + 2,500 KRW buffer:
    # After mon_1 fills, settled cash is ~2,500 KRW (< 1 share of US_SEC_02 ~20,000 KRW),
    # while NAV on fri_2 (~36,000 KRW * 0.65 > 20,000 KRW) gives q_raw == 1 share!
    init_cap = (
        float(bars["KR_SEC_01"].loc[fri_1, "Close"])
        + float(bars["US_SEC_01"].loc[fri_1, "Close"])
        + 2_500.0
    )
    res = run_e0_simulation(
        policy_name="NoPendingProceedsPreuse",
        calendar=cal,
        mapping=mapping,
        bars_by_code=bars,
        cost_schedule=default_krx_research_cost_schedule(),
        eval_start=fri_1,
        eval_end=sessions[sessions.index(fri_2) + 2],
        candidate_selector=_tight_selector,
        initial_capital=init_cap,
        max_slot_weight=0.65,
        slot_risk_budget=0.65,
    )
    fri2_events = [ev["event"] for ev in res.order_events if ev["date"] == fri_2.strftime("%Y-%m-%d")]
    assert "EXIT_QUEUED" in fri2_events
    assert "INSUFFICIENT_CASH_AT_SIGNAL" in fri2_events
    verify_accounting_conservation(res.daily_ledger, initial_capital=init_cap)


def _flat_or_curve_frame(dates: pd.DatetimeIndex, close_vals: float | np.ndarray, volume: float = 200_000.0) -> pd.DataFrame:
    c = np.broadcast_to(np.asarray(close_vals, dtype=float), (len(dates),)).copy()
    return clean_and_validate_bars(
        pd.DataFrame(
            {
                "Open": c,
                "High": c * 1.05,
                "Low": c * 0.95,
                "Close": c,
                "Volume": np.full(len(dates), volume, dtype=float),
                "SplitFactor": 1.0,
                "CashDividend": 0.0,
            },
            index=dates,
        ),
        "SYN",
    )


def test_r1_a_matched_isolates_sector_gate_only_and_per_sector_top5_turnover() -> None:
    from dataclasses import replace

    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=180)
    dates = bars["BM_KR"].index
    t = dates[-1]

    sector_curve = 10_000.0 * (1.001 ** np.arange(len(dates)))
    stock_curve = 10_000.0 * (1.002 ** np.arange(len(dates)))
    for code in list(bars.keys()):
        bars[code] = _flat_or_curve_frame(dates, 10_000.0)
    bars["KR_SEC_01"] = _flat_or_curve_frame(dates, sector_curve)
    bars["STOCK"] = _flat_or_curve_frame(dates, stock_curve)

    # Benchmark rises over the last 5 sessions so STOCK's RS vs benchmark decreases, while RS vs sector > 0
    bench = np.full(len(dates), 10_000.0)
    bench[-5:] *= np.linspace(1.001, 1.01, 5)
    bars["BM_KR"] = _flat_or_curve_frame(dates, bench)

    snap_base = evaluate_daily_snapshot(t, mapping, bars)
    pit_single = PITConstituentTable(
        {("KR", "SECTOR_KR_1"): [(dates[0], (ConstituentItem("STOCK", "Stock", "KOSPI"),))]},
        is_pit_verified=True,
    )

    # Case 1: Sector gate PASSED -> C and A-matched must select the exact same candidate ["STOCK"]
    rows_gate_on = tuple(
        replace(r, is_entry_qualified=(r.code == "KR_SEC_01"), regime_bullish=True, region_coverage_ok=True)
        for r in snap_base.observations
    )
    snap_gate_on = replace(snap_base, observations=rows_gate_on, region_status={"KR": "ACTIVE", "US": "ACTIVE"})
    c_on, _ = select_weekly_stock_candidates_c(t, snap_gate_on, pit_single, bars, 3, set(), set(), ("KR",))
    a_on = select_weekly_stock_candidates_a_matched(
        t, snap_gate_on, mapping, pit_single, bars, 3, set(), ("KR",), excluded_sector_keys=set()
    )
    assert [x["code"] for x in c_on] == ["STOCK"]
    assert [x["code"] for x in a_on] == ["STOCK"]

    # Case 2: Sector gate FAILED -> C is empty while A-matched still selects ["STOCK"]
    rows_gate_off = tuple(
        replace(r, is_entry_qualified=False, regime_bullish=True, region_coverage_ok=True)
        for r in snap_base.observations
    )
    snap_gate_off = replace(snap_base, observations=rows_gate_off, region_status={"KR": "ACTIVE", "US": "ACTIVE"})
    c_off, _ = select_weekly_stock_candidates_c(t, snap_gate_off, pit_single, bars, 3, set(), set(), ("KR",))
    a_off = select_weekly_stock_candidates_a_matched(
        t, snap_gate_off, mapping, pit_single, bars, 3, set(), ("KR",), excluded_sector_keys=set()
    )
    assert c_off == []
    assert [x["code"] for x in a_off] == ["STOCK"]

    # Case 3: Held-sector exclusion in A-matched blocks SECTOR_KR_1
    a_excl = select_weekly_stock_candidates_a_matched(
        t,
        snap_gate_on,
        mapping,
        pit_single,
        bars,
        3,
        set(),
        ("KR",),
        excluded_sector_keys={("KR", "SECTOR_KR_1")},
    )
    assert a_excl == []

    # Case 4: Per-sector top-5 turnover is NOT replaced by regional 5*N_sectors pool
    # SECTOR_KR_1 has 6 liquid stocks: S1..S5 (~27B..38B turnover) and S6 (~8.5B turnover, rank-6 in SECTOR_KR_1 with huge RS).
    # SECTOR_KR_2 has 5 lower-liquidity stocks (T1..T5, ~1.3B turnover; T1 has positive RS).
    bars["KR_SEC_02"] = _flat_or_curve_frame(dates, sector_curve)
    sec1_members = []
    for idx in range(1, 7):
        sc = f"S_{idx:02d}"
        sec1_members.append(ConstituentItem(sc, sc, "KOSPI"))
        mult = 1.003 if idx == 6 else (1.0015 if idx == 1 else 1.0005)
        vol = 500_000.0 if idx == 6 else float(3_000_000 - idx * 100_000)
        bars[sc] = _flat_or_curve_frame(
            dates,
            10_000.0 * (mult ** np.arange(len(dates))),
            volume=vol,
        )
    sec2_members = []
    for idx in range(1, 6):
        tc = f"T_{idx:02d}"
        sec2_members.append(ConstituentItem(tc, tc, "KOSPI"))
        mult = 1.0018 if idx == 1 else 1.0005
        bars[tc] = _flat_or_curve_frame(
            dates,
            10_000.0 * (mult ** np.arange(len(dates))),
            volume=float(100_000 - idx * 1_000),
        )
    pit_two_sec = PITConstituentTable(
        {
            ("KR", "SECTOR_KR_1"): [(dates[0], tuple(sec1_members))],
            ("KR", "SECTOR_KR_2"): [(dates[0], tuple(sec2_members))],
        },
        is_pit_verified=True,
    )
    snap_two = evaluate_daily_snapshot(t, mapping, bars)
    snap_two = replace(
        snap_two,
        observations=tuple(
            replace(r, regime_bullish=True, region_coverage_ok=True) for r in snap_two.observations
        ),
        region_status={"KR": "ACTIVE", "US": "ACTIVE"},
    )
    a_two = select_weekly_stock_candidates_a_matched(
        t, snap_two, mapping, pit_two_sec, bars, 3, set(), ("KR",)
    )
    a_two_codes = [x["code"] for x in a_two]
    assert "S_06" not in a_two_codes  # Rank 6 in SECTOR_KR_1 excluded despite huge regional turnover
    assert set(a_two_codes) == {"S_01", "T_01"}  # Max 1 winner per sector


def test_r2_boundary_input_validation_tr_reconciliation_pit_conflicts_and_defaulted_ca(tmp_path: Path) -> None:
    from dataclasses import replace

    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=180)
    # Upgrade bars to explicit corporate action columns first so we isolate TR corruption & PIT conflicts
    explicit_bars: dict[str, pd.DataFrame] = {}
    for code, df in bars.items():
        explicit_bars[code] = clean_and_validate_bars(
            df[["Open", "High", "Low", "Close", "Volume", "SplitFactor", "CashDividend", "TotalReturnClose"]].copy(),
            code=code,
            explicit_column_overrides={
                "SplitFactor": True,
                "CashDividend": True,
                "Halted": False,
                "TotalReturnClose": True,
            },
        )

    bad_bars = {k: v.copy() for k, v in explicit_bars.items()}
    bad_bars["KR_SEC_01"].loc[bad_bars["KR_SEC_01"].index[-1], "TotalReturnClose"] *= 2.0

    bar_files = {}
    for code, df in bad_bars.items():
        fname = f"{code}.csv"
        df.to_csv(tmp_path / fname)
        bar_files[code] = fname

    prov_conflict = DataProvenanceManifest(
        manifest_id="r2-audit",
        fetched_at="2026-11-07",
        provider="synthetic",
        total_return_verified=True,
        corporate_actions_verified=True,
        mapping_pit_verified=True,
        constituents_pit_verified_regions=("KR",),
    )
    bundle_doc = {
        "scheduled_sessions": [d.strftime("%Y-%m-%d") for d in cal.sessions],
        "benchmarks": {k: v.__dict__ for k, v in mapping.benchmarks.items()},
        "representatives": [s.__dict__ for s in mapping.representatives.values()],
        "mapping_meta": {
            "mapping_version": "r2-audit",
            "freeze_date": mapping.freeze_date.strftime("%Y-%m-%d"),
            "valid_from": mapping.valid_from.strftime("%Y-%m-%d"),
            "valid_until": mapping.valid_until.strftime("%Y-%m-%d"),
            "is_pit_verified": False,  # Conflicts with provenance.mapping_pit_verified=True!
        },
        "bar_files": bar_files,
        "provenance": {**prov_conflict.__dict__},
        "eval_start": mapping.valid_from.strftime("%Y-%m-%d"),
        "eval_end": explicit_bars["BM_KR"].index[-1].strftime("%Y-%m-%d"),
        "is_uninspected_oos": True,
    }
    bundle_path = tmp_path / "bundle_bad.json"
    bundle_path.write_text(json.dumps(bundle_doc), encoding="utf-8")

    # 1. Strict boundary rejects corrupted TotalReturnClose in BOTH CLI loader and Python evaluator
    with pytest.raises(ValueError, match="TOTAL_RETURN_RECONCILIATION_FAILED"):
        load_offline_bundle(bundle_path)

    with pytest.raises(ValueError, match="TOTAL_RETURN_RECONCILIATION_FAILED"):
        run_full_v1_evaluation(
            repo_root=WORKSPACE_ROOT,
            calendar=cal,
            mapping=replace(mapping, is_pit_verified=False),
            bars_by_code=bad_bars,
            cost_schedule=default_krx_research_cost_schedule(),
            provenance=prov_conflict,
            eval_start=mapping.valid_from,
            eval_end=explicit_bars["BM_KR"].index[-1],
            n_random_policies=1,
            bootstrap_reps=1,
        )

    # 2. Strict boundary also rejects conflicting PIT verification flags when TR is clean
    with pytest.raises(ValueError, match="INPUT_PROVENANCE_CONFLICT"):
        run_full_v1_evaluation(
            repo_root=WORKSPACE_ROOT,
            calendar=cal,
            mapping=replace(mapping, is_pit_verified=False),
            bars_by_code=explicit_bars,
            cost_schedule=default_krx_research_cost_schedule(),
            provenance=prov_conflict,
            eval_start=mapping.valid_from,
            eval_end=explicit_bars["BM_KR"].index[-1],
            n_random_policies=1,
            bootstrap_reps=1,
        )

    # 3. Diagnostic non-strict mode separates declared vs mechanical vs effective and forces INSUFFICIENT_EVIDENCE
    diag_loaded = load_offline_bundle(bundle_path, strict=False)
    diag_rep = run_full_v1_evaluation(
        repo_root=WORKSPACE_ROOT,
        **diag_loaded,
        n_random_policies=1,
        bootstrap_reps=1,
    )
    miss = set(diag_rep["verdict"]["missing_prerequisites"])
    assert "TOTAL_RETURN_RECONCILIATION_FAILED" in miss
    assert "TOTAL_RETURN_NOT_VERIFIED" in miss
    assert "CONFLICTING_MAPPING_PIT_VERIFICATION_FLAGS" in miss
    assert "SECTOR_MAPPING_NOT_PIT_VERIFIED" in miss
    assert diag_rep["input_audit"]["declared"]["total_return_verified"] is True
    assert diag_rep["input_audit"]["mechanical"]["all_total_return_reconciled"] is False
    assert diag_rep["input_audit"]["effective"]["total_return_verified"] is False

    # 4. Defaulted 0/1 corporate-action columns cannot be upgraded to verified even when provenance declares True
    prov_ca_declared = DataProvenanceManifest(
        manifest_id="r2-defaulted-ca",
        fetched_at="2026-11-07",
        provider="synthetic",
        total_return_verified=True,
        corporate_actions_verified=True,
        mapping_pit_verified=True,
        constituents_pit_verified_regions=(),
    )
    defaulted_rep = run_full_v1_evaluation(
        repo_root=WORKSPACE_ROOT,
        calendar=cal,
        mapping=mapping,  # is_pit_verified=True
        bars_by_code=bars,  # Created from OHLCV-only DataFrames -> SplitFactor/CashDividend defaulted!
        cost_schedule=default_krx_research_cost_schedule(),
        provenance=prov_ca_declared,
        eval_start=mapping.valid_from,
        eval_end=bars["BM_KR"].index[-1],
        n_random_policies=1,
        bootstrap_reps=1,
    )
    assert defaulted_rep["input_audit"]["declared"]["corporate_actions_verified"] is True
    assert defaulted_rep["input_audit"]["mechanical"]["all_explicit_corporate_action_columns"] is False
    assert defaulted_rep["input_audit"]["effective"]["corporate_actions_verified"] is False
    assert defaulted_rep["input_audit"]["effective"]["total_return_verified"] is False
    assert "CORPORATE_ACTIONS_DEFAULTED_COLUMNS_NOT_VERIFIED" in defaulted_rep["verdict"]["missing_prerequisites"]


def test_r3_pre_run_sealing_full_ledger_signal_preservation_and_standalone_replay(tmp_path: Path) -> None:
    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=180)
    src_dir = tmp_path / "src_bundle"
    src_bars = src_dir / "bars"
    src_bars.mkdir(parents=True)

    bar_files_rel: dict[str, str] = {}
    for code, df in bars.items():
        rel = f"bars/{code}.csv"
        df[["Open", "High", "Low", "Close", "Volume"]].to_csv(src_dir / rel)
        bar_files_rel[code] = rel

    bundle_doc = {
        "scheduled_sessions": [d.strftime("%Y-%m-%d") for d in cal.sessions],
        "benchmarks": {k: v.__dict__ for k, v in mapping.benchmarks.items()},
        "representatives": [spec.__dict__ for spec in mapping.representatives.values()],
        "mapping_meta": {
            "mapping_version": mapping.mapping_version,
            "freeze_date": mapping.freeze_date.strftime("%Y-%m-%d"),
            "valid_from": mapping.valid_from.strftime("%Y-%m-%d"),
            "valid_until": mapping.valid_until.strftime("%Y-%m-%d"),
            "is_pit_verified": True,
        },
        "bar_files": bar_files_rel,
        "provenance": {
            "manifest_id": "r3-replay-test",
            "fetched_at": "2026-11-07T12:00:00+09:00",
            "provider": "synthetic_offline",
            "total_return_verified": False,
            "corporate_actions_verified": False,
            "mapping_pit_verified": True,
            "constituents_pit_verified_regions": [],
        },
        "eval_start": mapping.valid_from.strftime("%Y-%m-%d"),
        "eval_end": bars["BM_KR"].index[-1].strftime("%Y-%m-%d"),
        "is_uninspected_oos": False,
    }
    bundle_path = src_dir / "bundle.json"
    bundle_path.write_text(json.dumps(bundle_doc, indent=2), encoding="utf-8")

    out_dir = tmp_path / "sealed_runs"
    rc = cli_main(
        [
            "--repo-root",
            str(WORKSPACE_ROOT),
            "--bundle",
            str(bundle_path),
            "--out-dir",
            str(out_dir),
            "--n-random",
            "3",
            "--bootstrap-reps",
            "10",
        ]
    )
    assert rc == 0
    assert (out_dir / "pre_run_seals.jsonl").is_file()

    reg = json.loads((out_dir / "attempt_registry.json").read_text(encoding="utf-8"))
    attempt_entry = reg["attempts"][0]
    assert attempt_entry["sealed_before_simulation_at_utc"] <= attempt_entry["recorded_at_utc"]
    run_dir = out_dir / attempt_entry["run_dir"]

    # Check complete replay artifact set
    assert (run_dir / "pre_run_input_seal.json").is_file()
    assert (run_dir / "evaluation_report.json").is_file()
    assert (run_dir / "artifact_manifest.json").is_file()
    assert (run_dir / "daily_signals.jsonl").is_file()
    assert (run_dir / "inputs_snapshot" / "bundle.json").is_file()
    assert (run_dir / "ledgers" / "B_sector_etf_30m_daily_ledger.csv").is_file()
    assert (run_dir / "ledgers" / "B_sector_etf_30m_closed_episodes.json").is_file()
    assert (run_dir / "ledgers" / "B_sector_etf_30m_open_positions.json").is_file()
    assert (run_dir / "ledgers" / "B_sector_etf_30m_order_events.json").is_file()

    rep_json = json.loads((run_dir / "evaluation_report.json").read_text(encoding="utf-8"))
    assert "models_engine_sha256" in rep_json["ledger_hashes"]
    assert "bar_data_hashes" in rep_json["ledger_hashes"]
    assert len(rep_json["ledger_hashes"]["source_file_hashes"]) >= len(bars)

    # Delete original source directory completely and verify standalone replay succeeds!
    shutil.rmtree(src_dir)
    assert not src_dir.exists()

    replay_res = replay_sealed_evaluation_run(run_dir, repo_root=WORKSPACE_ROOT)
    assert replay_res["replay_verified"] is True
    assert "B_sector_etf_30m" in replay_res["policies_verified"]

    rc_replay = cli_main(["--repo-root", str(WORKSPACE_ROOT), "--replay-run", str(run_dir)])
    assert rc_replay == 0

    # Tampering with a saved ledger file must be caught by artifact_manifest.json hash verification
    ledger_csv = run_dir / "ledgers" / "B_sector_etf_30m_daily_ledger.csv"
    ledger_csv.write_text(ledger_csv.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SEALED_ARTIFACT_HASH_MISMATCH"):
        replay_sealed_evaluation_run(run_dir, repo_root=WORKSPACE_ROOT)


def test_r4_short_calendar_vs_requested_24m_window_and_bool_json_schema() -> None:
    from dataclasses import replace

    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=180)
    extended_map = replace(mapping, valid_until=pd.Timestamp("2027-12-31"))
    short_sessions = cal.sessions_in_range(mapping.valid_from, bars["BM_KR"].index[-1])
    assert len(short_sessions) == 50
    short_calendar = TradingCalendar(short_sessions)

    prov = DataProvenanceManifest(
        "r4-audit", "2026-11-07", "synthetic", False, False, True, ()
    )
    report = run_full_v1_evaluation(
        repo_root=WORKSPACE_ROOT,
        calendar=short_calendar,
        mapping=extended_map,
        bars_by_code=bars,
        cost_schedule=default_krx_research_cost_schedule(),
        provenance=prov,
        eval_start=mapping.valid_from,
        eval_end=mapping.valid_from + pd.DateOffset(years=2),
        is_uninspected_oos=True,
        n_random_policies=1,
        bootstrap_reps=1,
    )
    win = report["window"]
    assert win["scheduled_sessions"] == 50
    assert type(win["meets_24_months"]) is bool  # Strict bool, never int 0/1!
    assert win["meets_24_months"] is False
    assert win["calendar_complete_for_requested_window"] is False
    assert win["months_approx"] < 3.0  # Actual evaluated span (~2.3m), NOT 24.016m!
    miss = set(report["verdict"]["missing_prerequisites"])
    assert "CALENDAR_INCOMPLETE_FOR_REQUESTED_WINDOW" in miss
    assert "WINDOW_SHORTER_THAN_24_MONTHS" in miss


def test_r5_non_frozen_mbb_and_random_protocol_forced_to_insufficient_evidence() -> None:
    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=180)
    prov = DataProvenanceManifest(
        "r5-audit", "2026-11-07", "synthetic", False, False, True, ()
    )
    reduced_report = run_full_v1_evaluation(
        repo_root=WORKSPACE_ROOT,
        calendar=cal,
        mapping=mapping,
        bars_by_code=bars,
        cost_schedule=default_krx_research_cost_schedule(),
        provenance=prov,
        eval_start=mapping.valid_from,
        eval_end=bars["BM_KR"].index[-1],
        is_uninspected_oos=True,
        n_random_policies=1,
        bootstrap_reps=1,
    )
    assert reduced_report["evaluation_mode"] == "DIAGNOSTIC_REDUCED_PROTOCOL_NO_ADOPTION"
    miss = set(reduced_report["verdict"]["missing_prerequisites"])
    assert "NON_FROZEN_MBB_PROTOCOL_DIAGNOSTIC_ONLY" in miss
    assert "NON_FROZEN_RANDOM_BENCHMARK_PROTOCOL_DIAGNOSTIC_ONLY" in miss
    assert reduced_report["verdict"]["verdict_code"] == "INSUFFICIENT_EVIDENCE"

    # Standalone classifier with non-frozen MBB or missing Random-1000 metadata must NEVER return ELIGIBLE
    prov_true = DataProvenanceManifest(
        "r5-prov", "2026-11-07", "synthetic", True, True, True, ("KR",)
    )
    verdict_one_boot = classify_v1_verdict(
        is_uninspected_oos=True,
        meets_24_months=True,
        closed_episodes_count=65,
        provenance=prov_true,
        b_cum_net_return=0.25,
        b_ann_net_return=0.118,
        simple60_ann_net_return=0.08,
        mbb_result={
            "valid": True,
            "n_boot": 1,
            "block_len": 1,
            "seed": 7,
            "ci_lower": 0.001,
            "ci_upper": 0.002,
            "mean_daily_diff": 0.0015,
        },
        b_mdd=0.10,
        b_profit_factor=1.5,
        b_profit_factor_status="DEFINED",
        b_cost2x_cum_net_return=0.20,
        simple60_cost2x_cum_net_return=0.15,
        b_delay1d_cum_net_return=0.19,
        simple60_delay1d_cum_net_return=0.14,
        random_p95_cum_net_return=0.18,
    )
    assert verdict_one_boot["verdict_code"] == "INSUFFICIENT_EVIDENCE"
    assert "NON_FROZEN_MBB_PROTOCOL_DIAGNOSTIC_ONLY" in verdict_one_boot["missing_prerequisites"]
    assert "RANDOM_BENCHMARK_PROTOCOL_METADATA_MISSING" in verdict_one_boot["missing_prerequisites"]


def test_r6_and_r7_e1_journal_chronology_canonical_dedup_late_violation_and_share_ledger(tmp_path: Path) -> None:
    from dataclasses import replace

    cal = TradingCalendar(pd.bdate_range("2023-01-02", "2026-12-31", freq="B"))

    # 1. R6: Observation/fill before signal_date MUST be rejected
    pre_signal_entry = E1JournalRecord(
        record_id="r6-pre",
        action_type="ENTRY",
        signal_date="2026-03-06",
        check_session_date="2026-03-09",
        code="ETF_01",
        region="KR",
        sector="SEMI",
        signal_close=10_400.0,
        signal_sma20=10_000.0,
        signal_atr20=200.0,
        disposition="FILLED",
        observed_at="2026-03-01T10:00:00+09:00",
        observed_price=10_450.0,
        ordered_at="2026-03-01T10:01:00+09:00",
        order_qty=10,
        filled_at="2026-03-01T10:01:05+09:00",
        filled_qty=10,
        filled_price=10_450.0,
    )
    with pytest.raises(ValueError, match="E1_OBSERVATION_ON_OR_BEFORE_SIGNAL_DATE"):
        pre_signal_entry.validate(calendar=cal)

    # 2. R6: Same signal_date encoded as "2026-03-06T00:00:00" MUST be rejected as duplicate single-check
    jpath = tmp_path / "r6.jsonl"
    valid_entry = replace(
        pre_signal_entry,
        record_id="r6-ok-1",
        observed_at="2026-03-09T10:00:00+09:00",
        ordered_at="2026-03-09T10:01:00+09:00",
        filled_at="2026-03-09T10:01:05+09:00",
    )
    append_e1_journal_record(jpath, valid_entry, calendar=cal)
    with pytest.raises(ValueError, match="SECOND_ENTRY_ATTEMPT_AFTER_SINGLE_CHECK_PROHIBITED"):
        append_e1_journal_record(
            jpath,
            replace(valid_entry, record_id="r6-dup-iso", signal_date="2026-03-06T00:00:00"),
            calendar=cal,
        )

    # 3. R6: Late observation/order/fill on 2026-03-10 for check_session_date 2026-03-09 is preserved
    #    as a real audit record with PROTOCOL_VIOLATION_PRESERVED, NOT counted as compliant!
    late_entry = E1JournalRecord(
        record_id="r6-late",
        action_type="ENTRY",
        signal_date="2026-03-13",
        check_session_date="2026-03-16",
        code="ETF_02",
        region="KR",
        sector="BIO",
        signal_close=10_400.0,
        signal_sma20=10_000.0,
        signal_atr20=200.0,
        disposition="FILLED",
        observed_at="2026-03-17T10:00:00+09:00",  # 1 session late!
        observed_price=10_450.0,
        ordered_at="2026-03-17T10:01:00+09:00",
        order_qty=10,
        filled_at="2026-03-17T10:01:05+09:00",
        filled_qty=10,
        filled_price=10_450.0,
    )
    append_e1_journal_record(jpath, late_entry, calendar=cal)
    loaded_r6 = load_e1_journal(jpath, calendar=cal)
    sum_r6 = summarize_e1_journal(loaded_r6, calendar=cal)
    assert sum_r6["real_entry_fills"] == 2
    assert sum_r6["compliant_entry_fills"] == 1
    assert sum_r6["violation_entry_fills"] == 1

    # 4. R7: EXIT_FILLED with order_qty=0, filled_qty=10 MUST raise E1_FILLED_QTY_EXCEEDS_ORDER_QTY
    bad_qty_exit = E1JournalRecord(
        record_id="r7-bad-qty",
        action_type="EXIT",
        signal_date="2026-03-20",
        check_session_date="2026-03-23",
        code="ETF_01",
        region="KR",
        sector="SEMI",
        signal_close=10_400.0,
        signal_sma20=10_000.0,
        signal_atr20=200.0,
        disposition="EXIT_FILLED",
        observed_at="2026-03-23T10:00:00+09:00",
        observed_price=10_500.0,
        ordered_at="2026-03-23T10:01:00+09:00",
        order_qty=0,
        filled_at="2026-03-23T10:01:05+09:00",
        filled_qty=10,
        filled_price=10_500.0,
    )
    with pytest.raises(ValueError, match="E1_FILLED_QTY_EXCEEDS_ORDER_QTY"):
        bad_qty_exit.validate()

    # 5. R7: 60 orphan EXIT_FILLED rows (with valid order_qty=10, filled_qty=10 but 0 entries) over 27m
    orphan_exits: list[E1JournalRecord] = []
    for i in range(60):
        sig_d = cal.sessions[10 + i * 10]
        chk_d = cal.next_session_after(sig_d, 1)
        assert chk_d is not None
        ts_iso = f"{chk_d.strftime('%Y-%m-%d')}T10:00:00+09:00"
        orphan_exits.append(
            E1JournalRecord(
                record_id=f"orphan-{i}",
                action_type="EXIT",
                signal_date=sig_d.strftime("%Y-%m-%d"),
                check_session_date=chk_d.strftime("%Y-%m-%d"),
                code="ETF_ORPHAN",
                region="KR",
                sector="SEMI",
                signal_close=10_400.0,
                signal_sma20=10_000.0,
                signal_atr20=200.0,
                disposition="EXIT_FILLED",
                observed_at=ts_iso,
                observed_price=10_450.0,
                ordered_at=ts_iso,
                order_qty=10,
                filled_at=ts_iso,
                filled_qty=10,
                filled_price=10_450.0,
            )
        )
    with pytest.raises(ValueError, match="ORPHAN_EXIT_WITHOUT_OPEN_POSITION"):
        append_e1_journal_record(tmp_path / "orphan.jsonl", orphan_exits[0], calendar=cal)

    orphan_summary = summarize_e1_journal(orphan_exits, calendar=cal)
    assert orphan_summary["ledger_consistent"] is False
    assert orphan_summary["closed_episode_count"] == 0
    assert orphan_summary["statistical_evaluation_eligible"] is False
    assert orphan_summary["profitability_claim_allowed"] is False

    # 6. R7: Partial exits only complete 1 episode when remaining shares hit 0, and profitability_claim_allowed stays False
    p_exit_1 = E1JournalRecord(
        record_id="r7-part-1",
        action_type="EXIT",
        signal_date="2026-03-19",
        check_session_date="2026-03-20",
        code="ETF_01",
        region="KR",
        sector="SEMI",
        signal_close=10_600.0,
        signal_sma20=10_200.0,
        signal_atr20=200.0,
        disposition="EXIT_FILLED",
        observed_at="2026-03-20T10:00:00+09:00",
        observed_price=10_600.0,
        ordered_at="2026-03-20T10:01:00+09:00",
        order_qty=4,
        filled_at="2026-03-20T10:01:05+09:00",
        filled_qty=4,
        filled_price=10_600.0,
    )
    p_exit_2 = replace(
        p_exit_1,
        record_id="r7-part-2",
        signal_date="2026-03-20",
        check_session_date="2026-03-23",
        observed_at="2026-03-23T10:00:00+09:00",
        ordered_at="2026-03-23T10:01:00+09:00",
        filled_at="2026-03-23T10:01:05+09:00",
        order_qty=6,
        filled_qty=6,
    )
    partial_ledger = reconcile_e1_position_ledger([valid_entry, p_exit_1], calendar=cal)
    assert len(partial_ledger["closed_episodes"]) == 0
    assert partial_ledger["open_episodes"][0]["remaining_qty"] == 6

    full_ledger = reconcile_e1_position_ledger([valid_entry, p_exit_1, p_exit_2], calendar=cal)
    assert full_ledger["ledger_consistent"] is True
    assert len(full_ledger["closed_episodes"]) == 1
    assert len(full_ledger["open_episodes"]) == 0


def test_r8_held_missing_or_halted_bars_emit_events_track_staleness_and_withhold_mdd_verdict() -> None:
    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=180)
    sessions = cal.sessions_in_range(mapping.valid_from, bars["BM_KR"].index[-1])
    start = next(d for d in sessions if cal.is_week_end_session(d))
    entry_day = cal.next_session_after(start)
    missing_day = cal.next_session_after(entry_day)
    end_day = cal.next_session_after(missing_day)
    assert entry_day is not None and missing_day is not None and end_day is not None

    code = "KR_SEC_01"
    bars[code] = _flat_or_curve_frame(bars[code].index, 10_000.0)
    bars[code] = bars[code].drop(index=[missing_day, end_day])

    def _selector(dt, _snap, _slots, _excl_sec, _excl_codes):
        if dt == start:
            return [
                {
                    "code": code,
                    "name": code,
                    "region": "KR",
                    "sector": "SECTOR_KR_1",
                    "instrument_kind": "EQUITY_ETF",
                    "signal_close": 10_000.0,
                    "signal_sma20": 10_000.0,
                    "signal_atr20": 500.0,
                }
            ], {}
        return [], {}

    res = run_e0_simulation(
        "r8-missing-held",
        cal,
        mapping,
        bars,
        default_krx_research_cost_schedule(),
        start,
        end_day,
        _selector,
    )
    endpos = res.open_positions_at_end[0]
    assert endpos["mark_is_stale"] is True
    assert endpos["stale_sessions"] == 2
    assert endpos["last_mark_date"] == entry_day.strftime("%Y-%m-%d")
    assert endpos["mark_status"] == "CARRIED_MISSING_BAR"
    assert endpos["total_missing_bar_sessions"] == 2

    missing_events = [
        ev for ev in res.order_events if ev["event"] == "HELD_MARK_MISSING_BAR"
    ]
    assert [ev["date"] for ev in missing_events] == [
        missing_day.strftime("%Y-%m-%d"),
        end_day.strftime("%Y-%m-%d"),
    ]
    assert all(ev["exit_signal_evaluation_skipped"] is True for ev in missing_events)

    summary = summarize_portfolio_result(res, start, end_day)
    assert summary["mark_quality"]["mdd_quality_status"] == "UNVERIFIED_CONTAINS_STALE_OR_MISSING_HELD_MARKS"
    assert summary["mark_quality"]["missing_held_bar_sessions"] == 2

    mbb_ok = {
        "valid": True,
        "block_len": 20,
        "n_boot": 10_000,
        "seed": FIXED_RESEARCH_SEED,
        "alpha": 0.05,
        "mean_daily_diff": 0.001,
        "ci_lower": 0.0004,
        "ci_upper": 0.0016,
    }
    rand_ok = {
        "n_policies": DEFAULT_RANDOM_POLICIES,
        "seed": FIXED_RESEARCH_SEED,
        "copies_actual_holding_periods": False,
        "p95_cumulative_net_return": 0.12,
    }
    prov_ok = DataProvenanceManifest("ok", "2026-11-07", "syn", True, True, True, ())
    verdict = classify_v1_verdict(
        is_uninspected_oos=True,
        meets_24_months=True,
        closed_episodes_count=65,
        provenance=prov_ok,
        b_cum_net_return=0.25,
        b_ann_net_return=0.118,
        simple60_ann_net_return=0.08,
        mbb_result=mbb_ok,
        b_mdd=0.09,
        b_profit_factor=1.5,
        b_profit_factor_status="DEFINED",
        b_cost2x_cum_net_return=0.20,
        simple60_cost2x_cum_net_return=0.15,
        b_delay1d_cum_net_return=0.19,
        simple60_delay1d_cum_net_return=0.14,
        random_benchmark=rand_ok,
        b_mdd_quality_status=summary["mark_quality"]["mdd_quality_status"],
    )
    assert verdict["verdict_code"] == "INSUFFICIENT_EVIDENCE"
    assert "UNOBSERVED_OR_STALE_HELD_MARKS_IN_MDD" in verdict["missing_prerequisites"]


def test_r9_common_pit_scope_matches_b_reference_to_c_and_withholds_insufficient_scope() -> None:
    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=180)
    dates = bars["BM_KR"].index

    members: dict[tuple[str, str], list[tuple[pd.Timestamp, tuple[ConstituentItem, ...]]]] = {}
    for (region, sector), spec in mapping.representatives.items():
        if region != "KR":
            continue
        scode = f"STOCK_{sector}"
        members[(region, sector)] = [(dates[0], (ConstituentItem(scode, scode, "KOSPI"),))]
        bars[scode] = _flat_or_curve_frame(
            dates,
            bars[spec.code]["Close"].to_numpy(dtype=float) * np.linspace(1.0, 1.1, len(dates)),
        )

    pit_kr = PITConstituentTable(members, is_pit_verified=True)
    prov = DataProvenanceManifest(
        "r9-audit", "2026-11-07", "synthetic", False, False, True, ("KR",)
    )
    report = run_full_v1_evaluation(
        repo_root=WORKSPACE_ROOT,
        calendar=cal,
        mapping=mapping,
        bars_by_code=bars,
        cost_schedule=default_krx_research_cost_schedule(),
        provenance=prov,
        eval_start=mapping.valid_from,
        eval_end=dates[-1],
        pit_constituents=pit_kr,
        n_random_policies=1,
        bootstrap_reps=1,
    )
    comp = report["stock_comparisons"]["c_vs_b"]
    assert comp["comparison_reference_policy"] == "B_sector_etf_pit_matched_30m"
    assert comp["common_pit_regions"] == ["KR"]
    assert comp["unimplemented_regions_no_pit"] == ["US"]
    # Primary B still enters US, while B_pit_matched, C, and A-matched strictly trade within KR!
    entered = comp["entered_regions_by_policy"]
    assert "US" in entered["b_full_multiregion_30m"]
    assert set(entered["b_pit_matched_30m"]) <= {"KR"}
    assert set(entered["c_30m"]) <= {"KR"}
    assert set(entered["a_matched_30m"]) <= {"KR"}

    # Insufficient common PIT scope (< 5 sectors in KR) -> NOT_IMPLEMENTED_INSUFFICIENT_COMMON_PIT_SCOPE
    tiny_members = {k: members[k] for k in list(sorted(members.keys()))[:2]}
    pit_tiny = PITConstituentTable(tiny_members, is_pit_verified=True)
    rep_tiny = run_full_v1_evaluation(
        repo_root=WORKSPACE_ROOT,
        calendar=cal,
        mapping=mapping,
        bars_by_code=bars,
        cost_schedule=default_krx_research_cost_schedule(),
        provenance=prov,
        eval_start=mapping.valid_from,
        eval_end=dates[-1],
        pit_constituents=pit_tiny,
        n_random_policies=1,
        bootstrap_reps=1,
    )
    assert rep_tiny["stock_comparisons"]["c_vs_b"]["status"] == "NOT_IMPLEMENTED_INSUFFICIENT_COMMON_PIT_SCOPE"


def test_rr1_sparse_calendar_omitting_observed_bar_sessions_rejected_while_holidays_pass() -> None:
    dates = pd.bdate_range("2024-01-01", "2026-01-05")
    assert len(dates) == 526
    sparse = TradingCalendar([d for d in dates if d.weekday() in (0, 2, 4)], calendar_id="sparse-mwf")
    assert len(sparse.sessions) == 316
    bm_bars = _flat_or_curve_frame(dates, 10_000.0)
    bm_spec = {"KR": RegionalBenchmarkSpec("KR", "BM", "BM", "KRW", False)}

    # 1. T-149 RR1 probe: 210 Tue/Thu completed benchmark sessions omitted from calendar -> REJECTED
    res_sparse = sparse.audit_window_coverage(dates[0], dates[-1], {"BM": bm_bars}, bm_spec)
    assert res_sparse["calendar_complete_for_requested_window"] is False
    assert res_sparse["meets_24_months"] is False
    assert res_sparse["unscheduled_benchmark_bar_sessions"] == 210
    assert "CALENDAR_OMITS_OBSERVED_BENCHMARK_SESSIONS" in res_sparse["calendar_issues"]
    assert res_sparse["price_data_issues"] == []

    # 2. Converse distinction: complete calendar + 5 missing benchmark price bars -> price issue, NOT calendar omission
    full_cal = TradingCalendar(dates, calendar_id="full-bdate", is_exchange_verified=True)
    bars_missing_5 = bm_bars.drop(index=list(dates[50:55]))
    res_missing_px = full_cal.audit_window_coverage(
        dates[0], dates[-1], {"BM": bars_missing_5}, bm_spec
    )
    assert res_missing_px["calendar_complete_for_requested_window"] is True
    assert res_missing_px["calendar_issues"] == []
    assert res_missing_px["unscheduled_benchmark_bar_sessions"] == 0
    assert res_missing_px["benchmark_session_coverage"]["KR"]["missing_or_halted_sessions"] == 5
    assert "BENCHMARK_PRICE_MISSING_OR_HALTED_ON_SCHEDULED_SESSIONS" in res_missing_px["price_data_issues"]

    # 3. Approved exchange holidays absent from BOTH calendar and price bars remain valid
    holidays = {
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2024-02-09"),
        pd.Timestamp("2024-02-12"),
        pd.Timestamp("2024-09-16"),
        pd.Timestamp("2024-09-17"),
        pd.Timestamp("2024-09-18"),
        pd.Timestamp("2025-01-28"),
        pd.Timestamp("2025-01-29"),
        pd.Timestamp("2025-01-30"),
        pd.Timestamp("2026-01-01"),
    }
    holiday_sessions = [d for d in dates if d not in holidays]
    holiday_cal = TradingCalendar(
        holiday_sessions,
        calendar_id="krx-holiday-syn",
        source="synthetic_exchange_schedule",
        covered_start=dates[0],
        covered_end=dates[-1],
        is_exchange_verified=True,
    )
    holiday_bars = _flat_or_curve_frame(pd.DatetimeIndex(holiday_sessions), 10_000.0)
    res_holidays = holiday_cal.audit_window_coverage(
        dates[0], dates[-1], {"BM": holiday_bars}, bm_spec
    )
    assert res_holidays["calendar_complete_for_requested_window"] is True
    assert res_holidays["meets_24_months"] is True
    assert res_holidays["calendar_issues"] == []
    assert res_holidays["price_data_issues"] == []

    # 4. End-to-end evaluator marks CALENDAR_OMITS_OBSERVED_TRADING_SESSIONS in verdict prerequisites
    cal_fix, mapping_fix, bars_fix = _build_multi_sector_fixture(n_sessions=180)
    mwf_fix = TradingCalendar([d for d in cal_fix.sessions if d.weekday() in (0, 2, 4)])
    prov = DataProvenanceManifest("rr1-eval", "2026-11-07", "synthetic", False, False, True, ())
    rep = run_full_v1_evaluation(
        repo_root=WORKSPACE_ROOT,
        calendar=mwf_fix,
        mapping=mapping_fix,
        bars_by_code=bars_fix,
        cost_schedule=default_krx_research_cost_schedule(),
        provenance=prov,
        eval_start=mapping_fix.valid_from,
        eval_end=bars_fix["BM_KR"].index[-1],
        n_random_policies=1,
        bootstrap_reps=1,
    )
    assert rep["window"]["calendar_complete_for_requested_window"] is False
    assert rep["window"]["unscheduled_benchmark_bar_sessions"] > 0
    assert "CALENDAR_OMITS_OBSERVED_TRADING_SESSIONS" in rep["verdict"]["missing_prerequisites"]
    assert "CALENDAR_INCOMPLETE_FOR_REQUESTED_WINDOW" in rep["verdict"]["missing_prerequisites"]


def test_rr2_e1_saved_violation_preserved_on_default_reload_no_calendar_and_reclassification_audit(
    tmp_path: Path,
) -> None:
    cal = TradingCalendar(
        pd.bdate_range("2026-03-06", "2026-03-20"),
        calendar_id="krx-2026-03",
        source="krx_schedule",
        is_exchange_verified=True,
    )
    record = E1JournalRecord(
        record_id="late",
        action_type="ENTRY",
        signal_date="2026-03-06",
        check_session_date="2026-03-10",  # Expected Monday 2026-03-09!
        code="ETF",
        region="KR",
        sector="S",
        signal_close=10_400.0,
        signal_sma20=10_000.0,
        signal_atr20=200.0,
        disposition="FILLED",
        observed_at="2026-03-10T10:00:00+09:00",
        observed_price=10_450.0,
        ordered_at="2026-03-10T10:01:00+09:00",
        order_qty=1,
        filled_at="2026-03-10T10:01:01+09:00",
        filled_qty=1,
        filled_price=10_450.0,
    )
    p = tmp_path / "journal.jsonl"
    append_e1_journal_record(p, record, calendar=cal)

    # 1. Default reload (restoring linked sidecar calendar) preserves violation: 0 compliant, 1 violation
    summary_default = summarize_e1_journal(load_e1_journal(p))
    assert summary_default["compliant_entry_fills"] == 0
    assert summary_default["violation_entry_fills"] == 1
    assert summary_default["protocol_violation_record_count"] == 1

    # 2. Even if sidecar file is missing (standalone JSONL copy, calendar=None), violation is NEVER upgraded to COMPLIANT
    sidecar = p.with_name(p.name + ".calendar.json")
    assert sidecar.is_file()
    sidecar.unlink()
    summary_no_sidecar = summarize_e1_journal(load_e1_journal(p))
    assert summary_no_sidecar["compliant_entry_fills"] == 0
    assert summary_no_sidecar["violation_entry_fills"] == 1
    assert summary_no_sidecar["protocol_violation_record_count"] == 1
    assert (
        "ENTRY_CHECK_SESSION_LATER_THAN_NEXT_TRADING_SESSION"
        in summary_no_sidecar["compliance_audit_trail"][0]["effective_protocol_violations"]
    )

    # 3. Re-evaluating with another calendar where 2026-03-09 is absent keeps prior violation auditable
    alt_cal = TradingCalendar(
        [d for d in pd.bdate_range("2026-03-06", "2026-03-20") if d != pd.Timestamp("2026-03-09")],
        calendar_id="alt-holiday-0309",
    )
    summary_alt = summarize_e1_journal(load_e1_journal(p, calendar=alt_cal), calendar=alt_cal)
    assert summary_alt["prior_recorded_violation_record_count"] == 1
    trail0 = summary_alt["compliance_audit_trail"][0]
    assert trail0["recorded_compliance_status"] == "PROTOCOL_VIOLATION_PRESERVED"
    assert trail0["recorded_protocol_violations"] == [
        "ENTRY_CHECK_SESSION_LATER_THAN_NEXT_TRADING_SESSION"
    ]
    assert trail0["recorded_expected_next_session_date"] == "2026-03-09"
    assert trail0["reevaluated_compliance_status"] == "COMPLIANT"
    assert trail0["calendar_reclassification_changed"] is True

    # 4. Calendarless save + reload marks record UNVERIFIED_NO_CALENDAR (compliant_entry_fills == 0, violation == 0)
    p_unver = tmp_path / "unverified.jsonl"
    rec_unver = E1JournalRecord(
        record_id="unver-1",
        action_type="ENTRY",
        signal_date="2026-03-06",
        check_session_date="2026-03-09",
        code="ETF2",
        region="KR",
        sector="S",
        signal_close=10_400.0,
        signal_sma20=10_000.0,
        signal_atr20=200.0,
        disposition="FILLED",
        observed_at="2026-03-09T10:00:00+09:00",
        observed_price=10_450.0,
        ordered_at="2026-03-09T10:01:00+09:00",
        order_qty=1,
        filled_at="2026-03-09T10:01:01+09:00",
        filled_qty=1,
        filled_price=10_450.0,
    )
    append_e1_journal_record(p_unver, rec_unver)
    sum_unver = summarize_e1_journal(load_e1_journal(p_unver))
    assert sum_unver["compliant_entry_fills"] == 0
    assert sum_unver["unverified_entry_fills"] == 1
    assert sum_unver["violation_entry_fills"] == 0
    assert sum_unver["compliance_audit_trail"][0]["effective_compliance_status"] == "UNVERIFIED_NO_CALENDAR"


def test_rr3_pre_sealed_handle_immutability_seed_mismatch_rejection_and_idempotent_retry(
    tmp_path: Path,
) -> None:
    cal, mapping, bars = _build_multi_sector_fixture(n_sessions=160)
    prov = DataProvenanceManifest("rr3-audit", "2026-11-07", "synthetic", False, False, True, ())
    inputs = dict(
        calendar=cal,
        mapping=mapping,
        bars_by_code=bars,
        cost_schedule=default_krx_research_cost_schedule(),
        provenance=prov,
    )
    validated = validate_research_inputs(**inputs)
    config = dict(
        eval_start=mapping.valid_from,
        eval_end=bars["BM_KR"].index[-1],
        n_random_policies=1,
        bootstrap_reps=1,
    )
    out = tmp_path / "runs"
    handle = seal_pre_run_attempt(
        out_root=out,
        repo_root=WORKSPACE_ROOT,
        validated_inputs=validated,
        **config,
    )

    report_seed_default = run_full_v1_evaluation(repo_root=WORKSPACE_ROOT, **inputs, **config)
    report_seed_7 = run_full_v1_evaluation(repo_root=WORKSPACE_ROOT, **inputs, **config, seed=7)

    # 1. Even BEFORE handle is finalized, attaching a different-seed report MUST fail without writing report
    with pytest.raises(ValueError, match="PRE_RUN_SEAL_MISMATCH_WITH_EVALUATION_REPORT"):
        seal_and_register_evaluation_run(out, report_seed_7, pre_sealed_run=handle)
    assert not (Path(handle["run_dir"]) / "evaluation_report.json").exists()
    assert not (out / "attempt_registry.json").exists()

    # 2. Finalize handle with matching report (seed=20261107)
    saved1 = seal_and_register_evaluation_run(out, report_seed_default, pre_sealed_run=handle)
    run_dir1 = Path(saved1["run_dir"])
    files_snapshot_1 = {
        p.relative_to(run_dir1).as_posix(): p.read_bytes()
        for p in sorted(run_dir1.rglob("*"))
        if p.is_file()
    }
    registry_bytes_1 = (out / "attempt_registry.json").read_bytes()

    # 3. Reusing finalized handle with seed=7 report MUST fail and leave all files & registry byte-identical
    with pytest.raises(ValueError, match="PRE_RUN_SEAL_MISMATCH_WITH_EVALUATION_REPORT"):
        seal_and_register_evaluation_run(out, report_seed_7, pre_sealed_run=handle)
    assert (out / "attempt_registry.json").read_bytes() == registry_bytes_1
    assert {
        p.relative_to(run_dir1).as_posix(): p.read_bytes()
        for p in sorted(run_dir1.rglob("*"))
        if p.is_file()
    } == files_snapshot_1

    # 4. Mutating report payload while keeping same pre_run_seal MUST fail with ATTEMPT_ALREADY_FINALIZED
    forged_same_seal = dict(report_seed_default)
    forged_same_seal["spec_note_tamper"] = "mutated_after_finalization"
    with pytest.raises(FileExistsError, match="ATTEMPT_ALREADY_FINALIZED_NEW_ATTEMPT_NUMBER_REQUIRED"):
        seal_and_register_evaluation_run(out, forged_same_seal, pre_sealed_run=handle)
    assert (out / "attempt_registry.json").read_bytes() == registry_bytes_1

    # 5. Exact same report retry on handle is idempotent: no file overwrite, no duplicate attempt_number [1, 1]
    retry_saved = seal_and_register_evaluation_run(out, report_seed_default, pre_sealed_run=handle)
    assert retry_saved["idempotent_retry"] is True
    assert retry_saved["attempt_number"] == 1
    assert (out / "attempt_registry.json").read_bytes() == registry_bytes_1
    assert {
        p.relative_to(run_dir1).as_posix(): p.read_bytes()
        for p in sorted(run_dir1.rglob("*"))
        if p.is_file()
    } == files_snapshot_1

    # 6. Registering seed=7 as a separate attempt allocates attempt_number=2 in a distinct directory
    saved2 = seal_and_register_evaluation_run(out, report_seed_7)
    assert saved2["attempt_number"] == 2
    assert saved2["run_dir"] != saved1["run_dir"]
    reg_after = json.loads((out / "attempt_registry.json").read_text(encoding="utf-8"))
    assert [a["attempt_number"] for a in reg_after["attempts"]] == [1, 2]

