"""Offline adversarial probes of the frozen T-147 implementation.

These assertions document observed defects, NOT desired regression-test behavior.
Run from workspace: python docs/tasks/T-148-repro.py
No market downloads, orders, production data writes, or implementation edits.
"""
from __future__ import annotations

import json
import runpy
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "quant-research"))
from research.sector_v1.models import (
    ConstituentItem, DataProvenanceManifest, PITConstituentTable,
    TradingCalendar, clean_and_validate_bars, default_krx_research_cost_schedule,
    verify_total_return_reconciliation,
)
from research.sector_v1.signals import (
    DailySignalSnapshot, SectorObservationRow, evaluate_daily_snapshot,
    select_weekly_stock_candidates_c, select_weekly_stock_candidates_a_matched,
)
from research.sector_v1.execution_e0 import run_e0_simulation
from research.sector_v1.evaluator import (
    run_full_v1_evaluation, seal_and_register_evaluation_run, classify_v1_verdict,
)
from research.sector_v1.journal_e1 import (
    E1JournalRecord, append_e1_journal_record, load_e1_journal, summarize_e1_journal,
)
from scripts.run_t145_sector_research import load_offline_bundle

FIXTURE = runpy.run_path(str(ROOT / "quant-research/tests/test_t145_sector_v1.py"))
build_fixture = FIXTURE["_build_multi_sector_fixture"]
OUT = {}


def frame(dates, close, volume=200_000):
    c = np.broadcast_to(np.asarray(close, dtype=float), (len(dates),)).copy()
    return clean_and_validate_bars(pd.DataFrame({
        "Open": c, "High": c * 1.05, "Low": c * .95,
        "Close": c, "Volume": volume,
    }, index=dates), "synthetic")


def provenance():
    return DataProvenanceManifest("audit", "2026-11-07", "synthetic",
                                  True, True, True, ("KR",))


def stock_ablation():
    cal, mapping, bars = build_fixture(180)
    dates = bars["BM_KR"].index
    t = dates[-1]
    # Stock outperforms a positive sector by a stable 0.1% each session.
    # It meets C but fails A's extra improvement requirement.
    sector = 10_000 * 1.001 ** np.arange(len(dates))
    stock = 10_000 * 1.002 ** np.arange(len(dates))
    for code in bars:
        bars[code] = frame(dates, 10_000)
    bars["KR_SEC_01"] = frame(dates, sector)
    bars["STOCK"] = frame(dates, stock)
    # Positive but *decreasing* stock RS against market removes floating-point ambiguity.
    bench = np.full(len(dates), 10_000.)
    bench[-5:] *= np.linspace(1.001, 1.01, 5)
    bars["BM_KR"] = frame(dates, bench)
    snap = evaluate_daily_snapshot(t, mapping, bars)
    # Force only sector qualification, keeping actual stock score calculations untouched.
    rows = tuple(replace(r, is_entry_qualified=(r.code == "KR_SEC_01"),
                         regime_bullish=True, region_coverage_ok=True) for r in snap.observations)
    snap = replace(snap, observations=rows, region_status={"KR": "ACTIVE", "US": "ACTIVE"})
    pit = PITConstituentTable({("KR", "SECTOR_KR_1"): [(dates[0], (
        ConstituentItem("STOCK", "Stock", "KOSPI"),))]}, True)
    c, _ = select_weekly_stock_candidates_c(t, snap, pit, bars, 3, set(), set(), ("KR",))
    a = select_weekly_stock_candidates_a_matched(t, snap, mapping, pit, bars, 3, set(), ("KR",))
    assert [x["code"] for x in c] == ["STOCK"] and a == []
    OUT["R1_a_matched_changes_more_than_sector_gate"] = {"C": [x["code"] for x in c], "A": a}


