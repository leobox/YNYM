"""Point-in-time signal engine for T-145 v1.0 (§§3, 4, 7, 9).

Strictly uses completed daily bars `<= eval_date` (zero lookahead).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from .models import (
    MIN_HISTORY_BARS,
    MIN_REGIONAL_COVERAGE_RATIO,
    MIN_REGIONAL_REPRESENTATIVES,
    FrozenSectorMapping,
    PITConstituentTable,
)


def pit_split_adjusted_ohlc(hist: pd.DataFrame) -> pd.DataFrame:
    """Express historical bars `<= t` in day-`t` share units using ONLY splits `<= t`.

    At `s == t`, `SplitFactor(s) / SplitFactor(t) == 1.0`, so adjusted Close at `t`
    is identically equal to actual trading Close at `t`, and future splits `> t`
    never leak backward into `<= t`.
    """
    if hist.empty:
        raise ValueError("EMPTY_HIST_FOR_SPLIT_ADJUST")
    sf = hist["SplitFactor"].astype(float)
    s_now = float(sf.iloc[-1])
    if not np.isfinite(s_now) or s_now <= 0.0:
        raise ValueError("INVALID_CURRENT_SPLIT_FACTOR")
    ratio = sf / s_now
    return pd.DataFrame(
        {
            "Open": hist["Open"].astype(float) * ratio,
            "High": hist["High"].astype(float) * ratio,
            "Low": hist["Low"].astype(float) * ratio,
            "Close": hist["Close"].astype(float) * ratio,
            "Volume": hist["Volume"].astype(float),
        },
        index=hist.index,
    )


def compute_wilder_atr20(adj_ohlc: pd.DataFrame, period: int = 20) -> pd.Series:
    """Canonical Wilder ATR(20): SMA seed over first `period` valid TRs, then 1/period smoothing."""
    n = len(adj_ohlc)
    out = np.full(n, np.nan, dtype=float)
    if n < period + 1:
        return pd.Series(out, index=adj_ohlc.index, dtype=float)

    h = adj_ohlc["High"].to_numpy(dtype=float)
    l = adj_ohlc["Low"].to_numpy(dtype=float)
    c = adj_ohlc["Close"].to_numpy(dtype=float)

    prev_c = c[:-1]
    tr = np.maximum(
        h[1:] - l[1:],
        np.maximum(np.abs(h[1:] - prev_c), np.abs(l[1:] - prev_c)),
    )
    if not np.isfinite(tr[:period]).all():
        return pd.Series(out, index=adj_ohlc.index, dtype=float)

    seed = float(np.mean(tr[:period]))
    out[period] = seed
    alpha = 1.0 / float(period)
    prev_atr = seed
    for idx in range(period, len(tr)):
        val = prev_atr * (1.0 - alpha) + float(tr[idx]) * alpha
        out[idx + 1] = val
        prev_atr = val
    return pd.Series(out, index=adj_ohlc.index, dtype=float)


def compute_rs20_series(
    asset_tr_aligned: pd.Series, bench_tr: pd.Series, window: int = 20
) -> pd.Series:
    """`RS20(t) = (P(t) / P(t-20)) / (B(t) / B(t-20)) - 1` on aligned regional sessions."""
    p_ratio = asset_tr_aligned / asset_tr_aligned.shift(window)
    b_ratio = bench_tr / bench_tr.shift(window)
    rs = (p_ratio / b_ratio) - 1.0
    return rs.replace([np.inf, -np.inf], np.nan)


def classify_rs_transition(rs_series: pd.Series, lookback_sessions: int = 10) -> str:
    """Classify RS state at `t` (`§3` line 47):

    - If `RS20(t) <= 0` or NaN: `"RS_NON_POSITIVE"`
    - If `RS20(t) > 0` and any `RS20` in `t-10 .. t-1` was `<= 0`: `"최근 상대강도 전환"`
    - If `RS20(t) > 0` and all `RS20` in `t-10 .. t` were `> 0`: `"강세 지속"`
    - If `t-10 .. t-1` contains NaN: `"전환이력부족"`
    """
    if len(rs_series) < lookback_sessions + 1:
        return "전환이력부족"
    window_vals = rs_series.iloc[-(lookback_sessions + 1) :].to_numpy(dtype=float)
    if not np.isfinite(window_vals).all():
        return "전환이력부족"
    if window_vals[-1] <= 0.0:
        return "RS_NON_POSITIVE"
    prior_vals = window_vals[:-1]
    if np.any(prior_vals <= 0.0):
        return "최근 상대강도 전환"
    return "강세 지속"


@dataclass(frozen=True)
class SectorObservationRow:
    eval_date: str
    region: str
    sector: str
    code: str
    name: str
    close: float
    sma20: float
    sma60: float
    atr20: float
    rs20: float
    rs20_improvement_5d: float
    ret60_total: float
    is_observed: bool
    is_entry_qualified: bool
    rs_transition_label: str
    regime_bullish: bool
    region_coverage_ok: bool


@dataclass(frozen=True)
class DailySignalSnapshot:
    eval_date: str
    region_status: dict[str, str]
    benchmark_metrics: dict[str, dict[str, float | bool]]
    observations: tuple[SectorObservationRow, ...]
    blocked_representatives: tuple[dict[str, str], ...]


def _completed_up_to(df: pd.DataFrame, eval_ts: pd.Timestamp) -> pd.DataFrame:
    sliced = df.loc[:eval_ts]
    if sliced.empty:
        return sliced
    if "Halted" in sliced.columns:
        return sliced.loc[~sliced["Halted"]]
    return sliced


def evaluate_daily_snapshot(
    eval_date: pd.Timestamp | str,
    mapping: FrozenSectorMapping,
    bars_by_code: Mapping[str, pd.DataFrame],
) -> DailySignalSnapshot:
    """Evaluate all regions, benchmarks, coverage gates, and sector observations on `eval_date`.

    Maintains full observation and RS transition records even when regional regime is `< SMA120` (§3, §7).
    Never substitutes another ETF when a locked representative is missing/halted/ineligible (§2).
    """
    t = pd.Timestamp(eval_date).normalize()
    v_from = pd.Timestamp(mapping.valid_from).normalize()
    v_until = pd.Timestamp(mapping.valid_until).normalize()
    if not (v_from <= t <= v_until):
        raise ValueError(
            f"EVAL_DATE_OUTSIDE_MAPPING_LOCK:{t.strftime('%Y-%m-%d')}:"
            f"{v_from.strftime('%Y-%m-%d')}..{v_until.strftime('%Y-%m-%d')}"
        )

    reps_by_region: dict[str, list[tuple[str, object]]] = {r: [] for r in mapping.benchmarks}
    for (region, sector), spec in sorted(mapping.representatives.items()):
        reps_by_region.setdefault(region, []).append((sector, spec))

    region_status: dict[str, str] = {}
    benchmark_metrics: dict[str, dict[str, float | bool]] = {}
    observations: list[SectorObservationRow] = []
    blocked_reps: list[dict[str, str]] = []

    for region, bench_spec in sorted(mapping.benchmarks.items()):
        bench_raw = bars_by_code.get(bench_spec.code)
        if bench_raw is None:
            region_status[region] = "HALTED_BENCHMARK_MISSING"
            continue

        bench_sliced = bench_raw.loc[:t]
        if bench_sliced.empty or bench_sliced.index[-1] != t or bool(bench_sliced["Halted"].iloc[-1]):
            region_status[region] = "HALTED_BENCHMARK_STALE_OR_HALTED"
            continue

        bench_completed = _completed_up_to(bench_raw, t)
        if len(bench_completed) < MIN_HISTORY_BARS:
            region_status[region] = "HALTED_BENCHMARK_SHORT_HISTORY"
            continue

        bench_adj = pit_split_adjusted_ohlc(bench_completed)
        bench_close = float(bench_adj["Close"].iloc[-1])
        bench_sma120 = float(bench_adj["Close"].iloc[-120:].mean())
        if not (np.isfinite(bench_close) and np.isfinite(bench_sma120) and bench_sma120 > 0.0):
            region_status[region] = "HALTED_BENCHMARK_INVALID_METRIC"
            continue

        regime_bullish = bool(bench_close >= bench_sma120)
        bench_tr = bench_completed["TotalReturnClose"].astype(float)
        bench_sessions = bench_completed.index

        scheduled_specs = reps_by_region.get(region, [])
        scheduled_count = len(scheduled_specs)
        region_rows: list[SectorObservationRow] = []

        for sector, spec in scheduled_specs:
            if not spec.is_active_on(t):
                blocked_reps.append(
                    {
                        "region": region,
                        "sector": sector,
                        "code": spec.code,
                        "reason": "REPRESENTATIVE_INELIGIBLE_OR_DELISTED_NO_AUTO_REPLACE",
                    }
                )
                continue
            etf_df = bars_by_code.get(spec.code)
            if etf_df is None:
                blocked_reps.append(
                    {"region": region, "sector": sector, "code": spec.code, "reason": "BARS_MISSING"}
                )
                continue
            etf_sliced = etf_df.loc[:t]
            if etf_sliced.empty or etf_sliced.index[-1] != t or bool(etf_sliced["Halted"].iloc[-1]):
                blocked_reps.append(
                    {"region": region, "sector": sector, "code": spec.code, "reason": "STALE_OR_HALTED_ON_EVAL_DATE"}
                )
                continue

            etf_completed = _completed_up_to(etf_df, t)
            if len(etf_completed) < MIN_HISTORY_BARS:
                blocked_reps.append(
                    {"region": region, "sector": sector, "code": spec.code, "reason": "HISTORY_BELOW_120"}
                )
                continue

            # Align strictly on regional benchmark completed sessions (no ffill!)
            etf_tr_aligned = etf_completed["TotalReturnClose"].astype(float).reindex(bench_sessions)
            rs20_series = compute_rs20_series(etf_tr_aligned, bench_tr, window=20)
            rs20_now = float(rs20_series.iloc[-1])
            rs20_prev5 = float(rs20_series.iloc[-6]) if len(rs20_series) >= 6 else float("nan")
            rs20_imp = rs20_now - rs20_prev5

            ret60_series = etf_tr_aligned / etf_tr_aligned.shift(60) - 1.0
            ret60_now = float(ret60_series.iloc[-1])

            etf_adj = pit_split_adjusted_ohlc(etf_completed)
            c_series = etf_adj["Close"]
            close_now = float(c_series.iloc[-1])
            sma20_now = float(c_series.iloc[-20:].mean())
            sma60_now = float(c_series.iloc[-60:].mean())
            atr20_series = compute_wilder_atr20(etf_adj, period=20)
            atr20_now = float(atr20_series.iloc[-1])

            metrics = [close_now, sma20_now, sma60_now, atr20_now, rs20_now, rs20_imp, ret60_now]
            if not np.isfinite(metrics).all() or atr20_now <= 0.0:
                blocked_reps.append(
                    {"region": region, "sector": sector, "code": spec.code, "reason": "NONFINITE_OR_ZERO_ATR_WINDOW"}
                )
                continue

            transition_label = classify_rs_transition(rs20_series, lookback_sessions=10)
            is_obs = bool(rs20_now > 0.0 and rs20_imp > 0.0)
            is_qual = bool(
                is_obs
                and close_now > sma60_now
                and (close_now - sma20_now) <= (2.0 * atr20_now)
            )
            region_rows.append(
                SectorObservationRow(
                    eval_date=t.strftime("%Y-%m-%d"),
                    region=region,
                    sector=sector,
                    code=spec.code,
                    name=spec.name,
                    close=close_now,
                    sma20=sma20_now,
                    sma60=sma60_now,
                    atr20=atr20_now,
                    rs20=rs20_now,
                    rs20_improvement_5d=rs20_imp,
                    ret60_total=ret60_now,
                    is_observed=is_obs,
                    is_entry_qualified=is_qual,
                    rs_transition_label=transition_label,
                    regime_bullish=regime_bullish,
                    region_coverage_ok=False,  # Filled once regional count is known
                )
            )

        valid_rep_count = len(region_rows)
        cov_ratio = (valid_rep_count / scheduled_count) if scheduled_count > 0 else 0.0
        coverage_ok = bool(
            valid_rep_count >= MIN_REGIONAL_REPRESENTATIVES
            and cov_ratio >= MIN_REGIONAL_COVERAGE_RATIO
        )
        benchmark_metrics[region] = {
            "benchmark_code": bench_spec.code,
            "close": bench_close,
            "sma120": bench_sma120,
            "regime_bullish": regime_bullish,
            "scheduled_representatives": scheduled_count,
            "valid_representatives": valid_rep_count,
            "coverage_ratio": cov_ratio,
            "coverage_ok": coverage_ok,
        }

        if not coverage_ok:
            region_status[region] = "HALTED_INSUFFICIENT_COVERAGE"
        elif not regime_bullish:
            region_status[region] = "HALTED_BEAR_REGIME"
        else:
            region_status[region] = "ACTIVE"

        for row in region_rows:
            observations.append(
                SectorObservationRow(
                    **{**row.__dict__, "region_coverage_ok": coverage_ok}
                )
            )

    return DailySignalSnapshot(
        eval_date=t.strftime("%Y-%m-%d"),
        region_status=region_status,
        benchmark_metrics=benchmark_metrics,
        observations=tuple(observations),
        blocked_representatives=tuple(blocked_reps),
    )


def select_weekly_etf_candidates_b(
    snapshot: DailySignalSnapshot,
    free_slots: int,
    excluded_sector_keys: set[tuple[str, str]],
    max_total_picks: int = 3,
    allowed_sector_keys: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Select weekly `B` (Representative Sector ETF) entries (§3).

    - Filters `region_coverage_ok`, `regime_bullish`, `is_entry_qualified`.
    - Excludes currently held sectors and sectors exited on the same bar.
    - Optional `allowed_sector_keys` restricts selection to a common PIT-verified scope (§4).
    - Sorts by `rs20` descending, tie-breaker `code` ascending.
    """
    limit = min(max(0, int(free_slots)), max_total_picks)
    if limit == 0:
        return []
    pool = [
        row
        for row in snapshot.observations
        if row.region_coverage_ok
        and row.regime_bullish
        and row.is_entry_qualified
        and (row.region, row.sector) not in excluded_sector_keys
        and (allowed_sector_keys is None or (row.region, row.sector) in allowed_sector_keys)
    ]
    pool.sort(key=lambda r: (-r.rs20, r.code))
    return [
        {
            "code": r.code,
            "name": r.name,
            "region": r.region,
            "sector": r.sector,
            "instrument_kind": "EQUITY_ETF",
            "signal_close": r.close,
            "signal_sma20": r.sma20,
            "signal_sma60": r.sma60,
            "signal_atr20": r.atr20,
            "score": r.rs20,
            "rs20": r.rs20,
            "rs20_improvement_5d": r.rs20_improvement_5d,
            "rs_transition_label": r.rs_transition_label,
        }
        for r in pool[:limit]
    ]


