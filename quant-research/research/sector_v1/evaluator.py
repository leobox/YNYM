"""Statistical validation, benchmark harness, 4-way verdict machine, and sealed run ledger (§§4, 8, 9, 10).

Enforces all T-145 v1.0 pre-committed evaluation criteria:
- Boundary input validation (`ValidatedResearchInputs`): mechanical `TotalReturnClose` reconciliation,
  explicit vs defaulted corporate-action column audit, and PIT flag consistency checks.
- Sealed calendar window coverage audit against `[eval_start, eval_end]` (actual span annualization,
  never marking a truncated 50-session calendar as a 24-month window).
- Frozen statistical protocol enforcement: 20-trading-day Moving Block Bootstrap (`10,000` reps,
  `seed=20261107`, two-sided 95% CI lower `> 0`) and fixed-seed (`20261107`) `1,000`-policy Random
  benchmark; reduced counts are sealed as `DIAGNOSTIC_REDUCED_PROTOCOL_NO_ADOPTION`.
- Daily mark-to-market MDD `<= 15%` with explicit separation of carried/stale/missing marks
  (`estimated_max_drawdown` vs `observable_only_max_drawdown`), withholding adoption review when
  unobserved held marks exist.
- Common PIT-verified `(region, sector)` scope matching for `C` vs `B_pit_matched` and `A-matched`,
  while preserving multi-region `B` as the primary experiment.
- Pre-calculation input/code/config sealing, full ledger/episode/event/daily-signal/input-snapshot
  preservation per attempt directory, and standalone offline replay verification.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from . import (
    SPEC_TASK_ID,
    SPEC_VERSION,
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
    verify_spec_freeze,
)
from .execution_e0 import (
    ClosedEpisode,
    E0PortfolioResult,
    build_precomputed_close_sma20,
    run_e0_simulation,
    verify_accounting_conservation,
)
from .models import (
    MIN_REGIONAL_COVERAGE_RATIO,
    MIN_REGIONAL_REPRESENTATIVES,
    ConstituentItem,
    CostSchedule,
    DataProvenanceManifest,
    FrozenSectorMapping,
    PITConstituentTable,
    RegionalBenchmarkSpec,
    SectorETFSpec,
    TaxRuleEntry,
    TradingCalendar,
    ValidatedResearchInputs,
    clean_and_validate_bars,
    default_krx_research_cost_schedule,
    validate_research_inputs,
)
from .signals import (
    DailySignalSnapshot,
    evaluate_daily_snapshot,
    select_weekly_etf_candidates_b,
    select_weekly_etf_candidates_random,
    select_weekly_etf_candidates_simple60,
    select_weekly_stock_candidates_a_matched,
    select_weekly_stock_candidates_c,
)

BOOTSTRAP_BLOCK_LEN = 20
BOOTSTRAP_REPS = 10_000
FROZEN_MBB_ALPHA = 0.05
FIXED_RESEARCH_SEED = 20261107
DEFAULT_RANDOM_POLICIES = 1_000

MIN_UNINSPECTED_MONTHS = 24
MIN_CLOSED_EPISODES = 60
MIN_ANNUALIZED_SPREAD = 0.02
MAX_ALLOWED_MDD = 0.15
MIN_PROFIT_FACTOR = 1.2

PRIMARY_ACCOUNT_KRW = 30_000_000.0
SMALL_ACCOUNT_KRW = 1_000_000.0

PARTICIPATING_PACKAGE_FILES = (
    "__init__.py",
    "models.py",
    "signals.py",
    "execution_e0.py",
    "journal_e1.py",
    "evaluator.py",
)


class EvaluationReport(dict):
    """JSON-serializable evaluation report dict carrying validated inputs for lossless sealing."""

    _validated_inputs: ValidatedResearchInputs | None = None
    _raw_policy_results: dict[str, E0PortfolioResult] | None = None
    _daily_snapshots: dict[pd.Timestamp, DailySignalSnapshot] | None = None


def _clean_for_json(obj: object) -> object:
    """Recursively replace NaN/Inf with None while preserving Python `bool` distinct from `int`."""
    if isinstance(obj, dict):
        return {str(k): _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_for_json(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        val = float(obj)
        return val if math.isfinite(val) else None
    return obj


def moving_block_bootstrap_ci(
    daily_diff: Sequence[float] | np.ndarray,
    block_len: int = BOOTSTRAP_BLOCK_LEN,
    n_boot: int = BOOTSTRAP_REPS,
    seed: int = FIXED_RESEARCH_SEED,
    alpha: float = FROZEN_MBB_ALPHA,
) -> dict:
    """20-trading-day Moving Block Bootstrap two-sided `(1 - alpha)` CI on daily net return diffs (§9).

    Never bootstraps individual trade episodes as i.i.d. observations.
    """
    arr = np.asarray(daily_diff, dtype=float)
    if arr.ndim != 1 or not np.isfinite(arr).all():
        raise ValueError("INVALID_DAILY_DIFF_ARRAY_FOR_MBB")
    if block_len < 1 or n_boot < 1 or not (0.0 < alpha < 1.0):
        raise ValueError("INVALID_MBB_PARAMETERS")

    t_len = len(arr)
    if t_len < block_len:
        return {
            "valid": False,
            "reason": "SERIES_SHORTER_THAN_BLOCK_LEN",
            "n_obs": t_len,
            "block_len": int(block_len),
            "n_boot": int(n_boot),
            "seed": int(seed),
            "alpha": float(alpha),
            "mean_daily_diff": float(np.mean(arr)) if t_len > 0 else None,
            "ci_lower": None,
            "ci_upper": None,
        }

    blocks = sliding_window_view(arr, window_shape=block_len)
    n_avail = blocks.shape[0]
    n_blocks = int(math.ceil(t_len / block_len))
    tail_len = t_len - (n_blocks - 1) * block_len

    full_block_sums = blocks.sum(axis=1)
    tail_block_sums = blocks[:, :tail_len].sum(axis=1)

    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n_avail, size=(n_boot, n_blocks))

    if n_blocks > 1:
        total_sums = full_block_sums[starts[:, :-1]].sum(axis=1) + tail_block_sums[starts[:, -1]]
    else:
        total_sums = tail_block_sums[starts[:, 0]]

    boot_means = total_sums / float(t_len)
    lo_pct = 100.0 * (alpha / 2.0)
    hi_pct = 100.0 * (1.0 - alpha / 2.0)
    ci_lo = float(np.percentile(boot_means, lo_pct))
    ci_hi = float(np.percentile(boot_means, hi_pct))

    return {
        "valid": True,
        "reason": "OK",
        "n_obs": t_len,
        "block_len": int(block_len),
        "n_boot": int(n_boot),
        "seed": int(seed),
        "alpha": float(alpha),
        "mean_daily_diff": float(np.mean(arr)),
        "ci_lower": ci_lo,
        "ci_upper": ci_hi,
    }


def compute_max_drawdown(nav_series: pd.Series, initial_capital: float) -> float:
    """Compute peak-to-trough daily Close mark-to-market Maximum Drawdown (`0.15` == 15% MDD)."""
    if nav_series.empty:
        return 0.0
    curve = np.concatenate(
        [np.array([float(initial_capital)], dtype=float), nav_series.to_numpy(dtype=float)]
    )
    if not np.isfinite(curve).all() or (curve <= 0.0).any():
        raise ValueError("INVALID_NAV_CURVE_FOR_MDD")
    peaks = np.maximum.accumulate(curve)
    dds = 1.0 - (curve / peaks)
    return float(np.max(dds))


def compute_observable_only_max_drawdown(
    daily_ledger: pd.DataFrame, initial_capital: float
) -> float | None:
    """Compute MDD strictly across sessions where all held positions had observed Close marks."""
    if daily_ledger.empty:
        return 0.0
    if "nav_all_marks_observed" not in daily_ledger.columns:
        return compute_max_drawdown(daily_ledger["nav"], initial_capital=initial_capital)
    mask = daily_ledger["nav_all_marks_observed"].astype(bool)
    obs_nav = daily_ledger.loc[mask, "nav"]
    if obs_nav.empty:
        return None
    return compute_max_drawdown(obs_nav, initial_capital=initial_capital)


def compute_episode_metrics(episodes: Sequence[ClosedEpisode]) -> dict:
    """Compute closed-episode metrics with explicit zero-loss Profit Factor guard (§9 line 146)."""
    n_ep = len(episodes)
    if n_ep == 0:
        return {
            "closed_episode_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "flat_count": 0,
            "win_rate": 0.0,
            "gross_profit_krw": 0.0,
            "gross_loss_abs_krw": 0.0,
            "profit_factor": None,
            "profit_factor_status": "UNDEFINED_NO_CLOSED_EPISODES",
            "avg_win_krw": 0.0,
            "avg_loss_krw": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "loss_tail_p05_pct": 0.0,
            "loss_tail_worst_pct": 0.0,
            "loss_tail_worst_krw": 0.0,
            "holding_sessions_mean": 0.0,
            "holding_sessions_median": 0.0,
            "holding_sessions_max": 0,
        }

    pnls = np.array([ep.net_pnl_krw for ep in episodes], dtype=float)
    rets = np.array([ep.net_return_pct for ep in episodes], dtype=float)
    holds = np.array([ep.holding_sessions for ep in episodes], dtype=float)

    wins_mask = pnls > 0.0
    loss_mask = pnls < 0.0
    flat_mask = pnls == 0.0

    gross_profit = float(pnls[wins_mask].sum()) if wins_mask.any() else 0.0
    gross_loss_abs = float(np.abs(pnls[loss_mask].sum())) if loss_mask.any() else 0.0

    if gross_loss_abs == 0.0:
        pf = None
        pf_status = "UNDEFINED_ZERO_LOSS_WITHHOLD"
    else:
        pf = float(gross_profit / gross_loss_abs)
        pf_status = "DEFINED"

    return {
        "closed_episode_count": n_ep,
        "win_count": int(wins_mask.sum()),
        "loss_count": int(loss_mask.sum()),
        "flat_count": int(flat_mask.sum()),
        "win_rate": float(wins_mask.mean()),
        "gross_profit_krw": gross_profit,
        "gross_loss_abs_krw": gross_loss_abs,
        "profit_factor": pf,
        "profit_factor_status": pf_status,
        "avg_win_krw": float(pnls[wins_mask].mean()) if wins_mask.any() else 0.0,
        "avg_loss_krw": float(pnls[loss_mask].mean()) if loss_mask.any() else 0.0,
        "avg_win_pct": float(rets[wins_mask].mean()) if wins_mask.any() else 0.0,
        "avg_loss_pct": float(rets[loss_mask].mean()) if loss_mask.any() else 0.0,
        "loss_tail_p05_pct": float(np.percentile(rets, 5.0)),
        "loss_tail_worst_pct": float(np.min(rets)),
        "loss_tail_worst_krw": float(np.min(pnls)),
        "holding_sessions_mean": float(np.mean(holds)),
        "holding_sessions_median": float(np.median(holds)),
        "holding_sessions_max": int(np.max(holds)),
    }


def _actual_span_years_and_months(
    start_ts: pd.Timestamp | str,
    end_ts: pd.Timestamp | str,
) -> tuple[float, float, bool]:
    s_ts = pd.Timestamp(start_ts).normalize()
    e_ts = pd.Timestamp(end_ts).normalize()
    cal_days = max(1, int((e_ts - s_ts).days) + 1)
    years = cal_days / 365.25
    months_approx = cal_days / 30.4375
    meets_24m = bool(
        e_ts >= (s_ts + pd.DateOffset(months=MIN_UNINSPECTED_MONTHS) - pd.Timedelta(days=1))
    )
    return years, months_approx, meets_24m


def summarize_portfolio_result(
    res: E0PortfolioResult,
    eval_start: pd.Timestamp | str,
    eval_end: pd.Timestamp | str,
    window_audit: Mapping[str, object] | None = None,
) -> dict:
    """Build complete mandatory diagnostic dictionary for a simulated portfolio (§4, §5, §9).

    Annualizes strictly over the ACTUAL evaluated ledger span `[ledger.index[0], ledger.index[-1]]`,
    never over unverified requested date strings when the calendar is truncated.
    """
    verify_accounting_conservation(res.daily_ledger, initial_capital=res.initial_capital)
    actual_start = pd.Timestamp(res.daily_ledger.index[0]).normalize()
    actual_end = pd.Timestamp(res.daily_ledger.index[-1]).normalize()
    years, months_approx, span_24m = _actual_span_years_and_months(actual_start, actual_end)
    if window_audit is not None:
        meets_24m = bool(window_audit.get("meets_24_months", False))
    else:
        req_start = pd.Timestamp(eval_start).normalize()
        req_end = pd.Timestamp(eval_end).normalize()
        edges_ok = (actual_start - req_start).days <= 7 and (req_end - actual_end).days <= 7
        meets_24m = bool(span_24m and edges_ok)

    final_nav = float(res.daily_ledger["nav"].iloc[-1])
    cum_ret = (final_nav / res.initial_capital) - 1.0
    ann_ret = (
        ((final_nav / res.initial_capital) ** (1.0 / years) - 1.0)
        if (years > 0.0 and final_nav > 0.0)
        else -1.0
    )

    daily_rets = res.daily_ledger["daily_net_return"].to_numpy(dtype=float)
    realized_sessions_per_year = (len(daily_rets) / years) if years > 0.0 else 0.0
    ann_vol = (
        float(np.std(daily_rets, ddof=1) * math.sqrt(realized_sessions_per_year))
        if (len(daily_rets) > 1 and realized_sessions_per_year > 0.0)
        else 0.0
    )

    estimated_mdd = compute_max_drawdown(res.daily_ledger["nav"], initial_capital=res.initial_capital)
    obs_only_mdd = compute_observable_only_max_drawdown(
        res.daily_ledger, initial_capital=res.initial_capital
    )

    unobs_sessions = (
        int((~res.daily_ledger["nav_all_marks_observed"].astype(bool)).sum())
        if "nav_all_marks_observed" in res.daily_ledger.columns
        else 0
    )
    missing_bar_sessions = (
        int((res.daily_ledger["missing_bar_positions"] > 0).sum())
        if "missing_bar_positions" in res.daily_ledger.columns
        else 0
    )
    halted_mark_sessions = (
        int((res.daily_ledger["halted_mark_positions"] > 0).sum())
        if "halted_mark_positions" in res.daily_ledger.columns
        else 0
    )
    delisted_unsettled_sessions = (
        int((res.daily_ledger["delisted_unsettled_positions"] > 0).sum())
        if "delisted_unsettled_positions" in res.daily_ledger.columns
        else 0
    )
    mdd_quality_status = (
        "VERIFIED_ALL_HELD_MARKS_OBSERVED"
        if unobs_sessions == 0
        else "UNVERIFIED_CONTAINS_STALE_OR_MISSING_HELD_MARKS"
    )

    ep_stats = compute_episode_metrics(res.closed_episodes)

    event_counts: dict[str, int] = {}
    entered_regions: set[str] = set()
    for ev in res.order_events:
        name = str(ev.get("event", "UNKNOWN"))
        event_counts[name] = event_counts.get(name, 0) + 1
        if name == "ENTRY_FILLED" and ev.get("region"):
            entered_regions.add(str(ev["region"]))

    zero_drops = event_counts.get("ZERO_QTY_DROPPED", 0)
    insufficient_cash = event_counts.get("INSUFFICIENT_CASH_AT_SIGNAL", 0)
    queued_entries = event_counts.get("ENTRY_QUEUED", 0) + event_counts.get(
        "PENDING_AFTER_WINDOW_END", 0
    )
    total_candidate_attempts = zero_drops + insufficient_cash + queued_entries
    zero_drop_rate = (
        float(zero_drops / total_candidate_attempts) if total_candidate_attempts > 0 else 0.0
    )

    mean_nav = float(res.daily_ledger["nav"].mean())
    one_way_turnover = 0.5 * (res.total_buy_turnover_krw + res.total_sell_turnover_krw)
    ann_turnover = (
        float((one_way_turnover / mean_nav) / years) if (mean_nav > 0.0 and years > 0.0) else 0.0
    )
    monthly_trades = (
        float(ep_stats["closed_episode_count"] / months_approx) if months_approx > 0.0 else 0.0
    )

    return {
        "policy_name": res.policy_name,
        "initial_capital": res.initial_capital,
        "actual_eval_start": actual_start.strftime("%Y-%m-%d"),
        "actual_eval_end": actual_end.strftime("%Y-%m-%d"),
        "evaluated_sessions": int(len(res.daily_ledger)),
        "entered_regions": sorted(entered_regions),
        "final_nav": final_nav,
        "cumulative_net_return": cum_ret,
        "annualized_net_return": ann_ret,
        "annualization_note": (
            "FULL_VERIFICATION_WINDOW"
            if meets_24m
            else "SHORT_OR_INCOMPLETE_WINDOW_DIAGNOSTIC_NOT_EXPECTED_RETURN"
        ),
        "max_drawdown": estimated_mdd,
        "estimated_max_drawdown": estimated_mdd,
        "observable_only_max_drawdown": obs_only_mdd,
        "mark_quality": {
            "mdd_quality_status": mdd_quality_status,
            "all_held_marks_observed": bool(unobs_sessions == 0),
            "unobserved_mark_sessions": unobs_sessions,
            "missing_held_bar_sessions": missing_bar_sessions,
            "halted_held_bar_sessions": halted_mark_sessions,
            "delisted_unsettled_sessions": delisted_unsettled_sessions,
        },
        "annualized_volatility": ann_vol,
        "avg_gross_exposure_ratio": float(res.daily_ledger["gross_exposure_ratio"].mean()),
        "avg_cash_ratio": float(res.daily_ledger["cash_ratio"].mean()),
        "avg_max_single_weight": float(res.daily_ledger["max_single_weight"].mean()),
        "peak_single_weight": float(res.daily_ledger["max_single_weight"].max()),
        "annualized_turnover_ratio": ann_turnover,
        "monthly_closed_trades": monthly_trades,
        "zero_qty_drop_count": zero_drops,
        "total_candidate_attempts": total_candidate_attempts,
        "zero_qty_drop_rate": zero_drop_rate,
        "unfilled_and_halt_counts": {
            "budget_exceeded_at_open": event_counts.get("BUDGET_EXCEEDED_AT_OPEN", 0),
            "unfilled_halted_or_no_trade_at_open": event_counts.get(
                "UNFILLED_HALTED_OR_NO_TRADE_AT_OPEN", 0
            ),
            "insufficient_cash_at_signal": insufficient_cash,
            "exit_delayed_halt_or_no_trade": event_counts.get(
                "EXIT_DELAYED_HALT_OR_NO_TRADE", 0
            ),
            "held_mark_missing_bar": event_counts.get("HELD_MARK_MISSING_BAR", 0),
            "held_mark_stale_halted": event_counts.get("HELD_MARK_STALE_HALTED", 0),
            "held_mark_delisted_unsettled": event_counts.get(
                "HELD_MARK_DELISTED_UNSETTLED_NO_PROCEEDS_DATA", 0
            ),
        },
        "open_position_count_at_end": len(res.open_positions_at_end),
        "open_positions_at_end": res.open_positions_at_end,
        "episode_metrics": ep_stats,
        "observation_summary": res.observation_summary,
    }


def compute_passive_references(
    sessions: Sequence[pd.Timestamp],
    mapping: FrozenSectorMapping,
    bars_by_code: Mapping[str, pd.DataFrame],
    eval_start: pd.Timestamp | str,
    eval_end: pd.Timestamp | str,
) -> dict:
    """Compute passive regional buy-and-hold and equal-weight sector ETF references (§9 line 136).

    Passive exposure is reported transparently without rescaling/levering to match `B`.
    Annualizes strictly over actual evaluated `[sessions[0], sessions[-1]]`.
    """
    _ = (eval_start, eval_end)
    s_first, s_last = sessions[0], sessions[-1]
    years, _, _ = _actual_span_years_and_months(s_first, s_last)

    regional_bh: dict[str, dict] = {}
    for region, bench in sorted(mapping.benchmarks.items()):
        df_b = bars_by_code.get(bench.code)
        if df_b is None:
            continue
        sub = df_b.loc[s_first:s_last]
        sub = sub.loc[~sub["Halted"]] if "Halted" in sub.columns else sub
        if len(sub) < 2:
            continue
        tr = sub["TotalReturnClose"].astype(float)
        cum = float(tr.iloc[-1] / tr.iloc[0] - 1.0)
        ann = (
            float((1.0 + cum) ** (1.0 / years) - 1.0)
            if (years > 0.0 and (1.0 + cum) > 0.0)
            else -1.0
        )
        regional_bh[region] = {
            "benchmark_code": bench.code,
            "cumulative_total_return": cum,
            "annualized_total_return": ann,
            "nominal_exposure_ratio": 1.0,
        }

    ew_daily_rets: list[float] = []
    rep_specs = [spec for _, spec in sorted(mapping.representatives.items())]
    for idx in range(1, len(sessions)):
        d_prev, d_now = sessions[idx - 1], sessions[idx]
        day_rets: list[float] = []
        for spec in rep_specs:
            if not spec.is_active_on(d_now):
                continue
            df_e = bars_by_code.get(spec.code)
            if df_e is None or d_prev not in df_e.index or d_now not in df_e.index:
                continue
            if bool(df_e.loc[d_now, "Halted"]) or bool(df_e.loc[d_prev, "Halted"]):
                continue
            r_i = float(
                df_e.loc[d_now, "TotalReturnClose"] / df_e.loc[d_prev, "TotalReturnClose"] - 1.0
            )
            if math.isfinite(r_i):
                day_rets.append(r_i)
        ew_daily_rets.append(float(np.mean(day_rets)) if day_rets else 0.0)

    ew_cum = (
        float(np.prod(1.0 + np.array(ew_daily_rets, dtype=float)) - 1.0)
        if ew_daily_rets
        else 0.0
    )
    ew_ann = (
        float((1.0 + ew_cum) ** (1.0 / years) - 1.0)
        if (years > 0.0 and (1.0 + ew_cum) > 0.0)
        else -1.0
    )

    return {
        "exposure_rescaling_applied": False,
        "regional_benchmark_buy_and_hold": regional_bh,
        "equal_weight_eligible_sector_etfs": {
            "cumulative_total_return": ew_cum,
            "annualized_total_return": ew_ann,
            "nominal_exposure_ratio": 1.0,
        },
    }


def run_random_policy_benchmark(
    calendar: TradingCalendar,
    mapping: FrozenSectorMapping,
    bars_by_code: Mapping[str, pd.DataFrame],
    cost_schedule: CostSchedule,
    eval_start: pd.Timestamp | str,
    eval_end: pd.Timestamp | str,
    precomputed_snapshots: Mapping[pd.Timestamp, DailySignalSnapshot],
    precomputed_close_sma20: Mapping[str, Mapping[pd.Timestamp, tuple[float, float]]],
    initial_capital: float = PRIMARY_ACCOUNT_KRW,
    n_policies: int = DEFAULT_RANDOM_POLICIES,
    seed: int = FIXED_RESEARCH_SEED,
) -> dict:
    """Run `n_policies` fixed-seed Random E0 policies with independent dynamic §7 exits (§9 line 149)."""
    if n_policies < 1:
        raise ValueError("RANDOM_POLICY_COUNT_MUST_BE_POSITIVE")

    master_ss = np.random.SeedSequence(seed)
    child_seeds = master_ss.spawn(n_policies)
    cum_returns = np.empty(n_policies, dtype=float)
    closed_counts = np.empty(n_policies, dtype=int)

    for idx, child_ss in enumerate(child_seeds):
        policy_rng = np.random.default_rng(child_ss)

        def _rand_selector(
            _dt: pd.Timestamp,
            snap: DailySignalSnapshot,
            free_slots: int,
            excl_sectors: set[tuple[str, str]],
            _excl_codes: set[str],
            _rng: np.random.Generator = policy_rng,
        ) -> tuple[list[dict], dict[str, str]]:
            picks = select_weekly_etf_candidates_random(
                snapshot=snap,
                free_slots=free_slots,
                excluded_sector_keys=excl_sectors,
                rng=_rng,
                max_total_picks=3,
            )
            return picks, {}

        r_res = run_e0_simulation(
            policy_name=f"Random-{idx + 1:04d}",
            calendar=calendar,
            mapping=mapping,
            bars_by_code=bars_by_code,
            cost_schedule=cost_schedule,
            eval_start=eval_start,
            eval_end=eval_end,
            candidate_selector=_rand_selector,
            initial_capital=initial_capital,
            cost_multiplier=1.0,
            execution_delay_sessions=0,
            precomputed_snapshots=precomputed_snapshots,
            precomputed_close_sma20=precomputed_close_sma20,
            collect_observation_summary=False,
        )
        final_nav = float(r_res.daily_ledger["nav"].iloc[-1])
        cum_returns[idx] = (final_nav / initial_capital) - 1.0
        closed_counts[idx] = len(r_res.closed_episodes)

    return {
        "n_policies": int(n_policies),
        "seed": int(seed),
        "copies_actual_holding_periods": False,
        "is_frozen_protocol": bool(
            int(n_policies) == DEFAULT_RANDOM_POLICIES and int(seed) == FIXED_RESEARCH_SEED
        ),
        "p05_cumulative_net_return": float(np.percentile(cum_returns, 5.0)),
        "p50_cumulative_net_return": float(np.percentile(cum_returns, 50.0)),
        "p90_cumulative_net_return": float(np.percentile(cum_returns, 90.0)),
        "p95_cumulative_net_return": float(np.percentile(cum_returns, 95.0)),
        "p99_cumulative_net_return": float(np.percentile(cum_returns, 99.0)),
        "mean_cumulative_net_return": float(np.mean(cum_returns)),
        "std_cumulative_net_return": float(np.std(cum_returns, ddof=1)) if n_policies > 1 else 0.0,
        "mean_closed_episodes": float(np.mean(closed_counts)),
    }


def classify_v1_verdict(
    *,
    is_uninspected_oos: bool,
    meets_24_months: bool,
    closed_episodes_count: int,
    provenance: DataProvenanceManifest,
    b_cum_net_return: float,
    b_ann_net_return: float,
    simple60_ann_net_return: float,
    mbb_result: Mapping[str, object],
    b_mdd: float,
    b_profit_factor: float | None,
    b_profit_factor_status: str,
    b_cost2x_cum_net_return: float,
    simple60_cost2x_cum_net_return: float,
    b_delay1d_cum_net_return: float,
    simple60_delay1d_cum_net_return: float,
    random_p95_cum_net_return: float | None = None,
    random_benchmark: Mapping[str, object] | None = None,
    input_audit: Mapping[str, object] | None = None,
    window_audit: Mapping[str, object] | None = None,
    b_mdd_quality_status: str = "VERIFIED_ALL_HELD_MARKS_OBSERVED",
) -> dict:
    """Deterministic 4-way T-145 v1.0 verdict classifier (§9 lines 138-162).

    Strictly enforces mechanical input verification, calendar window completeness, observed MDD
    quality, and frozen MBB (`block_len=20`, `n_boot=10_000`, `seed=20261107`) + Random-1000
    (`n_policies=1_000`, `seed=20261107`) statistical protocol metadata.
    """
    missing_prereqs: list[str] = []

    def _add_prereq(code_str: str) -> None:
        if code_str not in missing_prereqs:
            missing_prereqs.append(code_str)

    if input_audit is not None:
        for item in input_audit.get("missing_prerequisites", ()):  # type: ignore[union-attr]
            _add_prereq(str(item))
        eff = input_audit.get("effective", {})
        if isinstance(eff, Mapping):
            if not bool(eff.get("total_return_verified", False)):
                _add_prereq("TOTAL_RETURN_NOT_VERIFIED")
            if not bool(eff.get("corporate_actions_verified", False)):
                _add_prereq("CORPORATE_ACTIONS_NOT_VERIFIED")
            if not bool(eff.get("mapping_pit_verified", False)):
                _add_prereq("SECTOR_MAPPING_NOT_PIT_VERIFIED")
    else:
        if not provenance.total_return_verified:
            _add_prereq("TOTAL_RETURN_NOT_VERIFIED")
        if not provenance.corporate_actions_verified:
            _add_prereq("CORPORATE_ACTIONS_NOT_VERIFIED")
        if not provenance.mapping_pit_verified:
            _add_prereq("SECTOR_MAPPING_NOT_PIT_VERIFIED")

    if window_audit is not None:
        if not bool(window_audit.get("calendar_complete_for_requested_window", False)):
            _add_prereq("CALENDAR_INCOMPLETE_FOR_REQUESTED_WINDOW")
        if int(window_audit.get("unscheduled_bar_sessions_total", 0)) > 0:  # type: ignore[arg-type]
            _add_prereq("CALENDAR_OMITS_OBSERVED_TRADING_SESSIONS")
        if float(window_audit.get("max_benchmark_missing_ratio", 0.0)) > (
            1.0 - MIN_REGIONAL_COVERAGE_RATIO
        ):
            _add_prereq("BENCHMARK_SESSION_COVERAGE_BELOW_90PCT")

    if not is_uninspected_oos:
        _add_prereq("INSPECTED_SAMPLE_DIAGNOSTIC_ONLY_NOT_NEW_OOS")
    if not meets_24_months:
        _add_prereq("WINDOW_SHORTER_THAN_24_MONTHS")
    if closed_episodes_count < MIN_CLOSED_EPISODES:
        _add_prereq(f"CLOSED_EPISODES_BELOW_{MIN_CLOSED_EPISODES}")

    # Enforce frozen MBB statistical constants (§9 line 144)
    mbb_alpha = float(mbb_result.get("alpha", FROZEN_MBB_ALPHA))  # type: ignore[arg-type]
    mbb_is_frozen = bool(
        mbb_result.get("block_len") == BOOTSTRAP_BLOCK_LEN
        and mbb_result.get("n_boot") == BOOTSTRAP_REPS
        and mbb_result.get("seed") == FIXED_RESEARCH_SEED
        and abs(mbb_alpha - FROZEN_MBB_ALPHA) < 1e-12
    )
    if not mbb_is_frozen:
        _add_prereq("NON_FROZEN_MBB_PROTOCOL_DIAGNOSTIC_ONLY")
    if not bool(mbb_result.get("valid")):
        _add_prereq("MBB_SERIES_TOO_SHORT")

    # Enforce frozen Random-1000 benchmark metadata (§9 line 149)
    rand_is_frozen = False
    if random_benchmark is None:
        _add_prereq("RANDOM_BENCHMARK_PROTOCOL_METADATA_MISSING")
        eff_random_p95 = (
            float(random_p95_cum_net_return)
            if random_p95_cum_net_return is not None
            else float("inf")
        )
    else:
        rb_p95 = float(random_benchmark["p95_cumulative_net_return"])  # type: ignore[arg-type]
        p95_consistent = (
            random_p95_cum_net_return is None
            or abs(float(random_p95_cum_net_return) - rb_p95) <= 1e-12
        )
        rand_is_frozen = bool(
            random_benchmark.get("n_policies") == DEFAULT_RANDOM_POLICIES
            and random_benchmark.get("seed") == FIXED_RESEARCH_SEED
            and random_benchmark.get("copies_actual_holding_periods") is False
            and p95_consistent
        )
        if not rand_is_frozen:
            _add_prereq("NON_FROZEN_RANDOM_BENCHMARK_PROTOCOL_DIAGNOSTIC_ONLY")
        eff_random_p95 = rb_p95

    if b_mdd_quality_status != "VERIFIED_ALL_HELD_MARKS_OBSERVED":
        _add_prereq("UNOBSERVED_OR_STALE_HELD_MARKS_IN_MDD")

    if b_profit_factor_status != "DEFINED":
        _add_prereq(f"PROFIT_FACTOR_{b_profit_factor_status}")

    eval_mode = (
        "FROZEN_V1_PROTOCOL"
        if (mbb_is_frozen and rand_is_frozen)
        else "DIAGNOSTIC_REDUCED_PROTOCOL_NO_ADOPTION"
    )

    ann_spread = b_ann_net_return - simple60_ann_net_return
    cost2x_spread = b_cost2x_cum_net_return - simple60_cost2x_cum_net_return
    delay1d_spread = b_delay1d_cum_net_return - simple60_delay1d_cum_net_return
    ci_lower = mbb_result.get("ci_lower")

    gates = {
        "net_return_positive": bool(b_cum_net_return > 0.0),
        "annualized_spread_ge_2pp": bool(ann_spread >= MIN_ANNUALIZED_SPREAD),
        "mbb_ci95_lower_gt_0": bool(ci_lower is not None and float(ci_lower) > 0.0),  # type: ignore[arg-type]
        "mdd_le_15pct": bool(
            b_mdd_quality_status == "VERIFIED_ALL_HELD_MARKS_OBSERVED"
            and b_mdd <= MAX_ALLOWED_MDD
        ),
        "profit_factor_ge_1_2": bool(
            b_profit_factor_status == "DEFINED"
            and b_profit_factor is not None
            and float(b_profit_factor) >= MIN_PROFIT_FACTOR
        ),
        "cost_2x_stress_pass": bool(b_cost2x_cum_net_return > 0.0 and cost2x_spread > 0.0),
        "delay_1d_stress_pass": bool(b_delay1d_cum_net_return > 0.0 and delay1d_spread > 0.0),
        "random_1000_gt_p95": bool(b_cum_net_return > eff_random_p95),
    }

    if missing_prereqs:
        return {
            "verdict_code": "INSUFFICIENT_EVIDENCE",
            "verdict_ko": "검증 부족 (판정 유보)",
            "evaluation_mode": eval_mode,
            "operational_promotion_allowed": False,
            "e1_manual_validation_complete": False,
            "missing_prerequisites": missing_prereqs,
            "hurdle_gates": gates,
            "metrics": {
                "annualized_spread_vs_simple60": ann_spread,
                "cost_2x_spread_vs_simple60": cost2x_spread,
                "delay_1d_stress_spread_vs_simple60": delay1d_spread,
                "delay_1d_spread_vs_simple60": delay1d_spread,
            },
        }

    if all(gates.values()):
        return {
            "verdict_code": "ELIGIBLE_FOR_REVIEW_E0_ONLY",
            "verdict_ko": "해당 실행 모델(E0)·검증 범위에서 채택 검토 가능 (E1 수동 운영 검증 미완료)",
            "evaluation_mode": eval_mode,
            "operational_promotion_allowed": False,
            "e1_manual_validation_complete": False,
            "missing_prerequisites": [],
            "hurdle_gates": gates,
            "metrics": {
                "annualized_spread_vs_simple60": ann_spread,
                "cost_2x_spread_vs_simple60": cost2x_spread,
                "delay_1d_spread_vs_simple60": delay1d_spread,
            },
        }

    core_viability_ok = (
        gates["net_return_positive"]
        and gates["mdd_le_15pct"]
        and gates["profit_factor_ge_1_2"]
        and gates["cost_2x_stress_pass"]
        and gates["delay_1d_stress_pass"]
    )
    point_improvement_positive = (
        ann_spread > 0.0 and float(mbb_result.get("mean_daily_diff") or 0.0) > 0.0  # type: ignore[arg-type]
    )
    ci_upper = mbb_result.get("ci_upper")
    zero_in_ci = (
        ci_lower is not None
        and ci_upper is not None
        and float(ci_lower) <= 0.0 <= float(ci_upper)  # type: ignore[arg-type]
    )
    if core_viability_ok and point_improvement_positive and zero_in_ci:
        return {
            "verdict_code": "ADVANTAGE_UNPROVEN",
            "verdict_ko": "우위 미입증 (추정 개선 양수이나 신뢰구간에 0 포함)",
            "evaluation_mode": eval_mode,
            "operational_promotion_allowed": False,
            "e1_manual_validation_complete": False,
            "missing_prerequisites": [],
            "hurdle_gates": gates,
            "metrics": {
                "annualized_spread_vs_simple60": ann_spread,
                "cost_2x_spread_vs_simple60": cost2x_spread,
                "delay_1d_spread_vs_simple60": delay1d_spread,
            },
        }

    return {
        "verdict_code": "REJECTED_V1",
        "verdict_ko": "해당 v1.0 기각",
        "evaluation_mode": eval_mode,
        "operational_promotion_allowed": False,
        "e1_manual_validation_complete": False,
        "missing_prerequisites": [],
        "hurdle_gates": gates,
        "metrics": {
            "annualized_spread_vs_simple60": ann_spread,
            "cost_2x_spread_vs_simple60": cost2x_spread,
            "delay_1d_spread_vs_simple60": delay1d_spread,
        },
    }


def compute_ledger_hashes(
    *,
    repo_root: Path,
    freeze_info: Mapping[str, object],
    validated: ValidatedResearchInputs,
) -> dict:
    """Compute SHA256 hashes of all participating code files, frozen spec, and input tables (§8)."""
    pkg_dir = Path(__file__).resolve().parent
    code_hashes: dict[str, str] = {}
    for fname in PARTICIPATING_PACKAGE_FILES:
        code_hashes[f"research/sector_v1/{fname}"] = sha256_file(pkg_dir / fname)
    cli_path = Path(repo_root).resolve() / "quant-research/scripts/run_t145_sector_research.py"
    if cli_path.is_file():
        code_hashes["scripts/run_t145_sector_research.py"] = sha256_file(cli_path)

    return {
        "spec_task_id": SPEC_TASK_ID,
        "spec_version": SPEC_VERSION,
        "spec_freeze_sha256": freeze_info["freeze_sha256"],
        "candidate_engine_sha256": sha256_file(pkg_dir / "signals.py"),
        "exit_engine_sha256": sha256_file(pkg_dir / "execution_e0.py"),
        "evaluator_engine_sha256": sha256_file(pkg_dir / "evaluator.py"),
        "models_engine_sha256": sha256_file(pkg_dir / "models.py"),
        "journal_e1_engine_sha256": sha256_file(pkg_dir / "journal_e1.py"),
        "package_code_hashes": code_hashes,
        "universe_manifest_sha256": validated.provenance.sha256(),
        "source_file_hashes": dict(sorted(validated.provenance.file_hashes.items())),
        "bar_data_hashes": dict(sorted(validated.bar_hashes.items())),
        "mapping_sha256": validated.mapping.sha256(),
        "calendar_sha256": validated.calendar.sha256(),
        "cost_schedule_sha256": validated.cost_schedule.sha256(),
        "pit_constituents_sha256": (
            validated.pit_constituents.sha256()
            if validated.pit_constituents is not None
            else None
        ),
    }


def _build_pre_run_seal(
    *,
    repo_root: Path,
    freeze_info: Mapping[str, object],
    validated: ValidatedResearchInputs,
    window_audit: Mapping[str, object],
    eval_start: pd.Timestamp | str,
    eval_end: pd.Timestamp | str,
    is_uninspected_oos: bool,
    n_random_policies: int,
    bootstrap_reps: int,
    bootstrap_block_len: int,
    seed: int,
    strict_input_validation: bool,
) -> dict:
    ledger_hashes = compute_ledger_hashes(
        repo_root=repo_root, freeze_info=freeze_info, validated=validated
    )
    protocol_frozen = bool(
        int(n_random_policies) == DEFAULT_RANDOM_POLICIES
        and int(bootstrap_reps) == BOOTSTRAP_REPS
        and int(bootstrap_block_len) == BOOTSTRAP_BLOCK_LEN
        and int(seed) == FIXED_RESEARCH_SEED
    )
    eval_cfg = {
        "eval_start": pd.Timestamp(eval_start).strftime("%Y-%m-%d"),
        "eval_end": pd.Timestamp(eval_end).strftime("%Y-%m-%d"),
        "is_uninspected_oos": bool(is_uninspected_oos),
        "n_random_policies": int(n_random_policies),
        "bootstrap_reps": int(bootstrap_reps),
        "bootstrap_block_len": int(bootstrap_block_len),
        "seed": int(seed),
        "strict_input_validation": bool(strict_input_validation),
        "primary_account_krw": PRIMARY_ACCOUNT_KRW,
        "small_account_krw": SMALL_ACCOUNT_KRW,
        "evaluation_mode": (
            "FROZEN_V1_PROTOCOL"
            if protocol_frozen
            else "DIAGNOSTIC_REDUCED_PROTOCOL_NO_ADOPTION"
        ),
    }
    seal_core = _clean_for_json(
        {
            "spec_task_id": SPEC_TASK_ID,
            "spec_version": SPEC_VERSION,
            "ledger_hashes": ledger_hashes,
            "evaluation_config": eval_cfg,
            "input_audit": dict(validated.input_audit),
            "window_audit": dict(window_audit),
        }
    )
    seal_sha = sha256_bytes(canonical_json_bytes(seal_core))
    return {
        "sealed_before_simulation_at_utc": datetime.now(timezone.utc).isoformat(),
        "pre_run_seal_sha256": seal_sha,
        **seal_core,  # type: ignore[dict-item]
    }


def _write_inputs_snapshot(
    *,
    snapshot_dir: Path,
    validated: ValidatedResearchInputs,
    eval_cfg: Mapping[str, object],
) -> dict[str, str]:
    """Write a self-contained offline bundle & CSV bar snapshot so runs replay without source paths."""
    bars_dir = snapshot_dir / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)

    bar_files_rel: dict[str, str] = {}
    cols = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "SplitFactor",
        "CashDividend",
        "Halted",
        "TotalReturnClose",
    ]
    for code, df in sorted(validated.bars_by_code.items()):
        rel = f"bars/{code}.csv"
        out_df = df[cols].copy()
        out_df.index = pd.Index([d.strftime("%Y-%m-%d") for d in out_df.index], name="Date")
        out_df.to_csv(snapshot_dir / rel)
        bar_files_rel[code] = rel

    cs = validated.cost_schedule
    cost_doc = {
        "schedule_id": cs.schedule_id,
        "account_type": cs.account_type,
        "buy_fee_rate": cs.buy_fee_rate,
        "sell_fee_rate": cs.sell_fee_rate,
        "entry_slippage_rate": cs.entry_slippage_rate,
        "exit_slippage_rate": cs.exit_slippage_rate,
        "etf_ter_extra_deduction_rate": cs.etf_ter_extra_deduction_rate,
        "is_exact_personal_after_tax": cs.is_exact_personal_after_tax,
        "tax_rules": [r.__dict__ for r in cs.tax_rules],
    }

    bundle_doc = _clean_for_json(
        {
            "scheduled_sessions": [d.strftime("%Y-%m-%d") for d in validated.calendar.sessions],
            "calendar_meta": {
                "calendar_id": validated.calendar.calendar_id,
                "source": validated.calendar.source,
                "covered_start": (
                    validated.calendar.covered_start.strftime("%Y-%m-%d")
                    if validated.calendar.covered_start is not None
                    else None
                ),
                "covered_end": (
                    validated.calendar.covered_end.strftime("%Y-%m-%d")
                    if validated.calendar.covered_end is not None
                    else None
                ),
                "is_exchange_verified": bool(validated.calendar.is_exchange_verified),
            },
            "benchmarks": {
                k: v.__dict__ for k, v in sorted(validated.mapping.benchmarks.items())
            },
            "representatives": [
                spec.__dict__
                for _, spec in sorted(validated.mapping.representatives.items())
            ],
            "mapping_meta": {
                "mapping_version": validated.mapping.mapping_version,
                "freeze_date": pd.Timestamp(validated.mapping.freeze_date).strftime("%Y-%m-%d"),
                "valid_from": pd.Timestamp(validated.mapping.valid_from).strftime("%Y-%m-%d"),
                "valid_until": pd.Timestamp(validated.mapping.valid_until).strftime("%Y-%m-%d"),
                "pending_mixed_codes": list(validated.mapping.pending_mixed_codes),
                "excluded_non_equity_codes": list(validated.mapping.excluded_non_equity_codes),
                "is_pit_verified": bool(validated.mapping.is_pit_verified),
            },
            "bar_files": bar_files_rel,
            "explicit_columns_by_code": {
                c: dict(m) for c, m in sorted(validated.explicit_columns_by_code.items())
            },
            "cost_schedule": cost_doc,
            "pit_constituents": (
                validated.pit_constituents.to_serializable_dict()
                if validated.pit_constituents is not None
                else None
            ),
            "provenance": {
                "manifest_id": validated.provenance.manifest_id,
                "fetched_at": validated.provenance.fetched_at,
                "provider": validated.provenance.provider,
                "total_return_verified": bool(validated.provenance.total_return_verified),
                "corporate_actions_verified": bool(
                    validated.provenance.corporate_actions_verified
                ),
                "mapping_pit_verified": bool(validated.provenance.mapping_pit_verified),
                "constituents_pit_verified_regions": list(
                    validated.provenance.constituents_pit_verified_regions
                ),
                "notes": validated.provenance.notes,
            },
            "eval_start": eval_cfg["eval_start"],
            "eval_end": eval_cfg["eval_end"],
            "is_uninspected_oos": bool(eval_cfg["is_uninspected_oos"]),
            "evaluation_config": dict(eval_cfg),
        }
    )
    bundle_path = snapshot_dir / "bundle.json"
    bundle_path.write_text(
        json.dumps(bundle_doc, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    return bar_files_rel


def load_research_bundle(bundle_path: Path, *, strict: bool = True) -> dict:
    """Load an offline research bundle JSON and return boundary-validated research inputs."""
    bundle_path = Path(bundle_path).resolve()
    base_dir = bundle_path.parent
    doc = json.loads(bundle_path.read_text(encoding="utf-8"))

    file_hashes: dict[str, str] = {bundle_path.name: sha256_file(bundle_path)}
    cal_meta = doc.get("calendar_meta") or {}
    calendar = TradingCalendar(
        doc["scheduled_sessions"],
        calendar_id=str(cal_meta.get("calendar_id", "UNSPECIFIED_CALENDAR")),
        source=str(cal_meta.get("source", "unspecified")),
        covered_start=cal_meta.get("covered_start"),
        covered_end=cal_meta.get("covered_end"),
        is_exchange_verified=bool(cal_meta.get("is_exchange_verified", False)),
    )

    benchmarks = {
        reg: RegionalBenchmarkSpec(**meta) for reg, meta in doc["benchmarks"].items()
    }
    reps: dict[tuple[str, str], SectorETFSpec] = {}
    for item in doc["representatives"]:
        spec = SectorETFSpec(**item)
        reps[(spec.region, spec.sector)] = spec

    map_meta = doc["mapping_meta"]
    mapping = FrozenSectorMapping(
        mapping_version=map_meta["mapping_version"],
        freeze_date=pd.Timestamp(map_meta["freeze_date"]),
        valid_from=pd.Timestamp(map_meta["valid_from"]),
        valid_until=pd.Timestamp(map_meta["valid_until"]),
        benchmarks=benchmarks,
        representatives=reps,
        pending_mixed_codes=tuple(map_meta.get("pending_mixed_codes", ())),
        excluded_non_equity_codes=tuple(map_meta.get("excluded_non_equity_codes", ())),
        is_pit_verified=bool(map_meta.get("is_pit_verified", False)),
    )

    explicit_overrides_all = doc.get("explicit_columns_by_code") or {}
    bars_by_code: dict[str, pd.DataFrame] = {}
    for code, rel_file in sorted(doc["bar_files"].items()):
        fpath = (base_dir / rel_file).resolve()
        file_hashes[f"bars:{code}"] = sha256_file(fpath)
        if fpath.suffix.lower() == ".parquet":
            raw_df = pd.read_parquet(fpath)
        else:
            raw_df = pd.read_csv(fpath, index_col=0, float_precision="round_trip")
        overrides = explicit_overrides_all.get(code)
        bars_by_code[code] = clean_and_validate_bars(
            raw_df, code=code, explicit_column_overrides=overrides
        )

    if "cost_schedule" in doc and doc["cost_schedule"] is not None:
        cs_doc = doc["cost_schedule"]
        rules = tuple(TaxRuleEntry(**r) for r in cs_doc["tax_rules"])
        cost_schedule = CostSchedule(
            schedule_id=cs_doc["schedule_id"],
            account_type=cs_doc["account_type"],
            buy_fee_rate=float(cs_doc["buy_fee_rate"]),
            sell_fee_rate=float(cs_doc["sell_fee_rate"]),
            entry_slippage_rate=float(cs_doc["entry_slippage_rate"]),
            exit_slippage_rate=float(cs_doc["exit_slippage_rate"]),
            tax_rules=rules,
            etf_ter_extra_deduction_rate=float(cs_doc.get("etf_ter_extra_deduction_rate", 0.0)),
            is_exact_personal_after_tax=bool(cs_doc.get("is_exact_personal_after_tax", False)),
        )
    else:
        cost_schedule = default_krx_research_cost_schedule()

    pit_constituents = None
    if doc.get("pit_constituents"):
        snap_map: dict[tuple[str, str], list[tuple[pd.Timestamp, tuple[ConstituentItem, ...]]]] = {}
        for entry in doc["pit_constituents"]["entries"]:
            key = (entry["region"], entry["sector"])
            items = tuple(ConstituentItem(**m) for m in entry["members"])
            snap_map.setdefault(key, []).append((pd.Timestamp(entry["effective_date"]), items))
        for k in snap_map:
            snap_map[k].sort(key=lambda pair: pair[0])
        pit_constituents = PITConstituentTable(
            snapshots=snap_map,
            is_pit_verified=bool(doc["pit_constituents"].get("is_pit_verified", False)),
        )

    prov_doc = doc["provenance"]
    provenance = DataProvenanceManifest(
        manifest_id=prov_doc["manifest_id"],
        fetched_at=prov_doc["fetched_at"],
        provider=prov_doc["provider"],
        total_return_verified=bool(prov_doc.get("total_return_verified", False)),
        corporate_actions_verified=bool(prov_doc.get("corporate_actions_verified", False)),
        mapping_pit_verified=bool(prov_doc.get("mapping_pit_verified", False)),
        constituents_pit_verified_regions=tuple(
            prov_doc.get("constituents_pit_verified_regions", ())
        ),
        file_hashes=file_hashes,
        notes=str(prov_doc.get("notes", "")),
    )

    validated = validate_research_inputs(
        calendar=calendar,
        mapping=mapping,
        bars_by_code=bars_by_code,
        cost_schedule=cost_schedule,
        provenance=provenance,
        pit_constituents=pit_constituents,
        strict=strict,
    )

    return {
        "calendar": validated.calendar,
        "mapping": validated.mapping,
        "bars_by_code": validated.bars_by_code,
        "cost_schedule": validated.cost_schedule,
        "pit_constituents": validated.pit_constituents,
        "provenance": validated.provenance,
        "eval_start": doc["eval_start"],
        "eval_end": doc["eval_end"],
        "is_uninspected_oos": bool(doc.get("is_uninspected_oos", False)),
        "validated_inputs": validated,
        "strict_input_validation": strict,
    }


def _serialize_policy_result(res: E0PortfolioResult) -> dict:
    ledger_rows: list[dict] = []
    for idx, row in res.daily_ledger.iterrows():
        rec = {"date": pd.Timestamp(idx).strftime("%Y-%m-%d")}
        for col in res.daily_ledger.columns:
            val = row[col]
            if isinstance(val, (bool, np.bool_)):
                rec[col] = bool(val)
            elif isinstance(val, (int, np.integer)):
                rec[col] = int(val)
            else:
                rec[col] = float(val)
        ledger_rows.append(rec)

    return _clean_for_json(
        {
            "policy_name": res.policy_name,
            "initial_capital": res.initial_capital,
            "cost_multiplier": res.cost_multiplier,
            "execution_delay_sessions": res.execution_delay_sessions,
            "daily_ledger": ledger_rows,
            "closed_episodes": [asdict(ep) for ep in res.closed_episodes],
            "open_positions_at_end": res.open_positions_at_end,
            "order_events": res.order_events,
        }
    )  # type: ignore[return-value]


def _serialize_daily_snapshot(snap: DailySignalSnapshot) -> dict:
    return _clean_for_json(
        {
            "eval_date": snap.eval_date,
            "region_status": dict(snap.region_status),
            "benchmark_metrics": dict(snap.benchmark_metrics),
            "blocked_representatives": list(snap.blocked_representatives),
            "observations": [asdict(obs) for obs in snap.observations],
        }
    )  # type: ignore[return-value]


def seal_pre_run_attempt(
    *,
    out_root: Path,
    repo_root: Path,
    validated_inputs: ValidatedResearchInputs,
    eval_start: pd.Timestamp | str,
    eval_end: pd.Timestamp | str,
    is_uninspected_oos: bool = False,
    n_random_policies: int = DEFAULT_RANDOM_POLICIES,
    bootstrap_reps: int = BOOTSTRAP_REPS,
    bootstrap_block_len: int = BOOTSTRAP_BLOCK_LEN,
    seed: int = FIXED_RESEARCH_SEED,
    strict_input_validation: bool = True,
    run_label: str = "v1_0_e0",
) -> dict:
    """Seal inputs, code hashes, config, and raw input snapshot on disk BEFORE performance calculation (§8, §10)."""
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    registry_path = out_root / "attempt_registry.json"
    pre_seals_path = out_root / "pre_run_seals.jsonl"

    existing_attempts = 0
    if registry_path.exists():
        reg_doc = json.loads(registry_path.read_text(encoding="utf-8"))
        existing_attempts = len(reg_doc.get("attempts", []))
    pre_count = 0
    if pre_seals_path.exists():
        pre_count = sum(
            1 for ln in pre_seals_path.read_text(encoding="utf-8").splitlines() if ln.strip()
        )
    attempt_no = max(existing_attempts, pre_count) + 1

    freeze_info = verify_spec_freeze(repo_root)
    window_audit = validated_inputs.calendar.audit_window_coverage(
        eval_start=eval_start,
        eval_end=eval_end,
        bars_by_code=validated_inputs.bars_by_code,
        benchmarks=validated_inputs.mapping.benchmarks,
        min_uninspected_months=MIN_UNINSPECTED_MONTHS,
    )
    pre_seal = _build_pre_run_seal(
        repo_root=repo_root,
        freeze_info=freeze_info,
        validated=validated_inputs,
        window_audit=window_audit,
        eval_start=eval_start,
        eval_end=eval_end,
        is_uninspected_oos=is_uninspected_oos,
        n_random_policies=n_random_policies,
        bootstrap_reps=bootstrap_reps,
        bootstrap_block_len=bootstrap_block_len,
        seed=seed,
        strict_input_validation=strict_input_validation,
    )
    short_sha = str(pre_seal["pre_run_seal_sha256"])[:12]
    eff_label = (
        run_label
        if pre_seal["evaluation_config"]["evaluation_mode"] == "FROZEN_V1_PROTOCOL"
        else f"{run_label}_diagnostic"
    )
    run_dir_name = f"attempt_{attempt_no:04d}_{eff_label}_{short_sha}"
    run_dir = out_root / run_dir_name
    if run_dir.exists():
        raise FileExistsError(f"IMMUTABLE_RUN_DIR_ALREADY_EXISTS:{run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)

    (run_dir / "pre_run_input_seal.json").write_text(
        json.dumps(pre_seal, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    _write_inputs_snapshot(
        snapshot_dir=run_dir / "inputs_snapshot",
        validated=validated_inputs,
        eval_cfg=pre_seal["evaluation_config"],
    )

    pre_log_entry = {
        "attempt_number": attempt_no,
        "run_dir": run_dir_name,
        "sealed_before_simulation_at_utc": pre_seal["sealed_before_simulation_at_utc"],
        "pre_run_seal_sha256": pre_seal["pre_run_seal_sha256"],
        "evaluation_mode": pre_seal["evaluation_config"]["evaluation_mode"],
    }
    with pre_seals_path.open("ab") as fh:
        fh.write(canonical_json_bytes(pre_log_entry) + b"\n")

    return {
        "attempt_number": attempt_no,
        "run_dir": run_dir,
        "run_dir_name": run_dir_name,
        "pre_run_seal": pre_seal,
    }


def run_full_v1_evaluation(
    *,
    repo_root: Path,
    calendar: TradingCalendar,
    mapping: FrozenSectorMapping,
    bars_by_code: Mapping[str, pd.DataFrame],
    cost_schedule: CostSchedule,
    provenance: DataProvenanceManifest,
    eval_start: pd.Timestamp | str,
    eval_end: pd.Timestamp | str,
    pit_constituents: PITConstituentTable | None = None,
    is_uninspected_oos: bool = False,
    n_random_policies: int = DEFAULT_RANDOM_POLICIES,
    bootstrap_reps: int = BOOTSTRAP_REPS,
    bootstrap_block_len: int = BOOTSTRAP_BLOCK_LEN,
    seed: int = FIXED_RESEARCH_SEED,
    validated_inputs: ValidatedResearchInputs | None = None,
    strict_input_validation: bool = True,
) -> EvaluationReport:
    """Execute full T-145 v1.0 E0 research evaluation suite without modifying operational strategies."""
    freeze_info = verify_spec_freeze(repo_root)

    # Always validate inputs mechanically at the Python API boundary (even if called without CLI)
    _ = validated_inputs
    validated = validate_research_inputs(
        calendar=calendar,
        mapping=mapping,
        bars_by_code=bars_by_code,
        cost_schedule=cost_schedule,
        provenance=provenance,
        pit_constituents=pit_constituents,
        strict=strict_input_validation,
    )
    calendar = validated.calendar
    mapping = validated.mapping
    bars_by_code = validated.bars_by_code
    cost_schedule = validated.cost_schedule
    pit_constituents = validated.pit_constituents

    window_audit = calendar.audit_window_coverage(
        eval_start=eval_start,
        eval_end=eval_end,
        bars_by_code=bars_by_code,
        benchmarks=mapping.benchmarks,
        min_uninspected_months=MIN_UNINSPECTED_MONTHS,
    )
    sessions = calendar.sessions_in_range(eval_start, eval_end)
    if not sessions:
        raise ValueError("EMPTY_EVALUATION_SESSIONS")
    meets_24m = bool(window_audit["meets_24_months"])

    # Seal inputs, code hashes, and configuration BEFORE running any strategy simulation (§8, §10)
    pre_run_seal = _build_pre_run_seal(
        repo_root=repo_root,
        freeze_info=freeze_info,
        validated=validated,
        window_audit=window_audit,
        eval_start=eval_start,
        eval_end=eval_end,
        is_uninspected_oos=is_uninspected_oos,
        n_random_policies=n_random_policies,
        bootstrap_reps=bootstrap_reps,
        bootstrap_block_len=bootstrap_block_len,
        seed=seed,
        strict_input_validation=strict_input_validation,
    )

    # Precompute daily snapshots once across the window (strictly PIT per session `t`)
    snapshots_by_day: dict[pd.Timestamp, DailySignalSnapshot] = {
        t: evaluate_daily_snapshot(t, mapping, bars_by_code) for t in sessions
    }
    etf_codes = [spec.code for spec in mapping.representatives.values()]
    precomputed_etf_close_sma20 = build_precomputed_close_sma20(
        sessions=sessions,
        codes=etf_codes,
        bars_by_code=bars_by_code,
    )

    def _sel_b(
        _dt: pd.Timestamp,
        snap: DailySignalSnapshot,
        free_slots: int,
        excl_sec: set[tuple[str, str]],
        _excl_codes: set[str],
    ) -> tuple[list[dict], dict[str, str]]:
        return (
            select_weekly_etf_candidates_b(
                snapshot=snap, free_slots=free_slots, excluded_sector_keys=excl_sec
            ),
            {},
        )

    def _sel_s60(
        _dt: pd.Timestamp,
        snap: DailySignalSnapshot,
        free_slots: int,
        excl_sec: set[tuple[str, str]],
        _excl_codes: set[str],
    ) -> tuple[list[dict], dict[str, str]]:
        return (
            select_weekly_etf_candidates_simple60(
                snapshot=snap, free_slots=free_slots, excluded_sector_keys=excl_sec
            ),
            {},
        )

    policy_results: dict[str, E0PortfolioResult] = {}

    # 1. Primary 30,000,000 KRW runs: Multi-region B and B-simple60
    b_res = run_e0_simulation(
        policy_name="B_sector_etf_30m",
        calendar=calendar,
        mapping=mapping,
        bars_by_code=bars_by_code,
        cost_schedule=cost_schedule,
        eval_start=eval_start,
        eval_end=eval_end,
        candidate_selector=_sel_b,
        initial_capital=PRIMARY_ACCOUNT_KRW,
        precomputed_snapshots=snapshots_by_day,
        precomputed_close_sma20=precomputed_etf_close_sma20,
    )
    policy_results[b_res.policy_name] = b_res

    s60_res = run_e0_simulation(
        policy_name="B_simple60_30m",
        calendar=calendar,
        mapping=mapping,
        bars_by_code=bars_by_code,
        cost_schedule=cost_schedule,
        eval_start=eval_start,
        eval_end=eval_end,
        candidate_selector=_sel_s60,
        initial_capital=PRIMARY_ACCOUNT_KRW,
        precomputed_snapshots=snapshots_by_day,
        precomputed_close_sma20=precomputed_etf_close_sma20,
    )
    policy_results[s60_res.policy_name] = s60_res

    # 2. Separate 2x cost stress & +1 session delay stress (§9 lines 147-148, 151)
    b_cost2x = run_e0_simulation(
        policy_name="B_sector_etf_cost2x",
        calendar=calendar,
        mapping=mapping,
        bars_by_code=bars_by_code,
        cost_schedule=cost_schedule,
        eval_start=eval_start,
        eval_end=eval_end,
        candidate_selector=_sel_b,
        initial_capital=PRIMARY_ACCOUNT_KRW,
        cost_multiplier=2.0,
        precomputed_snapshots=snapshots_by_day,
        precomputed_close_sma20=precomputed_etf_close_sma20,
        collect_observation_summary=False,
    )
    policy_results[b_cost2x.policy_name] = b_cost2x

    s60_cost2x = run_e0_simulation(
        policy_name="B_simple60_cost2x",
        calendar=calendar,
        mapping=mapping,
        bars_by_code=bars_by_code,
        cost_schedule=cost_schedule,
        eval_start=eval_start,
        eval_end=eval_end,
        candidate_selector=_sel_s60,
        initial_capital=PRIMARY_ACCOUNT_KRW,
        cost_multiplier=2.0,
        precomputed_snapshots=snapshots_by_day,
        precomputed_close_sma20=precomputed_etf_close_sma20,
        collect_observation_summary=False,
    )
    policy_results[s60_cost2x.policy_name] = s60_cost2x

    b_delay1d = run_e0_simulation(
        policy_name="B_sector_etf_delay1d",
        calendar=calendar,
        mapping=mapping,
        bars_by_code=bars_by_code,
        cost_schedule=cost_schedule,
        eval_start=eval_start,
        eval_end=eval_end,
        candidate_selector=_sel_b,
        initial_capital=PRIMARY_ACCOUNT_KRW,
        execution_delay_sessions=1,
        precomputed_snapshots=snapshots_by_day,
        precomputed_close_sma20=precomputed_etf_close_sma20,
        collect_observation_summary=False,
    )
    policy_results[b_delay1d.policy_name] = b_delay1d

    s60_delay1d = run_e0_simulation(
        policy_name="B_simple60_delay1d",
        calendar=calendar,
        mapping=mapping,
        bars_by_code=bars_by_code,
        cost_schedule=cost_schedule,
        eval_start=eval_start,
        eval_end=eval_end,
        candidate_selector=_sel_s60,
        initial_capital=PRIMARY_ACCOUNT_KRW,
        execution_delay_sessions=1,
        precomputed_snapshots=snapshots_by_day,
        precomputed_close_sma20=precomputed_etf_close_sma20,
        collect_observation_summary=False,
    )
    policy_results[s60_delay1d.policy_name] = s60_delay1d

    # 3. Separate 1,000,000 KRW small-account diagnostic scenario (§5 line 66)
    b_small_1m = run_e0_simulation(
        policy_name="B_sector_etf_small_1m",
        calendar=calendar,
        mapping=mapping,
        bars_by_code=bars_by_code,
        cost_schedule=cost_schedule,
        eval_start=eval_start,
        eval_end=eval_end,
        candidate_selector=_sel_b,
        initial_capital=SMALL_ACCOUNT_KRW,
        precomputed_snapshots=snapshots_by_day,
        precomputed_close_sma20=precomputed_etf_close_sma20,
        collect_observation_summary=False,
    )
    policy_results[b_small_1m.policy_name] = b_small_1m

    b_summary = summarize_portfolio_result(b_res, eval_start, eval_end, window_audit=window_audit)

    # 4. Parallel stock comparisons B_pit_matched, C, and A-matched on identical PIT scope (§4)
    b_pit_matched_summary = None
    c_summary = None
    a_matched_summary = None
    eff_pit_regions = tuple(
        validated.input_audit["effective"]["constituents_pit_verified_regions"]  # type: ignore[index]
    )

    if pit_constituents is None:
        c_vs_b_comparison: dict = {"status": "NOT_IMPLEMENTED_NO_PIT_CONSTITUENTS"}
    elif not bool(pit_constituents.is_pit_verified) or not eff_pit_regions:
        c_vs_b_comparison = {"status": "NOT_IMPLEMENTED_CONSTITUENTS_NOT_PIT_VERIFIED"}
    else:
        common_pit_regions: list[str] = []
        common_pit_sector_keys: set[tuple[str, str]] = set()
        insufficient_pit_regions: dict[str, dict] = {}

        for reg in eff_pit_regions:
            mapped_in_reg = {(r, s) for (r, s) in mapping.representatives if r == reg}
            covered_in_reg = {
                k
                for k in mapped_in_reg
                if pit_constituents.constituents_as_of(k[0], k[1], sessions[0]) is not None
            }
            cov_ratio = (len(covered_in_reg) / len(mapped_in_reg)) if mapped_in_reg else 0.0
            if (
                len(covered_in_reg) >= MIN_REGIONAL_REPRESENTATIVES
                and cov_ratio >= MIN_REGIONAL_COVERAGE_RATIO
            ):
                common_pit_regions.append(reg)
                common_pit_sector_keys.update(covered_in_reg)
            else:
                insufficient_pit_regions[reg] = {
                    "mapped_sectors": len(mapped_in_reg),
                    "pit_covered_sectors": len(covered_in_reg),
                    "coverage_ratio": round(cov_ratio, 6),
                }

        unverified_regions = sorted(set(mapping.benchmarks.keys()) - set(common_pit_regions))

        if not common_pit_sector_keys:
            c_vs_b_comparison = {
                "status": "NOT_IMPLEMENTED_INSUFFICIENT_COMMON_PIT_SCOPE",
                "pit_verified_regions": list(eff_pit_regions),
                "insufficient_pit_regions": insufficient_pit_regions,
                "unimplemented_regions_no_pit": unverified_regions,
            }
        else:
            common_regs_tuple = tuple(sorted(common_pit_regions))
            frozen_common_keys = set(common_pit_sector_keys)

            def _sel_b_pit_matched(
                _dt: pd.Timestamp,
                snap: DailySignalSnapshot,
                free_slots: int,
                excl_sec: set[tuple[str, str]],
                _excl_codes: set[str],
            ) -> tuple[list[dict], dict[str, str]]:
                return (
                    select_weekly_etf_candidates_b(
                        snapshot=snap,
                        free_slots=free_slots,
                        excluded_sector_keys=excl_sec,
                        allowed_sector_keys=frozen_common_keys,
                    ),
                    {},
                )

            def _sel_c(
                dt: pd.Timestamp,
                snap: DailySignalSnapshot,
                free_slots: int,
                excl_sec: set[tuple[str, str]],
                excl_codes: set[str],
            ) -> tuple[list[dict], dict[str, str]]:
                return select_weekly_stock_candidates_c(
                    eval_date=dt,
                    snapshot=snap,
                    pit_constituents=pit_constituents,
                    bars_by_code=bars_by_code,
                    free_slots=free_slots,
                    excluded_sector_keys=excl_sec,
                    excluded_codes=excl_codes,
                    pit_verified_regions=common_regs_tuple,
                    allowed_sector_keys=frozen_common_keys,
                )

            def _sel_a_matched(
                dt: pd.Timestamp,
                snap: DailySignalSnapshot,
                free_slots: int,
                excl_sec: set[tuple[str, str]],
                excl_codes: set[str],
            ) -> tuple[list[dict], dict[str, str]]:
                return (
                    select_weekly_stock_candidates_a_matched(
                        eval_date=dt,
                        snapshot=snap,
                        mapping=mapping,
                        pit_constituents=pit_constituents,
                        bars_by_code=bars_by_code,
                        free_slots=free_slots,
                        excluded_codes=excl_codes,
                        pit_verified_regions=common_regs_tuple,
                        excluded_sector_keys=excl_sec,
                        allowed_sector_keys=frozen_common_keys,
                    ),
                    {},
                )

            b_pit_matched_res = run_e0_simulation(
                policy_name="B_sector_etf_pit_matched_30m",
                calendar=calendar,
                mapping=mapping,
                bars_by_code=bars_by_code,
                cost_schedule=cost_schedule,
                eval_start=eval_start,
                eval_end=eval_end,
                candidate_selector=_sel_b_pit_matched,
                initial_capital=PRIMARY_ACCOUNT_KRW,
                precomputed_snapshots=snapshots_by_day,
                precomputed_close_sma20=precomputed_etf_close_sma20,
                collect_observation_summary=False,
            )
            policy_results[b_pit_matched_res.policy_name] = b_pit_matched_res

            c_res = run_e0_simulation(
                policy_name="C_sector_stock_30m",
                calendar=calendar,
                mapping=mapping,
                bars_by_code=bars_by_code,
                cost_schedule=cost_schedule,
                eval_start=eval_start,
                eval_end=eval_end,
                candidate_selector=_sel_c,
                initial_capital=PRIMARY_ACCOUNT_KRW,
                precomputed_snapshots=snapshots_by_day,
                collect_observation_summary=False,
            )
            policy_results[c_res.policy_name] = c_res

            a_res = run_e0_simulation(
                policy_name="A_matched_no_sector_gate_30m",
                calendar=calendar,
                mapping=mapping,
                bars_by_code=bars_by_code,
                cost_schedule=cost_schedule,
                eval_start=eval_start,
                eval_end=eval_end,
                candidate_selector=_sel_a_matched,
                initial_capital=PRIMARY_ACCOUNT_KRW,
                precomputed_snapshots=snapshots_by_day,
                collect_observation_summary=False,
            )
            policy_results[a_res.policy_name] = a_res

            b_pit_matched_summary = summarize_portfolio_result(
                b_pit_matched_res, eval_start, eval_end, window_audit=window_audit
            )
            c_summary = summarize_portfolio_result(
                c_res, eval_start, eval_end, window_audit=window_audit
            )
            a_matched_summary = summarize_portfolio_result(
                a_res, eval_start, eval_end, window_audit=window_audit
            )

            c_diff_vs_b_matched = (
                c_res.daily_ledger["daily_net_return"].to_numpy(dtype=float)
                - b_pit_matched_res.daily_ledger["daily_net_return"].to_numpy(dtype=float)
            )
            c_mbb = moving_block_bootstrap_ci(
                c_diff_vs_b_matched,
                block_len=bootstrap_block_len,
                n_boot=bootstrap_reps,
                seed=seed,
            )
            protocol_mbb_ok = bool(
                bootstrap_block_len == BOOTSTRAP_BLOCK_LEN
                and bootstrap_reps == BOOTSTRAP_REPS
                and seed == FIXED_RESEARCH_SEED
            )
            c_superior = bool(
                protocol_mbb_ok
                and meets_24m
                and c_mbb.get("valid")
                and (c_mbb.get("ci_lower") or 0.0) > 0.0
                and c_summary["annualized_net_return"]
                > b_pit_matched_summary["annualized_net_return"]
                and c_summary["mark_quality"]["all_held_marks_observed"]
                and c_summary["max_drawdown"] <= MAX_ALLOWED_MDD
            )
            c_vs_b_comparison = {
                "status": (
                    "C_SUPERIORITY_CANDIDATE_IN_VERIFIED_REGIONS"
                    if c_superior
                    else "C_HOLD_NO_SUPERIORITY_OVER_B"
                ),
                "comparison_reference_policy": "B_sector_etf_pit_matched_30m",
                "common_pit_regions": list(common_regs_tuple),
                "common_pit_sectors": [
                    f"{r}::{s}" for (r, s) in sorted(frozen_common_keys)
                ],
                "insufficient_pit_regions": insufficient_pit_regions,
                "unimplemented_regions_no_pit": unverified_regions,
                "entered_regions_by_policy": {
                    "b_full_multiregion_30m": b_summary["entered_regions"],
                    "b_pit_matched_30m": b_pit_matched_summary["entered_regions"],
                    "c_30m": c_summary["entered_regions"],
                    "a_matched_30m": a_matched_summary["entered_regions"],
                },
                "exposure_and_drop_comparison": {
                    "b_pit_matched_30m": {
                        "avg_gross_exposure_ratio": b_pit_matched_summary[
                            "avg_gross_exposure_ratio"
                        ],
                        "avg_cash_ratio": b_pit_matched_summary["avg_cash_ratio"],
                        "zero_qty_drop_rate": b_pit_matched_summary["zero_qty_drop_rate"],
                    },
                    "c_30m": {
                        "avg_gross_exposure_ratio": c_summary["avg_gross_exposure_ratio"],
                        "avg_cash_ratio": c_summary["avg_cash_ratio"],
                        "zero_qty_drop_rate": c_summary["zero_qty_drop_rate"],
                    },
                    "a_matched_30m": {
                        "avg_gross_exposure_ratio": a_matched_summary["avg_gross_exposure_ratio"],
                        "avg_cash_ratio": a_matched_summary["avg_cash_ratio"],
                        "zero_qty_drop_rate": a_matched_summary["zero_qty_drop_rate"],
                    },
                },
                "c_minus_b_pit_matched_ann_return": (
                    c_summary["annualized_net_return"]
                    - b_pit_matched_summary["annualized_net_return"]
                ),
                "c_minus_b_mbb_ci95": c_mbb,
                "sector_gate_ablation_c_minus_a_matched_ann_return": (
                    c_summary["annualized_net_return"]
                    - a_matched_summary["annualized_net_return"]
                ),
            }

    # 5. Moving Block Bootstrap & Random-1000
    s60_summary = summarize_portfolio_result(
        s60_res, eval_start, eval_end, window_audit=window_audit
    )
    b_cost2x_summary = summarize_portfolio_result(
        b_cost2x, eval_start, eval_end, window_audit=window_audit
    )
    s60_cost2x_summary = summarize_portfolio_result(
        s60_cost2x, eval_start, eval_end, window_audit=window_audit
    )
    b_delay1d_summary = summarize_portfolio_result(
        b_delay1d, eval_start, eval_end, window_audit=window_audit
    )
    s60_delay1d_summary = summarize_portfolio_result(
        s60_delay1d, eval_start, eval_end, window_audit=window_audit
    )
    b_small_summary = summarize_portfolio_result(
        b_small_1m, eval_start, eval_end, window_audit=window_audit
    )

    daily_diff_b_vs_s60 = (
        b_res.daily_ledger["daily_net_return"].to_numpy(dtype=float)
        - s60_res.daily_ledger["daily_net_return"].to_numpy(dtype=float)
    )
    mbb_b_vs_s60 = moving_block_bootstrap_ci(
        daily_diff_b_vs_s60,
        block_len=bootstrap_block_len,
        n_boot=bootstrap_reps,
        seed=seed,
    )
    random_bench = run_random_policy_benchmark(
        calendar=calendar,
        mapping=mapping,
        bars_by_code=bars_by_code,
        cost_schedule=cost_schedule,
        eval_start=eval_start,
        eval_end=eval_end,
        precomputed_snapshots=snapshots_by_day,
        precomputed_close_sma20=precomputed_etf_close_sma20,
        initial_capital=PRIMARY_ACCOUNT_KRW,
        n_policies=n_random_policies,
        seed=seed,
    )
    passive_refs = compute_passive_references(
        sessions=sessions,
        mapping=mapping,
        bars_by_code=bars_by_code,
        eval_start=eval_start,
        eval_end=eval_end,
    )

    verdict = classify_v1_verdict(
        is_uninspected_oos=is_uninspected_oos,
        meets_24_months=meets_24m,
        closed_episodes_count=b_summary["episode_metrics"]["closed_episode_count"],
        provenance=provenance,
        b_cum_net_return=b_summary["cumulative_net_return"],
        b_ann_net_return=b_summary["annualized_net_return"],
        simple60_ann_net_return=s60_summary["annualized_net_return"],
        mbb_result=mbb_b_vs_s60,
        b_mdd=b_summary["max_drawdown"],
        b_profit_factor=b_summary["episode_metrics"]["profit_factor"],
        b_profit_factor_status=b_summary["episode_metrics"]["profit_factor_status"],
        b_cost2x_cum_net_return=b_cost2x_summary["cumulative_net_return"],
        simple60_cost2x_cum_net_return=s60_cost2x_summary["cumulative_net_return"],
        b_delay1d_cum_net_return=b_delay1d_summary["cumulative_net_return"],
        simple60_delay1d_cum_net_return=s60_delay1d_summary["cumulative_net_return"],
        random_p95_cum_net_return=random_bench["p95_cumulative_net_return"],
        random_benchmark=random_bench,
        input_audit=validated.input_audit,
        window_audit=window_audit,
        b_mdd_quality_status=b_summary["mark_quality"]["mdd_quality_status"],
    )

    policy_ledgers = {
        name: _serialize_policy_result(res) for name, res in sorted(policy_results.items())
    }
    daily_signals_serialized = [
        _serialize_daily_snapshot(snapshots_by_day[t]) for t in sessions
    ]

    window_section = {
        "eval_start": window_audit["requested_eval_start"],
        "eval_end": window_audit["requested_eval_end"],
        "actual_eval_start": window_audit["actual_eval_start"],
        "actual_eval_end": window_audit["actual_eval_end"],
        "scheduled_sessions": window_audit["scheduled_sessions"],
        "leading_gap_days": window_audit["leading_gap_days"],
        "trailing_gap_days": window_audit["trailing_gap_days"],
        "max_internal_gap_days": window_audit["max_internal_gap_days"],
        "sparse_full_months": window_audit["sparse_full_months"],
        "unscheduled_benchmark_bar_sessions": window_audit.get(
            "unscheduled_benchmark_bar_sessions", 0
        ),
        "unscheduled_instrument_bar_sessions": window_audit.get(
            "unscheduled_instrument_bar_sessions", 0
        ),
        "unscheduled_bar_sessions_total": window_audit.get("unscheduled_bar_sessions_total", 0),
        "calendar_issues": window_audit.get("calendar_issues", []),
        "price_data_issues": window_audit.get("price_data_issues", []),
        "calendar_complete_for_requested_window": window_audit[
            "calendar_complete_for_requested_window"
        ],
        "benchmark_session_coverage": window_audit["benchmark_session_coverage"],
        "max_benchmark_missing_ratio": window_audit["max_benchmark_missing_ratio"],
        "requested_months_approx": window_audit["requested_months_approx"],
        "months_approx": window_audit["months_approx"],
        "meets_24_months": meets_24m,
        "is_uninspected_oos": bool(is_uninspected_oos),
    }

    raw_report = {
        "execution_model": "E0_HISTORICAL_OPEN_APPROXIMATION_NOT_REAL_MANUAL_EXECUTION",
        "evaluation_mode": verdict["evaluation_mode"],
        "pre_run_seal": pre_run_seal,
        "ledger_hashes": pre_run_seal["ledger_hashes"],
        "input_audit": dict(validated.input_audit),
        "window": window_section,
        "verdict": verdict,
        "primary_b_30m": b_summary,
        "benchmark_simple60_30m": s60_summary,
        "stress_cost_2x": {
            "b_30m": b_cost2x_summary,
            "simple60_30m": s60_cost2x_summary,
        },
        "stress_delay_1d": {
            "b_30m": b_delay1d_summary,
            "simple60_30m": s60_delay1d_summary,
        },
        "small_account_1m_diagnostic": b_small_summary,
        "mbb_20d_b_minus_simple60": mbb_b_vs_s60,
        "random_policy_benchmark": random_bench,
        "passive_references": passive_refs,
        "stock_comparisons": {
            "b_pit_matched_30m": b_pit_matched_summary,
            "c_30m": c_summary,
            "a_matched_30m": a_matched_summary,
            "c_vs_b": c_vs_b_comparison,
        },
        "daily_signals": daily_signals_serialized,
        "policy_ledgers": policy_ledgers,
    }
    cleaned = _clean_for_json(raw_report)
    out_report = EvaluationReport(cleaned)  # type: ignore[arg-type]
    out_report._validated_inputs = validated
    out_report._raw_policy_results = policy_results
    out_report._daily_snapshots = snapshots_by_day
    return out_report


def _extract_and_verify_seal_core(pre_seal_doc: Mapping[str, object]) -> tuple[str, dict]:
    """Verify cryptographic self-consistency of a `pre_run_seal` document and return `(sha256, core)`."""
    required_keys = (
        "pre_run_seal_sha256",
        "spec_task_id",
        "spec_version",
        "ledger_hashes",
        "evaluation_config",
        "input_audit",
        "window_audit",
    )
    for k in required_keys:
        if k not in pre_seal_doc:
            raise ValueError(f"PRE_RUN_SEAL_MISSING_KEY:{k}")
    core = _clean_for_json(
        {
            "spec_task_id": pre_seal_doc["spec_task_id"],
            "spec_version": pre_seal_doc["spec_version"],
            "ledger_hashes": pre_seal_doc["ledger_hashes"],
            "evaluation_config": pre_seal_doc["evaluation_config"],
            "input_audit": pre_seal_doc["input_audit"],
            "window_audit": pre_seal_doc["window_audit"],
        }
    )
    assert isinstance(core, dict)
    recomputed_sha = sha256_bytes(canonical_json_bytes(core))
    claimed_sha = str(pre_seal_doc["pre_run_seal_sha256"])
    if recomputed_sha != claimed_sha:
        raise ValueError(
            f"PRE_RUN_SEAL_SELF_HASH_MISMATCH:claimed={claimed_sha}:computed={recomputed_sha}"
        )
    return recomputed_sha, core


def _verify_existing_sealed_dir_integrity(run_dir: Path) -> dict:
    completed_path = run_dir / "SEAL_COMPLETED.json"
    manifest_path = run_dir / "artifact_manifest.json"
    if not completed_path.is_file() or not manifest_path.is_file():
        raise RuntimeError(f"INCOMPLETE_OR_CORRUPTED_FINALIZED_RUN_DIR:{run_dir}")
    completed_doc = json.loads(completed_path.read_text(encoding="utf-8"))
    manifest_sha_actual = sha256_file(manifest_path)
    if completed_doc.get("artifact_manifest_sha256") != manifest_sha_actual:
        raise ValueError(f"FINALIZED_MANIFEST_HASH_MISMATCH:{run_dir}")
    manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    for rel_path, expected_sha in sorted(manifest_doc["files"].items()):
        target = run_dir / rel_path
        if not target.is_file():
            raise FileNotFoundError(f"MISSING_SEALED_ARTIFACT:{rel_path}")
        if sha256_file(target) != expected_sha:
            raise ValueError(f"SEALED_ARTIFACT_HASH_MISMATCH:{rel_path}")
    return manifest_doc


def _write_post_simulation_artifacts(
    *,
    target_dir: Path,
    attempt_no: int,
    run_dir_name: str,
    evaluation_report: Mapping[str, object],
    report_sha: str,
    pre_run_seal: Mapping[str, object],
    daily_signals: Sequence[object],
    policy_ledgers: Mapping[str, object],
) -> tuple[str, Path]:
    signals_path = target_dir / "daily_signals.jsonl"
    with signals_path.open("wb") as fh:
        for day_obj in daily_signals:
            fh.write(canonical_json_bytes(day_obj) + b"\n")

    ledgers_dir = target_dir / "ledgers"
    ledgers_dir.mkdir(parents=True, exist_ok=True)
    for pol_name, pol_data in sorted(policy_ledgers.items()):
        assert isinstance(pol_data, Mapping)
        dl_df = pd.DataFrame(pol_data["daily_ledger"])
        dl_df.to_csv(ledgers_dir / f"{pol_name}_daily_ledger.csv", index=False)
        (ledgers_dir / f"{pol_name}_closed_episodes.json").write_text(
            json.dumps(
                pol_data["closed_episodes"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (ledgers_dir / f"{pol_name}_open_positions.json").write_text(
            json.dumps(
                pol_data["open_positions_at_end"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (ledgers_dir / f"{pol_name}_order_events.json").write_text(
            json.dumps(
                pol_data["order_events"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )

    report_path = target_dir / "evaluation_report.json"
    report_path.write_text(
        json.dumps(
            evaluation_report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        + "\n",
        encoding="utf-8",
    )

    artifact_hashes: dict[str, str] = {}
    for fpath in sorted(target_dir.rglob("*")):
        if fpath.is_file() and fpath.name not in {"artifact_manifest.json", "SEAL_COMPLETED.json"}:
            rel = fpath.relative_to(target_dir).as_posix()
            artifact_hashes[rel] = sha256_file(fpath)

    manifest_path = target_dir / "artifact_manifest.json"
    manifest_payload = {
        "attempt_number": attempt_no,
        "run_dir": run_dir_name,
        "report_sha256": report_sha,
        "pre_run_seal_sha256": pre_run_seal["pre_run_seal_sha256"],
        "files": artifact_hashes,
    }
    manifest_path.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    manifest_sha = sha256_file(manifest_path)

    completed_payload = {
        "attempt_number": attempt_no,
        "run_dir": run_dir_name,
        "pre_run_seal_sha256": pre_run_seal["pre_run_seal_sha256"],
        "report_sha256": report_sha,
        "artifact_manifest_sha256": manifest_sha,
        "finalized_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (target_dir / "SEAL_COMPLETED.json").write_text(
        json.dumps(completed_payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    return manifest_sha, report_path


def seal_and_register_evaluation_run(
    out_root: Path,
    evaluation_report: Mapping[str, object],
    run_label: str = "v1_0_e0",
    *,
    validated_inputs: ValidatedResearchInputs | None = None,
    pre_sealed_run: Mapping[str, object] | None = None,
) -> dict[str, str | int | bool]:
    """Persist full replayable artifacts atomically and register the attempt in `attempt_registry.json` (§0, §8, §9, §10).

    Invariants enforced BEFORE writing any file:
    1. Recomputes and verifies `pre_run_seal_sha256` and `ledger_hashes` inside `evaluation_report`.
    2. When `pre_sealed_run` is provided, verifies exact equality of `pre_run_seal_sha256`,
       `ledger_hashes`, and `evaluation_config` (including `seed`) across the handle, on-disk
       `pre_run_input_seal.json`, and `evaluation_report`. Any mismatch raises `ValueError`
       before touching disk.
    3. If the attempt directory is already finalized (`SEAL_COMPLETED.json` / `evaluation_report.json`
       / `attempt_number` in registry):
       - Different `report_sha256` -> raises `FileExistsError` without modifying any file.
       - Identical `report_sha256` -> verifies existing artifact hashes and returns existing registration
         with `idempotent_retry=True` without overwriting files or duplicating `attempt_number`.
    4. Stages all outputs in a temporary directory and swaps atomically so partial failures never
       corrupt a pre-sealed or finalized run directory.
    """
    out_root = Path(out_root).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    registry_path = out_root / "attempt_registry.json"

    eff_validated = validated_inputs
    if eff_validated is None and isinstance(evaluation_report, EvaluationReport):
        eff_validated = evaluation_report._validated_inputs

    policy_ledgers = evaluation_report.get("policy_ledgers")
    daily_signals = evaluation_report.get("daily_signals")
    pre_run_seal = evaluation_report.get("pre_run_seal")
    if (
        not isinstance(policy_ledgers, Mapping)
        or not policy_ledgers
        or not isinstance(daily_signals, list)
        or not isinstance(pre_run_seal, Mapping)
    ):
        raise ValueError("EVALUATION_REPORT_MISSING_LEDGER_OR_SIGNAL_REPLAY_ARTIFACTS")
    if eff_validated is None and pre_sealed_run is None:
        raise ValueError("MISSING_VALIDATED_INPUTS_FOR_STANDALONE_REPLAY_SNAPSHOT")

    report_seal_sha, report_seal_core = _extract_and_verify_seal_core(pre_run_seal)
    if _clean_for_json(evaluation_report.get("ledger_hashes")) != report_seal_core["ledger_hashes"]:
        raise ValueError("EVALUATION_REPORT_LEDGER_HASHES_MISMATCH_WITH_SEAL")

    if registry_path.exists():
        registry_doc = json.loads(registry_path.read_text(encoding="utf-8"))
        attempts = list(registry_doc.get("attempts", []))
    else:
        attempts = []

    report_bytes = canonical_json_bytes(evaluation_report)
    report_sha = sha256_bytes(report_bytes)
    short_sha = report_sha[:12]

    if pre_sealed_run is not None:
        attempt_no = int(pre_sealed_run["attempt_number"])  # type: ignore[arg-type]
        run_dir = Path(pre_sealed_run["run_dir"]).resolve()  # type: ignore[arg-type]
        run_dir_name = run_dir.name
        if run_dir.parent != out_root:
            raise ValueError(f"PRE_SEALED_RUN_DIR_OUTSIDE_OUT_ROOT:{run_dir}!={out_root}")
        if not run_dir.is_dir():
            raise FileNotFoundError(f"PRE_SEALED_RUN_DIR_MISSING:{run_dir}")

        handle_pre_seal = pre_sealed_run.get("pre_run_seal")
        if not isinstance(handle_pre_seal, Mapping):
            raise ValueError("PRE_SEALED_HANDLE_MISSING_SEAL_PAYLOAD")
        handle_seal_sha, handle_seal_core = _extract_and_verify_seal_core(handle_pre_seal)

        disk_seal_path = run_dir / "pre_run_input_seal.json"
        if not disk_seal_path.is_file():
            raise FileNotFoundError(f"MISSING_DISK_PRE_RUN_SEAL:{disk_seal_path}")
        disk_pre_seal = json.loads(disk_seal_path.read_text(encoding="utf-8"))
        disk_seal_sha, disk_seal_core = _extract_and_verify_seal_core(disk_pre_seal)

        if not (
            handle_seal_sha == disk_seal_sha == report_seal_sha
            and handle_seal_core == disk_seal_core == report_seal_core
        ):
            raise ValueError(
                "PRE_RUN_SEAL_MISMATCH_WITH_EVALUATION_REPORT:"
                f"handle={handle_seal_sha[:12]}:disk={disk_seal_sha[:12]}:report={report_seal_sha[:12]}"
            )

        already_finalized_on_disk = any(
            (run_dir / marker).exists()
            for marker in (
                "SEAL_COMPLETED.json",
                "artifact_manifest.json",
                "evaluation_report.json",
                "daily_signals.jsonl",
            )
        )
        matching_reg_entries = [
            a for a in attempts if int(a.get("attempt_number", -1)) == attempt_no
        ]
        if already_finalized_on_disk or matching_reg_entries:
            existing_manifest = _verify_existing_sealed_dir_integrity(run_dir)
            existing_report_sha = str(existing_manifest["report_sha256"])
            if report_sha != existing_report_sha:
                raise FileExistsError(
                    f"ATTEMPT_ALREADY_FINALIZED_NEW_ATTEMPT_NUMBER_REQUIRED:{run_dir_name}"
                )
            if len(matching_reg_entries) != 1:
                raise RuntimeError(f"CORRUPTED_ATTEMPT_REGISTRY_FOR_ATTEMPT:{attempt_no}")
            return {
                "attempt_number": attempt_no,
                "run_dir": str(run_dir),
                "report_path": str(run_dir / "evaluation_report.json"),
                "report_sha256": existing_report_sha,
                "artifact_manifest_sha256": sha256_file(run_dir / "artifact_manifest.json"),
                "idempotent_retry": True,
            }

        # Stage atomically from the pre-sealed directory so partial failure never alters `run_dir`
        stage_dir = out_root / f".staging_{run_dir_name}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        backup_dir = out_root / f".backup_{run_dir_name}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        try:
            shutil.copytree(run_dir, stage_dir)
            manifest_sha, _ = _write_post_simulation_artifacts(
                target_dir=stage_dir,
                attempt_no=attempt_no,
                run_dir_name=run_dir_name,
                evaluation_report=evaluation_report,
                report_sha=report_sha,
                pre_run_seal=disk_pre_seal,
                daily_signals=daily_signals,
                policy_ledgers=policy_ledgers,
            )
            os.replace(run_dir, backup_dir)
            try:
                os.replace(stage_dir, run_dir)
            except Exception:
                if backup_dir.exists() and not run_dir.exists():
                    os.replace(backup_dir, run_dir)
                raise
            shutil.rmtree(backup_dir, ignore_errors=True)
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
        report_path = run_dir / "evaluation_report.json"
        effective_sealed_ts = disk_pre_seal.get("sealed_before_simulation_at_utc")

    else:
        pre_seals_path = out_root / "pre_run_seals.jsonl"
        pre_count = (
            sum(
                1
                for ln in pre_seals_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            )
            if pre_seals_path.exists()
            else 0
        )
        max_reg_no = max((int(a.get("attempt_number", 0)) for a in attempts), default=0)
        attempt_no = max(len(attempts), max_reg_no, pre_count) + 1
        eval_mode = str(evaluation_report.get("evaluation_mode", "FROZEN_V1_PROTOCOL"))
        eff_label = (
            run_label if eval_mode == "FROZEN_V1_PROTOCOL" else f"{run_label}_diagnostic"
        )
        run_dir_name = f"attempt_{attempt_no:04d}_{eff_label}_{short_sha}"
        run_dir = out_root / run_dir_name
        if run_dir.exists():
            raise FileExistsError(f"IMMUTABLE_RUN_DIR_ALREADY_EXISTS:{run_dir}")

        stage_dir = out_root / f".staging_{run_dir_name}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        try:
            stage_dir.mkdir(parents=True, exist_ok=False)
            (stage_dir / "pre_run_input_seal.json").write_text(
                json.dumps(
                    pre_run_seal, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
                )
                + "\n",
                encoding="utf-8",
            )
            assert eff_validated is not None
            _write_inputs_snapshot(
                snapshot_dir=stage_dir / "inputs_snapshot",
                validated=eff_validated,
                eval_cfg=pre_run_seal["evaluation_config"],  # type: ignore[index]
            )
            manifest_sha, _ = _write_post_simulation_artifacts(
                target_dir=stage_dir,
                attempt_no=attempt_no,
                run_dir_name=run_dir_name,
                evaluation_report=evaluation_report,
                report_sha=report_sha,
                pre_run_seal=pre_run_seal,
                daily_signals=daily_signals,
                policy_ledgers=policy_ledgers,
            )
            os.replace(stage_dir, run_dir)
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
        report_path = run_dir / "evaluation_report.json"
        effective_sealed_ts = pre_run_seal.get("sealed_before_simulation_at_utc")

    verdict_obj = evaluation_report.get("verdict", {})
    verdict_code = (
        verdict_obj.get("verdict_code", "UNKNOWN") if isinstance(verdict_obj, dict) else "UNKNOWN"
    )
    ledger_hashes = evaluation_report.get("ledger_hashes", {})

    entry = {
        "attempt_number": attempt_no,
        "sealed_before_simulation_at_utc": effective_sealed_ts,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "spec_version": SPEC_VERSION,
        "evaluation_mode": evaluation_report.get("evaluation_mode"),
        "run_dir": run_dir_name,
        "pre_run_seal_sha256": report_seal_sha,
        "report_sha256": report_sha,
        "artifact_manifest_sha256": manifest_sha,
        "verdict_code": verdict_code,
        "ledger_hashes": ledger_hashes,
    }
    attempts.append(entry)
    registry_payload = {
        "task_id": SPEC_TASK_ID,
        "spec_version": SPEC_VERSION,
        "total_attempts": len(attempts),
        "attempts": attempts,
    }
    reg_tmp = registry_path.with_name(f"attempt_registry.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    reg_tmp.write_text(
        json.dumps(registry_payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    os.replace(reg_tmp, registry_path)
    return {
        "attempt_number": attempt_no,
        "run_dir": str(run_dir),
        "report_path": str(report_path),
        "report_sha256": report_sha,
        "artifact_manifest_sha256": manifest_sha,
        "idempotent_retry": False,
    }


def replay_sealed_evaluation_run(run_dir: Path, *, repo_root: Path) -> dict:
    """Re-execute a sealed attempt directory WITHOUT original input paths and verify exact ledger/episode parity."""
    run_dir = Path(run_dir).resolve()
    manifest_path = run_dir / "artifact_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"MISSING_ARTIFACT_MANIFEST:{manifest_path}")

    manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    for rel_path, expected_sha in sorted(manifest_doc["files"].items()):
        target = run_dir / rel_path
        if not target.is_file():
            raise FileNotFoundError(f"MISSING_SEALED_ARTIFACT:{rel_path}")
        if sha256_file(target) != expected_sha:
            raise ValueError(f"SEALED_ARTIFACT_HASH_MISMATCH:{rel_path}")

    pre_seal_doc = json.loads((run_dir / "pre_run_input_seal.json").read_text(encoding="utf-8"))
    saved_report = json.loads((run_dir / "evaluation_report.json").read_text(encoding="utf-8"))
    eval_cfg = pre_seal_doc["evaluation_config"]

    snap_bundle_path = run_dir / "inputs_snapshot" / "bundle.json"
    loaded = load_research_bundle(
        snap_bundle_path,
        strict=bool(eval_cfg.get("strict_input_validation", True)),
    )

    replayed = run_full_v1_evaluation(
        repo_root=repo_root,
        calendar=loaded["calendar"],
        mapping=loaded["mapping"],
        bars_by_code=loaded["bars_by_code"],
        cost_schedule=loaded["cost_schedule"],
        provenance=loaded["provenance"],
        eval_start=eval_cfg["eval_start"],
        eval_end=eval_cfg["eval_end"],
        pit_constituents=loaded["pit_constituents"],
        is_uninspected_oos=bool(eval_cfg["is_uninspected_oos"]),
        n_random_policies=int(eval_cfg["n_random_policies"]),
        bootstrap_reps=int(eval_cfg["bootstrap_reps"]),
        bootstrap_block_len=int(eval_cfg["bootstrap_block_len"]),
        seed=int(eval_cfg["seed"]),
        validated_inputs=loaded["validated_inputs"],
        strict_input_validation=bool(eval_cfg.get("strict_input_validation", True)),
    )

    if (
        replayed["ledger_hashes"]["bar_data_hashes"]
        != saved_report["ledger_hashes"]["bar_data_hashes"]
    ):
        raise ValueError("REPLAY_BAR_DATA_HASHES_MISMATCH")
    if replayed["verdict"] != saved_report["verdict"]:
        raise ValueError("REPLAY_VERDICT_MISMATCH")
    if replayed["mbb_20d_b_minus_simple60"] != saved_report["mbb_20d_b_minus_simple60"]:
        raise ValueError("REPLAY_MBB_MISMATCH")
    if replayed["random_policy_benchmark"] != saved_report["random_policy_benchmark"]:
        raise ValueError("REPLAY_RANDOM_BENCHMARK_MISMATCH")

    saved_policies = sorted(saved_report["policy_ledgers"].keys())
    replayed_policies = sorted(replayed["policy_ledgers"].keys())
    if saved_policies != replayed_policies:
        raise ValueError(f"REPLAY_POLICY_SET_MISMATCH:{saved_policies}!={replayed_policies}")

    ledgers_dir = run_dir / "ledgers"
    for pol_name in saved_policies:
        saved_csv = pd.read_csv(
            ledgers_dir / f"{pol_name}_daily_ledger.csv", float_precision="round_trip"
        )
        rep_df = pd.DataFrame(replayed["policy_ledgers"][pol_name]["daily_ledger"])
        if list(saved_csv["date"]) != list(rep_df["date"]):
            raise ValueError(f"REPLAY_LEDGER_DATES_MISMATCH:{pol_name}")
        for num_col in ("cash", "market_value", "nav", "daily_net_return"):
            if not np.allclose(
                saved_csv[num_col].to_numpy(dtype=float),
                rep_df[num_col].to_numpy(dtype=float),
                rtol=1e-11,
                atol=1e-8,
            ):
                raise ValueError(f"REPLAY_LEDGER_NUMERIC_MISMATCH:{pol_name}:{num_col}")

        saved_eps = json.loads(
            (ledgers_dir / f"{pol_name}_closed_episodes.json").read_text(encoding="utf-8")
        )
        if saved_eps != replayed["policy_ledgers"][pol_name]["closed_episodes"]:
            raise ValueError(f"REPLAY_CLOSED_EPISODES_MISMATCH:{pol_name}")

        saved_open = json.loads(
            (ledgers_dir / f"{pol_name}_open_positions.json").read_text(encoding="utf-8")
        )
        if saved_open != replayed["policy_ledgers"][pol_name]["open_positions_at_end"]:
            raise ValueError(f"REPLAY_OPEN_POSITIONS_MISMATCH:{pol_name}")

        saved_events = json.loads(
            (ledgers_dir / f"{pol_name}_order_events.json").read_text(encoding="utf-8")
        )
        if saved_events != replayed["policy_ledgers"][pol_name]["order_events"]:
            raise ValueError(f"REPLAY_ORDER_EVENTS_MISMATCH:{pol_name}")

    return {
        "replay_verified": True,
        "run_dir": str(run_dir),
        "verdict_code": replayed["verdict"]["verdict_code"],
        "policies_verified": saved_policies,
    }