def bundle_and_artifacts(tmp):
    cal, mapping, bars = build_fixture(180)
    # Inject finite but inconsistent vendor total-return prices.
    bad_code = "KR_SEC_01"
    bars[bad_code].loc[bars[bad_code].index[-1], "TotalReturnClose"] *= 2
    files = {}
    for code, df in bars.items():
        name = f"{code}.csv"
        df.to_csv(tmp / name)
        files[code] = name
    doc = {
        "scheduled_sessions": [str(d.date()) for d in cal.sessions],
        "benchmarks": {k: v.__dict__ for k, v in mapping.benchmarks.items()},
        "representatives": [x.__dict__ for x in mapping.representatives.values()],
        "mapping_meta": {
            "mapping_version": "audit", "freeze_date": str(mapping.freeze_date.date()),
            "valid_from": str(mapping.valid_from.date()),
            "valid_until": str(mapping.valid_until.date()), "is_pit_verified": False,
        },
        "bar_files": files,
        "provenance": {**provenance().__dict__},
        "eval_start": str(mapping.valid_from.date()),
        "eval_end": str(bars["BM_KR"].index[-1].date()),
        "is_uninspected_oos": True,
    }
    bundle = tmp / "bundle.json"
    bundle.write_text(json.dumps(doc), encoding="utf-8")
    loaded = load_offline_bundle(bundle)
    assert not verify_total_return_reconciliation(loaded["bars_by_code"][bad_code])
    assert loaded["provenance"].total_return_verified
    assert not loaded["mapping"].is_pit_verified and loaded["provenance"].mapping_pit_verified
    report = run_full_v1_evaluation(repo_root=ROOT, **loaded, n_random_policies=1, bootstrap_reps=1)
    missing = report["verdict"]["missing_prerequisites"]
    assert "TOTAL_RETURN_NOT_VERIFIED" not in missing and "SECTOR_MAPPING_NOT_PIT_VERIFIED" not in missing
    OUT["R2_bad_total_return_and_conflicting_pit_flags_accepted"] = {
        "reconciliation": False, "declared_verified": True, "missing_prerequisites": missing,
    }
    reg = seal_and_register_evaluation_run(tmp / "runs", report)
    saved_files = [p.name for p in Path(reg["run_dir"]).iterdir()]
    assert saved_files == ["evaluation_report.json"]
    assert "daily_ledger" not in json.dumps(report) and "order_events" not in json.dumps(report)
    OUT["R3_run_drops_replay_artifacts"] = {"saved_files": saved_files,
        "closed_count": report["primary_b_30m"]["episode_metrics"]["closed_episode_count"],
        "missing": ["raw bundle/bars", "daily_ledger", "closed_episodes", "order_events", "daily signals"]}

    # Requested window != calendar actually evaluated; no coverage check.
    extended = replace(mapping, valid_until=pd.Timestamp("2027-12-31"))
    short_calendar = TradingCalendar(cal.sessions_in_range(mapping.valid_from, bars["BM_KR"].index[-1]))
    window_report = run_full_v1_evaluation(repo_root=ROOT, calendar=short_calendar,
        mapping=extended, bars_by_code=bars, cost_schedule=loaded["cost_schedule"],
        provenance=provenance(), eval_start=mapping.valid_from,
        eval_end=mapping.valid_from + pd.DateOffset(years=2), is_uninspected_oos=True,
        n_random_policies=1, bootstrap_reps=1)
    assert window_report["window"]["meets_24_months"] and window_report["window"]["scheduled_sessions"] == 50
    OUT["R4_short_calendar_marked_24_months"] = window_report["window"]
    OUT["R5_nonfrozen_statistics_not_withheld"] = {
        "n_random": report["random_policy_benchmark"]["n_policies"],
        "bootstrap_reps": report["mbb_20d_b_minus_simple60"]["n_boot"],
        "missing_prerequisites": missing,
    }


def protocol_verdict():
    verdict = classify_v1_verdict(is_uninspected_oos=True, meets_24_months=True,
        closed_episodes_count=65, provenance=provenance(), b_cum_net_return=.25,
        b_ann_net_return=.118, simple60_ann_net_return=.08,
        mbb_result={"valid": True, "n_boot": 1, "block_len": 1, "seed": 7,
                    "ci_lower": .001, "ci_upper": .001, "mean_daily_diff": .001},
        b_mdd=.1, b_profit_factor=1.5, b_profit_factor_status="DEFINED",
        b_cost2x_cum_net_return=.2, simple60_cost2x_cum_net_return=.15,
        b_delay1d_cum_net_return=.19, simple60_delay1d_cum_net_return=.14,
        random_p95_cum_net_return=.18)
    assert verdict["verdict_code"] == "ELIGIBLE_FOR_REVIEW_E0_ONLY"
    OUT["R5_nonfrozen_statistics_not_withheld"]["classifier_with_one_bootstrap"] = verdict["verdict_code"]