def select_weekly_etf_candidates_simple60(
    snapshot: DailySignalSnapshot,
    free_slots: int,
    excluded_sector_keys: set[tuple[str, str]],
    max_total_picks: int = 3,
) -> list[dict]:
    """Select weekly `B-simple60` benchmark entries (§9: 단순 60일 수익률 상위 3개).

    Applies identical data eligibility, regional coverage, regional SMA120 regime,
    slot/holding exclusions, sizing, execution, and exits — omitting ONLY RS/improvement/SMA60/2ATR gates.
    """
    limit = min(max(0, int(free_slots)), max_total_picks)
    if limit == 0:
        return []
    pool = [
        row
        for row in snapshot.observations
        if row.region_coverage_ok
        and row.regime_bullish
        and np.isfinite(row.ret60_total)
        and (row.region, row.sector) not in excluded_sector_keys
    ]
    pool.sort(key=lambda r: (-r.ret60_total, r.code))
    return [
        {
            "code": r.code,
            "name": r.name,
            "region": r.region,
            "sector": r.sector,
            "instrument_kind": "EQUITY_ETF",
            "signal_close": r.close,
            "signal_sma20": r.sma20,
            "signal_sma60": r.sma60,
            "signal_atr20": r.atr20,
            "score": r.ret60_total,
            "ret60_total": r.ret60_total,
        }
        for r in pool[:limit]
    ]


def select_weekly_etf_candidates_random(
    snapshot: DailySignalSnapshot,
    free_slots: int,
    excluded_sector_keys: set[tuple[str, str]],
    rng: np.random.Generator,
    max_total_picks: int = 3,
) -> list[dict]:
    """Select weekly Random Policy entries from eligible regime-passing sector ETFs (§9)."""
    limit = min(max(0, int(free_slots)), max_total_picks)
    if limit == 0:
        return []
    pool = [
        row
        for row in snapshot.observations
        if row.region_coverage_ok
        and row.regime_bullish
        and (row.region, row.sector) not in excluded_sector_keys
    ]
    pool.sort(key=lambda r: r.code)  # Deterministic input order before seeded choice
    if not pool:
        return []
    k = min(limit, len(pool))
    chosen_idx = rng.choice(len(pool), size=k, replace=False)
    return [
        {
            "code": pool[int(i)].code,
            "name": pool[int(i)].name,
            "region": pool[int(i)].region,
            "sector": pool[int(i)].sector,
            "instrument_kind": "EQUITY_ETF",
            "signal_close": pool[int(i)].close,
            "signal_sma20": pool[int(i)].sma20,
            "signal_sma60": pool[int(i)].sma60,
            "signal_atr20": pool[int(i)].atr20,
            "score": 0.0,
        }
        for i in chosen_idx
    ]