def journal_probes(tmp):
    entry = E1JournalRecord(record_id="one", action_type="ENTRY", signal_date="2026-03-06",
        check_session_date="2026-03-09", code="ETF", region="KR", sector="S",
        signal_close=10400, signal_sma20=10000, signal_atr20=200,
        disposition="FILLED", observed_at="2026-03-01T10:00:00+09:00", observed_price=10450,
        ordered_at="2026-03-01T10:01:00+09:00", order_qty=1,
        filled_at="2026-03-01T10:01:01+09:00", filled_qty=1, filled_price=10450)
    path = tmp / "e1.jsonl"
    append_e1_journal_record(path, entry)
    # Same calendar date encoded differently bypasses duplicate signal key.
    append_e1_journal_record(path, replace(entry, record_id="two", signal_date="2026-03-06T00:00:00"))
    records = load_e1_journal(path)
    assert len(records) == 2
    OUT["R6_e1_pre_signal_fill_and_duplicate_accepted"] = {
        "signal_date": entry.signal_date, "filled_at": entry.filled_at,
        "same_date_entry_records": len(records)}
    exits = []
    for i in range(60):
        d = pd.Timestamp("2023-01-02") + pd.Timedelta(days=14*i)
        exits.append(E1JournalRecord(record_id=f"exit-{i}", action_type="EXIT",
            signal_date=str((d-pd.Timedelta(days=1)).date()), check_session_date=str(d.date()),
            code="ETF", region="KR", sector="S", signal_close=10400,
            signal_sma20=10000, signal_atr20=200, disposition="EXIT_FILLED",
            observed_at=d.isoformat(), observed_price=10450, ordered_at=d.isoformat(),
            order_qty=0, filled_at=d.isoformat(), filled_qty=10, filled_price=10450))
    summary = summarize_e1_journal(exits)
    assert summary["profitability_claim_allowed"] and summary["real_entry_fills"] == 0
    OUT["R7_e1_orphan_exits_allow_profitability_claim"] = summary


def held_missing_data():
    cal, mapping, bars = build_fixture(180)
    sessions = cal.sessions_in_range(mapping.valid_from, bars["BM_KR"].index[-1])
    start = next(d for d in sessions if cal.is_week_end_session(d))
    entry_day = cal.next_session_after(start)
    missing_day = cal.next_session_after(entry_day)
    end = cal.next_session_after(missing_day)
    code = "KR_SEC_01"
    bars[code] = frame(bars[code].index, 10000)
    bars[code] = bars[code].drop(index=[missing_day, end])
    def selector(t, snap, slots, excluded, codes):
        return ([dict(code=code, name=code, region="KR", sector="SECTOR_KR_1",
                      instrument_kind="EQUITY_ETF", signal_close=10000,
                      signal_sma20=10000, signal_atr20=500)] if t == start else []), {}
    result = run_e0_simulation("missing-held", cal, mapping, bars,
        default_krx_research_cost_schedule(), start, end, selector)
    endpos = result.open_positions_at_end[0]
    assert endpos["mark_close"] == 10000
    assert not any(ev["date"] in {str(missing_day.date()), str(end.date())} for ev in result.order_events)
    OUT["R8_missing_held_marks_silently_carried"] = {
        "missing_dates": [str(missing_day.date()), str(end.date())],
        "end_mark": endpos["mark_close"], "mark_timestamp_or_stale_flag": "absent",
        "order_events_on_missing_dates": [], "coverage_halt_recorded": result.observation_summary["region_non_active_session_counts"]}


def mixed_region_comparison():
    # Record both policies' actual selections in full evaluator without changing implementation.
    from unittest.mock import patch
    import research.sector_v1.evaluator as evaluator
    cal, mapping, bars = build_fixture(180)
    dates = bars["BM_KR"].index
    members = {}
    for (region, sector), spec in mapping.representatives.items():
        if region != "KR":
            continue
        code = f"STOCK_{sector}"
        members[(region, sector)] = [(dates[0], (ConstituentItem(code, code, "KOSPI"),))]
        bars[code] = frame(dates, bars[spec.code]["Close"].to_numpy() * np.linspace(1, 1.1, len(dates)))
    pit = PITConstituentTable(members, True)
    captured = {}
    original = evaluator.run_e0_simulation
    def spy(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["policy_name"] in {"B_sector_etf_30m", "C_sector_stock_30m"}:
            captured[kwargs["policy_name"]] = sorted({e["region"] for e in result.order_events if e["event"] == "ENTRY_FILLED"})
        return result
    with patch.object(evaluator, "run_e0_simulation", side_effect=spy):
        report = evaluator.run_full_v1_evaluation(repo_root=ROOT, calendar=cal,
            mapping=mapping, bars_by_code=bars, cost_schedule=default_krx_research_cost_schedule(),
            provenance=provenance(), eval_start=mapping.valid_from, eval_end=dates[-1],
            pit_constituents=pit, n_random_policies=1, bootstrap_reps=1)
    assert "US" in captured["B_sector_etf_30m"] and captured["C_sector_stock_30m"] == ["KR"]
    OUT["R9_c_domestic_compared_to_multiregion_b"] = {
        "entry_regions": captured, "comparison": report["stock_comparisons"]["c_vs_b"]}


def main():
    stock_ablation()
    with tempfile.TemporaryDirectory(prefix="t148-audit-") as temp:
        tmp = Path(temp)
        bundle_and_artifacts(tmp)
        protocol_verdict()
        journal_probes(tmp)
    held_missing_data()
    mixed_region_comparison()
    print(json.dumps(OUT, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