def _stock_instrument_kind(market: str) -> str:
    m = market.upper()
    if m == "KOSDAQ":
        return "KOSDAQ_STOCK"
    if m == "KOSPI":
        return "KOSPI_STOCK"
    raise ValueError(f"UNSUPPORTED_STOCK_MARKET_COST_KIND:{market}")


def _evaluate_single_stock_on_date(
    code: str,
    name: str,
    market: str,
    eval_ts: pd.Timestamp,
    stock_df: pd.DataFrame,
    ref_tr_series: pd.Series,
    ref_sessions: pd.Index,
    require_improvement_5d: bool = False,
) -> dict | None:
    sliced = stock_df.loc[:eval_ts]
    if sliced.empty or sliced.index[-1] != eval_ts or bool(sliced["Halted"].iloc[-1]):
        return None
    completed = _completed_up_to(stock_df, eval_ts)
    if len(completed) < MIN_HISTORY_BARS:
        return None

    recent20 = completed.iloc[-20:]
    turnover20 = float(
        (recent20["Close"].astype(float) * recent20["Volume"].astype(float)).median()
    )
    if not np.isfinite(turnover20) or turnover20 <= 0.0:
        return None

    tr_aligned = completed["TotalReturnClose"].astype(float).reindex(ref_sessions)
    rs20_series = compute_rs20_series(tr_aligned, ref_tr_series, window=20)
    rs20_now = float(rs20_series.iloc[-1])
    if not np.isfinite(rs20_now):
        return None
    rs20_imp = 0.0
    if require_improvement_5d:
        rs20_prev5 = float(rs20_series.iloc[-6]) if len(rs20_series) >= 6 else float("nan")
        rs20_imp = rs20_now - rs20_prev5
        if not np.isfinite(rs20_imp):
            return None

    adj = pit_split_adjusted_ohlc(completed)
    c_ser = adj["Close"]
    close_now = float(c_ser.iloc[-1])
    sma20_now = float(c_ser.iloc[-20:].mean())
    sma60_now = float(c_ser.iloc[-60:].mean())
    atr20_now = float(compute_wilder_atr20(adj, period=20).iloc[-1])
    if not np.isfinite([close_now, sma20_now, sma60_now, atr20_now]).all() or atr20_now <= 0.0:
        return None

    return {
        "code": code,
        "name": name,
        "market": market,
        "instrument_kind": _stock_instrument_kind(market),
        "turnover20": turnover20,
        "rs20": rs20_now,
        "rs20_imp": rs20_imp,
        "close": close_now,
        "sma20": sma20_now,
        "sma60": sma60_now,
        "atr20": atr20_now,
    }


def _select_weekly_stock_candidates_shared(
    *,
    eval_date: pd.Timestamp | str,
    snapshot: DailySignalSnapshot,
    pit_constituents: PITConstituentTable,
    bars_by_code: Mapping[str, pd.DataFrame],
    free_slots: int,
    excluded_sector_keys: set[tuple[str, str]],
    excluded_codes: set[str],
    pit_verified_regions: Sequence[str],
    require_sector_entry_gate: bool,
    max_total_picks: int = 3,
    allowed_sector_keys: set[tuple[str, str]] | None = None,
) -> tuple[list[dict], dict[str, str]]:
    """Shared stock candidate generator for `C` and `A-matched` (§4 lines 56-59).

    Holds ALL stock-level parameters strictly identical:
    - Point-in-time constituent table (`pit_constituents.is_pit_verified` required)
    - Regional coverage & SMA120 bull-regime gate (`region_coverage_ok` & `regime_bullish`)
    - Per-sector top-5 constituents by 20d median turnover (never regional `5 * N_sectors`)
    - Stock score: `RS20` vs sector representative ETF `> 0` (no individual stock 5d improvement gate)
    - Trend/extension gate: `Close > SMA60` and `Close - SMA20 <= 2 * ATR20`
    - Tie-breaker: `(-rs20_vs_sector, -turnover20, code)`
    - Exclusions: `excluded_sector_keys`, `excluded_codes`, max 1 stock per `(region, sector)`

    Switches ONLY `require_sector_entry_gate` (`True` for `C`, `False` for `A-matched`).
    """
    limit = min(max(0, int(free_slots)), max_total_picks)
    region_notes: dict[str, str] = {}
    verified_set = set(pit_verified_regions)

    if not bool(pit_constituents.is_pit_verified):
        for reg in sorted(snapshot.region_status):
            region_notes[reg] = "NOT_IMPLEMENTED_CONSTITUENTS_NOT_PIT_VERIFIED"
        return [], region_notes

    t = pd.Timestamp(eval_date).normalize()

    # Record regions without PIT constituents while isolating PIT-covered (region, sector) pairs
    pit_covered_obs: list[SectorObservationRow] = []
    for obs in snapshot.observations:
        if not (obs.region_coverage_ok and obs.regime_bullish):
            continue
        if (obs.region, obs.sector) in excluded_sector_keys:
            continue
        if allowed_sector_keys is not None and (obs.region, obs.sector) not in allowed_sector_keys:
            continue
        if obs.region not in verified_set:
            if not require_sector_entry_gate or obs.is_entry_qualified:
                region_notes[obs.region] = "NOT_IMPLEMENTED_NO_PIT_CONSTITUENTS"
            continue
        if pit_constituents.constituents_as_of(obs.region, obs.sector, t) is None:
            if not require_sector_entry_gate or obs.is_entry_qualified:
                region_notes[obs.region] = "NOT_IMPLEMENTED_NO_PIT_CONSTITUENTS"
            continue
        pit_covered_obs.append(obs)

    if limit == 0 or not pit_covered_obs:
        return [], region_notes

    if require_sector_entry_gate:
        target_obs = [obs for obs in pit_covered_obs if obs.is_entry_qualified]
        target_obs.sort(key=lambda r: (-r.rs20, r.code))
        target_obs = target_obs[:limit]
    else:
        target_obs = sorted(pit_covered_obs, key=lambda r: (r.region, r.sector, r.code))

    qualifying_by_sector: list[tuple[SectorObservationRow, list[dict]]] = []
    for obs in target_obs:
        members = pit_constituents.constituents_as_of(obs.region, obs.sector, t)
        if not members:
            continue
        etf_df = bars_by_code.get(obs.code)
        if etf_df is None:
            continue
        etf_completed = _completed_up_to(etf_df, t)
        if etf_completed.empty:
            continue
        etf_tr = etf_completed["TotalReturnClose"].astype(float)
        etf_sessions = etf_completed.index

        evaluated_members: list[dict] = []
        for item in members:
            if item.code in excluded_codes:
                continue
            s_df = bars_by_code.get(item.code)
            if s_df is None:
                continue
            info = _evaluate_single_stock_on_date(
                code=item.code,
                name=item.name,
                market=item.market,
                eval_ts=t,
                stock_df=s_df,
                ref_tr_series=etf_tr,
                ref_sessions=etf_sessions,
                require_improvement_5d=False,
            )
            if info is not None:
                evaluated_members.append(info)

        if not evaluated_members:
            continue

        # Strict per-sector top 5 by 20d median turnover (tie-breaker: code ascending)
        evaluated_members.sort(key=lambda x: (-x["turnover20"], x["code"]))
        top5_liquid = evaluated_members[:5]

        qualifying = [
            m
            for m in top5_liquid
            if m["rs20"] > 0.0
            and m["close"] > m["sma60"]
            and (m["close"] - m["sma20"]) <= (2.0 * m["atr20"])
        ]
        if not qualifying:
            continue

        # Highest RS20 vs sector; tie-breaker: 20d median turnover desc, then code asc (§4 line 56)
        qualifying.sort(key=lambda x: (-x["rs20"], -x["turnover20"], x["code"]))
        qualifying_by_sector.append((obs, qualifying))

    chosen: list[dict] = []
    used_codes = set(excluded_codes)
    used_sectors = set(excluded_sector_keys)

    if require_sector_entry_gate:
        for obs, qualifying in qualifying_by_sector:
            if len(chosen) >= limit:
                break
            sec_key = (obs.region, obs.sector)
            if sec_key in used_sectors:
                continue
            winner = next((m for m in qualifying if m["code"] not in used_codes), None)
            if winner is None:
                continue
            used_codes.add(winner["code"])
            used_sectors.add(sec_key)
            chosen.append(
                {
                    "code": winner["code"],
                    "name": winner["name"],
                    "region": obs.region,
                    "sector": obs.sector,
                    "instrument_kind": winner["instrument_kind"],
                    "signal_close": winner["close"],
                    "signal_sma20": winner["sma20"],
                    "signal_sma60": winner["sma60"],
                    "signal_atr20": winner["atr20"],
                    "score": winner["rs20"],
                    "rs20_vs_sector": winner["rs20"],
                    "turnover20": winner["turnover20"],
                }
            )
    else:
        pooled_sector_candidates: list[tuple[SectorObservationRow, dict]] = []
        for obs, qualifying in qualifying_by_sector:
            for m in qualifying:
                pooled_sector_candidates.append((obs, m))
        pooled_sector_candidates.sort(
            key=lambda pair: (
                -pair[1]["rs20"],
                -pair[1]["turnover20"],
                pair[1]["code"],
                pair[0].region,
                pair[0].sector,
            )
        )
        for obs, winner in pooled_sector_candidates:
            if len(chosen) >= limit:
                break
            sec_key = (obs.region, obs.sector)
            if sec_key in used_sectors or winner["code"] in used_codes:
                continue
            used_codes.add(winner["code"])
            used_sectors.add(sec_key)
            chosen.append(
                {
                    "code": winner["code"],
                    "name": winner["name"],
                    "region": obs.region,
                    "sector": obs.sector,
                    "instrument_kind": winner["instrument_kind"],
                    "signal_close": winner["close"],
                    "signal_sma20": winner["sma20"],
                    "signal_sma60": winner["sma60"],
                    "signal_atr20": winner["atr20"],
                    "score": winner["rs20"],
                    "rs20_vs_sector": winner["rs20"],
                    "turnover20": winner["turnover20"],
                }
            )

    return chosen, region_notes


def select_weekly_stock_candidates_c(
    eval_date: pd.Timestamp | str,
    snapshot: DailySignalSnapshot,
    pit_constituents: PITConstituentTable,
    bars_by_code: Mapping[str, pd.DataFrame],
    free_slots: int,
    excluded_sector_keys: set[tuple[str, str]],
    excluded_codes: set[str],
    pit_verified_regions: Sequence[str],
    max_total_picks: int = 3,
    allowed_sector_keys: set[tuple[str, str]] | None = None,
) -> tuple[list[dict], dict[str, str]]:
    """Select weekly `C` (Strongest Constituent in Selected Sectors) entries (§4)."""
    return _select_weekly_stock_candidates_shared(
        eval_date=eval_date,
        snapshot=snapshot,
        pit_constituents=pit_constituents,
        bars_by_code=bars_by_code,
        free_slots=free_slots,
        excluded_sector_keys=excluded_sector_keys,
        excluded_codes=excluded_codes,
        pit_verified_regions=pit_verified_regions,
        require_sector_entry_gate=True,
        max_total_picks=max_total_picks,
        allowed_sector_keys=allowed_sector_keys,
    )


def select_weekly_stock_candidates_a_matched(
    eval_date: pd.Timestamp | str,
    snapshot: DailySignalSnapshot,
    mapping: FrozenSectorMapping,
    pit_constituents: PITConstituentTable,
    bars_by_code: Mapping[str, pd.DataFrame],
    free_slots: int,
    excluded_codes: set[str],
    pit_verified_regions: Sequence[str],
    max_total_picks: int = 3,
    excluded_sector_keys: set[tuple[str, str]] | None = None,
    allowed_sector_keys: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Select weekly `A-matched` (Sector-Gate-Removed Stock Ablation) entries (§4 line 59).

    Shares the exact candidate generator, PIT constituent universe, per-sector top-5 liquidity
    filter, sector-relative `RS20` score, tie-breaking, and duplicate-sector cap with `C`,
    removing ONLY the sector entry qualification gate (`require_sector_entry_gate=False`).
    """
    _ = mapping  # Kept in signature for caller compatibility; active sector rows come from snapshot
    chosen, _ = _select_weekly_stock_candidates_shared(
        eval_date=eval_date,
        snapshot=snapshot,
        pit_constituents=pit_constituents,
        bars_by_code=bars_by_code,
        free_slots=free_slots,
        excluded_sector_keys=set(excluded_sector_keys) if excluded_sector_keys is not None else set(),
        excluded_codes=excluded_codes,
        pit_verified_regions=pit_verified_regions,
        require_sector_entry_gate=False,
        max_total_picks=max_total_picks,
        allowed_sector_keys=allowed_sector_keys,
    )
    return chosen
