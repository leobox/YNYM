"""Public daily observation & README panel builder for T-145 v1.0 Sector RS (`RS20`) + KR Leaders (`C`).

Strictly adheres to `docs/tasks/T-145-spec-v1.md` (§§1-7, §9) and `quant-research/AGENTS.md`:
- Zero order/trading execution APIs.
- Pre-reviewed, 6-month frozen `KR` & `US` non-leveraged equity sector representative mapping
  (`v1.0-public-obs-2026H2`) with currency/FX-hedge matched regional benchmarks (`069500`, `360750`).
- Uninterrupted daily observation & `RS20` transition tracking even when regional `SMA120` regime is bearish.
- Weekly candidate tables (`B` representative sector ETFs & `C` domestic sector leading stocks, max 3 slots each)
  never pad with unqualified instruments (`0` candidates allowed) and separate confirmed week-end rebalance
  closes from mid-week observation previews.
- `C` evaluates per-sector top-5 20d median turnover common-stock constituents (no 5조원 market-cap ceiling)
  by `RS20` vs the sector's representative ETF (`> 0`), `Close > SMA60`, and `Close - SMA20 <= 2 * ATR20`,
  while marking `US` individual stocks as `NOT_IMPLEMENTED_OVERSEAS_CONSTITUENTS` (representative ETF only, §4).
- Explicit provenance banner stating public KRX price bars and current-date public ETF constituent snapshots
  lack verified total-return dividend reinvestment and historical survivorship-free PIT constituents
  (forward observation only, not profitability proof).
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
import re
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import SPEC_TASK_ID, SPEC_VERSION
from .execution_e0 import compute_target_qty
from .journal_e1 import compute_e1_entry_check_band
from .models import (
    MIN_HISTORY_BARS,
    FrozenSectorMapping,
    RegionalBenchmarkSpec,
    SectorETFSpec,
    clean_and_validate_bars,
)
from .signals import (
    DailySignalSnapshot,
    SectorObservationRow,
    compute_rs20_series,
    compute_wilder_atr20,
    evaluate_daily_snapshot,
    pit_split_adjusted_ohlc,
    select_weekly_etf_candidates_b,
)

KST = ZoneInfo("Asia/Seoul")
DEFAULT_REFERENCE_CAPITAL_KRW = 30_000_000.0
PUBLIC_MAPPING_VERSION = "v1.0-public-obs-2026H2"
PUBLIC_MAPPING_FREEZE_DATE = "2026-11-06"
PUBLIC_MAPPING_VALID_FROM = "2026-11-06"
PUBLIC_MAPPING_VALID_UNTIL = "2027-05-07"
OBSERVATION_SCHEMA_VERSION = "1.1"

SECTOR_V1_START = "<!-- SECTOR_V1:START -->"
SECTOR_V1_END = "<!-- SECTOR_V1:END -->"

# Standard KRX 6-char common stock code ending in '0' (supports alphanumeric codes like 0126Z0 while
# excluding preferred shares ending in 5/7/8/9/K/L/M, cash/futures codes, and foreign tickers).
_KRX_COMMON_CODE_RE = re.compile(r"^[0-9]{4}[0-9A-Z]0$")


def is_eligible_kr_common_constituent(
    code: str,
    name: str,
    excluded_etf_codes: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """Return True if `(code, name)` is an eligible KRX common-stock constituent (not ETF/preferred/SPAC/REIT)."""
    clean_code = str(code or "").strip()
    clean_name = str(name or "").strip()
    if not clean_code or not clean_name:
        return False
    if not _KRX_COMMON_CODE_RE.fullmatch(clean_code):
        return False
    if clean_code in excluded_etf_codes:
        return False
    if "스팩" in clean_name or "리츠" in clean_name:
        return False
    return True


def build_frozen_public_sector_mapping() -> FrozenSectorMapping:
    """Return the pre-reviewed, 6-month frozen `KR` & `US` sector ETF mapping (§2)."""
    benchmarks = {
        "KR": RegionalBenchmarkSpec(
            region="KR",
            code="069500",
            name="KODEX 200",
            currency="KRW",
            fx_hedged=False,
        ),
        "US": RegionalBenchmarkSpec(
            region="US",
            code="360750",
            name="TIGER 미국S&P500",
            currency="KRW",
            fx_hedged=False,
        ),
    }

    raw_specs = (
        # KR domestic non-leveraged equity sectors (13 distinct sectors, KRW unhedged)
        ("396500", "TIGER 반도체TOP10", "KR", "반도체", "FnGuide 반도체TOP10"),
        ("305720", "KODEX 2차전지산업", "KR", "2차전지", "FnGuide 2차전지산업"),
        ("487240", "KODEX AI전력핵심설비", "KR", "전력설비", "iSelect AI전력핵심설비"),
        ("466920", "SOL 조선TOP3플러스", "KR", "조선", "FnGuide 조선TOP3플러스"),
        ("0080G0", "KODEX 방산TOP10", "KR", "방산", "iSelect 방산TOP10"),
        ("228790", "TIGER 화장품", "KR", "화장품/소비재", "Wise 화장품"),
        ("117700", "KODEX 건설", "KR", "건설", "KRX 건설"),
        ("466940", "TIGER 은행고배당플러스TOP10", "KR", "은행", "FnGuide 은행고배당플러스TOP10"),
        ("434730", "HANARO 원자력iSelect", "KR", "원자력", "iSelect 원자력"),
        ("364970", "TIGER 바이오TOP10", "KR", "바이오/헬스케어", "FnGuide 바이오TOP10"),
        ("445290", "KODEX 로봇액티브", "KR", "로봇/자동화", "KRX/FnGuide 로봇산업"),
        ("102970", "KODEX 증권", "KR", "증권", "KRX 증권"),
        ("091180", "KODEX 자동차", "KR", "자동차", "KRX 자동차"),
        # US non-leveraged equity sectors (7 pure-US sectors, KRW unhedged, no mixed-global/covered-call)
        ("381180", "TIGER 미국필라델피아반도체나스닥", "US", "반도체", "PHLX Semiconductor"),
        ("458730", "TIGER 미국배당다우존스", "US", "배당/가치주", "Dow Jones US Dividend 100"),
        ("381170", "TIGER 미국테크TOP10 INDXX", "US", "빅테크/플랫폼", "Indxx US Tech Top 10"),
        ("0173Y0", "KODEX 미국AI광통신네트워크", "US", "광통신/네트워크", "US AI Optical Network"),
        ("487230", "KODEX 미국AI전력핵심인프라", "US", "전력/인프라", "iSelect US AI Power Infra"),
        ("0183J0", "TIGER 미국우주테크", "US", "우주항공/방산", "US Space & Defense Tech"),
        ("0041D0", "KODEX 미국AI소프트웨어TOP10", "US", "AI소프트웨어", "US Enterprise AI Software"),
    )

    representatives: dict[tuple[str, str], SectorETFSpec] = {}
    for code, name, region, sector, underlying_index in raw_specs:
        representatives[(region, sector)] = SectorETFSpec(
            code=code,
            name=name,
            region=region,
            sector=sector,
            underlying_index=underlying_index,
            currency="KRW",
            fx_hedged=False,
            asset_class="EQUITY_SECTOR",
            mapping_reviewed=True,
        )

    mapping = FrozenSectorMapping(
        mapping_version=PUBLIC_MAPPING_VERSION,
        freeze_date=pd.Timestamp(PUBLIC_MAPPING_FREEZE_DATE),
        valid_from=pd.Timestamp(PUBLIC_MAPPING_VALID_FROM),
        valid_until=pd.Timestamp(PUBLIC_MAPPING_VALID_UNTIL),
        benchmarks=benchmarks,
        representatives=representatives,
        is_pit_verified=False,
    )
    mapping.validate_lock_period(require_six_month_lock=True)
    return mapping


def is_confirmed_weekly_rebalance_close(as_of: str | pd.Timestamp, fetched_at: datetime) -> bool:
    """Return True when `as_of` is the confirmed final completed trading bar of its ISO week (§3).

    - Any Friday completed bar (`weekday == 4`) is the regular KRX week-end close.
    - If Friday was a KRX holiday and the run executes after Friday 16:00 KST (or on Sat/Sun)
      within the same ISO year-week, the latest completed session of that week (`Thu`/`Wed`)
      is also confirmed as the week-end rebalance session.
    - Mid-week Mon-Thu runs observed before Friday 16:00 KST return `False` (`MIDWEEK_OBSERVATION_ONLY`).
    """
    as_of_ts = pd.Timestamp(as_of).normalize()
    if as_of_ts.weekday() == 4:
        return True
    now_kst = fetched_at.astimezone(KST)
    after_friday_close = (now_kst.weekday() == 4 and now_kst.hour >= 16) or (now_kst.weekday() >= 5)
    if not after_friday_close:
        return False
    as_iso = as_of_ts.isocalendar()
    now_iso = pd.Timestamp(now_kst.date()).isocalendar()
    return (int(as_iso.year), int(as_iso.week)) == (int(now_iso.year), int(now_iso.week))


def _gate_failure_reasons(row: SectorObservationRow) -> list[str]:
    reasons: list[str] = []
    if not row.region_coverage_ok:
        reasons.append("지역 커버리지 미달")
    if not row.regime_bullish:
        reasons.append("지역 레짐 차단(<SMA120)")
    if row.rs20 <= 0.0:
        reasons.append("RS20≤0")
    if row.rs20_improvement_5d <= 0.0:
        reasons.append("5일 개선≤0")
    if row.close <= row.sma60:
        reasons.append("SMA60 하회")
    if (row.close - row.sma20) > (2.0 * row.atr20):
        reasons.append("2ATR 이격 초과")
    return reasons


def _completed_traded_bars_up_to(df: pd.DataFrame, eval_ts: pd.Timestamp) -> pd.DataFrame:
    sliced = df.loc[:eval_ts]
    if sliced.empty:
        return sliced
    mask = sliced["Volume"].astype(float) > 0.0
    if "Halted" in sliced.columns:
        mask = mask & (~sliced["Halted"].astype(bool))
    return sliced.loc[mask]


def _evaluate_kr_constituent_on_date(
    *,
    code: str,
    name: str,
    eval_ts: pd.Timestamp,
    stock_df: pd.DataFrame,
    etf_tr_series: pd.Series,
    etf_sessions: pd.Index,
) -> dict | None:
    """Evaluate one `KR` common-stock constituent on `eval_ts` against its sector ETF (§4).

    Matches `signals._evaluate_single_stock_on_date` 100% on `turnover20`, `rs20`, `close`,
    `sma20`, `sma60`, and Wilder `atr20` using strictly completed bars `<= eval_ts`.
    """
    sliced = stock_df.loc[:eval_ts]
    if sliced.empty or sliced.index[-1] != eval_ts:
        return None
    if bool(sliced["Halted"].iloc[-1]) or float(sliced["Volume"].iloc[-1]) <= 0.0:
        return None

    completed = _completed_traded_bars_up_to(stock_df, eval_ts)
    if len(completed) < MIN_HISTORY_BARS:
        return None

    recent20 = completed.iloc[-20:]
    turnover20 = float(
        (recent20["Close"].astype(float) * recent20["Volume"].astype(float)).median()
    )
    if not np.isfinite(turnover20) or turnover20 <= 0.0:
        return None

    tr_aligned = completed["TotalReturnClose"].astype(float).reindex(etf_sessions)
    rs20_series = compute_rs20_series(tr_aligned, etf_tr_series, window=20)
    rs20_now = float(rs20_series.iloc[-1])
    if not np.isfinite(rs20_now):
        return None

    rs20_imp_5d: float | None = None
    if len(rs20_series) >= 6:
        prev5 = float(rs20_series.iloc[-6])
        if np.isfinite(prev5):
            rs20_imp_5d = float(rs20_now - prev5)

    adj = pit_split_adjusted_ohlc(completed)
    c_ser = adj["Close"]
    close_now = float(c_ser.iloc[-1])
    sma20_now = float(c_ser.iloc[-20:].mean())
    sma60_now = float(c_ser.iloc[-60:].mean())
    atr20_now = float(compute_wilder_atr20(adj, period=20).iloc[-1])
    if not np.isfinite([close_now, sma20_now, sma60_now, atr20_now]).all() or atr20_now <= 0.0:
        return None

    failures: list[str] = []
    if rs20_now <= 0.0:
        failures.append("섹터대비 RS20≤0")
    if close_now <= sma60_now:
        failures.append("SMA60 하회")
    if (close_now - sma20_now) > (2.0 * atr20_now):
        failures.append("2ATR 이격 초과")

    return {
        "code": code,
        "name": name,
        "turnover20": turnover20,
        "rs20_vs_sector": rs20_now,
        "rs20_vs_sector_imp_5d": rs20_imp_5d,
        "close": close_now,
        "sma20": sma20_now,
        "sma60": sma60_now,
        "atr20": atr20_now,
        "is_stock_qualified": bool(
            rs20_now > 0.0
            and close_now > sma60_now
            and (close_now - sma20_now) <= (2.0 * atr20_now)
        ),
        "stock_gate_failures": failures,
    }


def _evaluate_sector_leader_pool(
    *,
    obs: SectorObservationRow,
    eval_ts: pd.Timestamp,
    cleaned_bars: Mapping[str, pd.DataFrame],
    sector_constituents: Mapping[tuple[str, str], Sequence[Mapping[str, str]]] | None,
    excluded_etf_codes: set[str],
    reference_capital: float = DEFAULT_REFERENCE_CAPITAL_KRW,
) -> tuple[dict, list[dict]]:
    """Return `(leader_observation_summary, qualifying_top5_candidates_for_c)` for one sector."""
    if obs.region != "KR":
        return (
            {
                "status": "NOT_APPLICABLE_OVERSEAS_ETF_ONLY",
                "reason": "해외 개별주 C 미실시 (§4 · 대표 ETF 전용)",
                "evaluated_constituent_count": 0,
                "top5_liquid_count": 0,
                "outperforming_top5_count": 0,
                "leader": None,
            },
            [],
        )

    if sector_constituents is None or (obs.region, obs.sector) not in sector_constituents:
        return (
            {
                "status": "NO_CONSTITUENT_SNAPSHOT",
                "reason": "국내 섹터 구성종목 스냅샷 미수신",
                "evaluated_constituent_count": 0,
                "top5_liquid_count": 0,
                "outperforming_top5_count": 0,
                "leader": None,
            },
            [],
        )

    raw_members = sector_constituents.get((obs.region, obs.sector)) or ()
    etf_df = cleaned_bars.get(obs.code)
    if etf_df is None:
        return (
            {
                "status": "SECTOR_ETF_BARS_MISSING",
                "reason": "섹터 대표 ETF 일봉 없음",
                "evaluated_constituent_count": 0,
                "top5_liquid_count": 0,
                "outperforming_top5_count": 0,
                "leader": None,
            },
            [],
        )

    # Same sector ETF reference series as `signals._select_weekly_stock_candidates_shared`
    etf_sliced = etf_df.loc[:eval_ts]
    etf_completed = (
        etf_sliced.loc[~etf_sliced["Halted"].astype(bool)]
        if "Halted" in etf_sliced.columns
        else etf_sliced
    )
    if etf_completed.empty:
        return (
            {
                "status": "SECTOR_ETF_BARS_MISSING",
                "reason": "섹터 대표 ETF 완료 일봉 없음",
                "evaluated_constituent_count": 0,
                "top5_liquid_count": 0,
                "outperforming_top5_count": 0,
                "leader": None,
            },
            [],
        )

    etf_tr = etf_completed["TotalReturnClose"].astype(float)
    etf_sessions = etf_completed.index

    seen_codes: set[str] = set()
    evaluated_members: list[dict] = []
    for item in raw_members:
        m_code = str(item.get("code", "")).strip()
        m_name = str(item.get("name", "")).strip()
        if not is_eligible_kr_common_constituent(m_code, m_name, excluded_etf_codes):
            continue
        if m_code in seen_codes:
            continue
        seen_codes.add(m_code)
        s_df = cleaned_bars.get(m_code)
        if s_df is None:
            continue
        info = _evaluate_kr_constituent_on_date(
            code=m_code,
            name=m_name,
            eval_ts=eval_ts,
            stock_df=s_df,
            etf_tr_series=etf_tr,
            etf_sessions=etf_sessions,
        )
        if info is not None:
            evaluated_members.append(info)

    if not evaluated_members:
        return (
            {
                "status": "INSUFFICIENT_CONSTITUENT_BARS",
                "reason": "유효 완료 일봉(120거래일+) 구성종목 없음",
                "evaluated_constituent_count": 0,
                "top5_liquid_count": 0,
                "outperforming_top5_count": 0,
                "leader": None,
            },
            [],
        )

    # Strict per-sector top 5 by 20d median turnover (tie-breaker: code ascending, §4 line 56)
    evaluated_members.sort(key=lambda m: (-m["turnover20"], m["code"]))
    top5_liquid = evaluated_members[:5]

    outperforming = [m for m in top5_liquid if m["rs20_vs_sector"] > 0.0]
    outperforming.sort(key=lambda m: (-m["rs20_vs_sector"], -m["turnover20"], m["code"]))

    qualifying = [m for m in outperforming if m["is_stock_qualified"]]

    if not outperforming:
        return (
            {
                "status": "NO_OUTPERFORMING_TOP5_STOCK",
                "reason": "거래대금 상위 5개 내 섹터 대표 대비 RS20>0 종목 없음",
                "evaluated_constituent_count": len(evaluated_members),
                "top5_liquid_count": len(top5_liquid),
                "outperforming_top5_count": 0,
                "leader": None,
            },
            [],
        )

    top1 = outperforming[0]
    band = compute_e1_entry_check_band(
        signal_sma20=float(top1["sma20"]),
        signal_close=float(top1["close"]),
        signal_atr20=float(top1["atr20"]),
    )
    non_rs_failures = [f for f in top1["stock_gate_failures"] if f != "섹터대비 RS20≤0"]
    leader_summary = {
        "status": "QUALIFIED_STOCK_GATE" if not non_rs_failures else "OUTPERFORMING_SECTOR_WATCH",
        "reason": "개별주 추세·비과열 통과" if not non_rs_failures else ", ".join(non_rs_failures),
        "evaluated_constituent_count": len(evaluated_members),
        "top5_liquid_count": len(top5_liquid),
        "outperforming_top5_count": len(outperforming),
        "leader": {
            "code": top1["code"],
            "name": top1["name"],
            "close": round(float(top1["close"]), 2),
            "sma20": round(float(top1["sma20"]), 2),
            "sma60": round(float(top1["sma60"]), 2),
            "atr20": round(float(top1["atr20"]), 2),
            "rs20_vs_sector": round(float(top1["rs20_vs_sector"]), 6),
            "rs20_vs_sector_improvement_5d": (
                round(float(top1["rs20_vs_sector_imp_5d"]), 6)
                if top1["rs20_vs_sector_imp_5d"] is not None
                else None
            ),
            "turnover20_median_krw": round(float(top1["turnover20"]), 2),
            "is_stock_qualified": bool(top1["is_stock_qualified"]),
            "stock_gate_failures": non_rs_failures,
            "e1_check_lower_exclusive_sma20": band["lower_exclusive_sma20"],
            "e1_check_upper_inclusive": band["upper_inclusive_close_plus_half_atr"],
            "reference_stop_close_minus_2atr": round(
                float(top1["close"]) - 2.0 * float(top1["atr20"]), 2
            ),
            "reference_qty_30m_krw": int(
                compute_target_qty(
                    nav_t=reference_capital,
                    signal_close=float(top1["close"]),
                    signal_atr20=float(top1["atr20"]),
                )
            ),
        },
    }
    return leader_summary, qualifying


def evaluate_public_sector_snapshot(
    raw_frames: Mapping[str, pd.DataFrame],
    as_of: str,
    fetched_at: datetime,
    mapping: FrozenSectorMapping | None = None,
    reference_capital: float = DEFAULT_REFERENCE_CAPITAL_KRW,
    sector_constituents: Mapping[tuple[str, str], Sequence[Mapping[str, str]]] | None = None,
    extra_excluded_etf_codes: set[str] | None = None,
) -> dict:
    """Evaluate daily `RS20` observations, regional `SMA120` regime, weekly `B` ETFs, and `KR` `C` leaders."""
    eff_mapping = mapping if mapping is not None else build_frozen_public_sector_mapping()
    core_codes = {b.code for b in eff_mapping.benchmarks.values()} | {
        s.code for s in eff_mapping.representatives.values()
    }
    cleaned_bars: dict[str, pd.DataFrame] = {}
    for code, df in sorted(raw_frames.items()):
        if df is None or df.empty:
            continue
        if code in core_codes:
            cleaned_bars[code] = clean_and_validate_bars(df, code=code)
            continue
        try:
            # A malformed constituent series is skipped (never repaired/fabricated) without
            # blocking the sector ETF panel.
            cleaned_bars[code] = clean_and_validate_bars(df, code=code)
        except ValueError:
            continue

    excluded_etfs = (
        {b.code for b in eff_mapping.benchmarks.values()}
        | {s.code for s in eff_mapping.representatives.values()}
        | (set(extra_excluded_etf_codes) if extra_excluded_etf_codes is not None else set())
    )

    eval_ts = pd.Timestamp(as_of).normalize()
    snapshot: DailySignalSnapshot = evaluate_daily_snapshot(eval_ts, eff_mapping, cleaned_bars)
    weekly_confirmed = is_confirmed_weekly_rebalance_close(eval_ts, fetched_at)

    # Weekly `B` candidates (Representative Sector ETFs, max 3 slots)
    raw_candidates_b = select_weekly_etf_candidates_b(
        snapshot=snapshot,
        free_slots=3,
        excluded_sector_keys=set(),
        max_total_picks=3,
    )

    enriched_candidates_b: list[dict] = []
    for rank, cand in enumerate(raw_candidates_b, start=1):
        band = compute_e1_entry_check_band(
            signal_sma20=float(cand["signal_sma20"]),
            signal_close=float(cand["signal_close"]),
            signal_atr20=float(cand["signal_atr20"]),
        )
        ref_qty = compute_target_qty(
            nav_t=reference_capital,
            signal_close=float(cand["signal_close"]),
            signal_atr20=float(cand["signal_atr20"]),
        )
        stop_ref = float(cand["signal_close"]) - 2.0 * float(cand["signal_atr20"])
        enriched_candidates_b.append(
            {
                "rank": rank,
                **cand,
                "e1_check_lower_exclusive_sma20": band["lower_exclusive_sma20"],
                "e1_check_upper_inclusive": band["upper_inclusive_close_plus_half_atr"],
                "reference_stop_close_minus_2atr": round(stop_ref, 2),
                "reference_qty_30m_krw": int(ref_qty),
                "weekly_action_status": (
                    "WEEKEND_CANDIDATE_CONFIRMED"
                    if weekly_confirmed
                    else "MIDWEEK_PREVIEW_WAIT_FOR_WEEKEND_CLOSE"
                ),
            }
        )

    # Evaluate per-sector leader pools for ALL observed sectors (including Bear Regime sectors, §7)
    leader_by_sector_key: dict[tuple[str, str], dict] = {}
    qualifying_c_by_sector_key: dict[tuple[str, str], list[dict]] = {}
    for obs in snapshot.observations:
        l_summary, qual_list = _evaluate_sector_leader_pool(
            obs=obs,
            eval_ts=eval_ts,
            cleaned_bars=cleaned_bars,
            sector_constituents=sector_constituents,
            excluded_etf_codes=excluded_etfs,
            reference_capital=reference_capital,
        )
        leader_by_sector_key[(obs.region, obs.sector)] = l_summary
        qualifying_c_by_sector_key[(obs.region, obs.sector)] = qual_list

    # Weekly `C` candidates (Domestic Leading Stocks in Selected Qualified `KR` Sectors, max 3 slots, §4)
    # Matches `signals.select_weekly_stock_candidates_c` 100%:
    # 1) Filter `KR` observations with `region_coverage_ok`, `regime_bullish`, constituent snapshot present,
    #    and `is_entry_qualified`.
    # 2) Sort by `(-obs.rs20, obs.code)` and slice `[:3]`.
    # 3) Pick #1 qualifying top-5 turnover constituent (`rs20_vs_sector > 0`, `>SMA60`, `<=SMA20+2ATR20`),
    #    excluding duplicate stock codes across slots.
    c_region_notes: dict[str, str] = {"US": "NOT_IMPLEMENTED_OVERSEAS_CONSTITUENTS"}
    if sector_constituents is None:
        c_region_notes["KR"] = "NOT_PROVIDED_CONSTITUENT_SNAPSHOT"
    else:
        c_region_notes["KR"] = "CONTEMPORANEOUS_PUBLIC_ETF_CONSTITUENTS_NOT_HISTORICAL_PIT"

    eligible_kr_sector_obs = [
        obs
        for obs in snapshot.observations
        if obs.region == "KR"
        and obs.region_coverage_ok
        and obs.regime_bullish
        and obs.is_entry_qualified
        and sector_constituents is not None
        and (obs.region, obs.sector) in sector_constituents
    ]
    eligible_kr_sector_obs.sort(key=lambda r: (-r.rs20, r.code))
    target_kr_sector_obs = eligible_kr_sector_obs[:3]

    enriched_candidates_c: list[dict] = []
    used_stock_codes: set[str] = set()
    for obs in target_kr_sector_obs:
        if len(enriched_candidates_c) >= 3:
            break
        qual_members = qualifying_c_by_sector_key.get((obs.region, obs.sector), [])
        winner = next((m for m in qual_members if m["code"] not in used_stock_codes), None)
        if winner is None:
            continue
        used_stock_codes.add(winner["code"])
        band = compute_e1_entry_check_band(
            signal_sma20=float(winner["sma20"]),
            signal_close=float(winner["close"]),
            signal_atr20=float(winner["atr20"]),
        )
        ref_qty = compute_target_qty(
            nav_t=reference_capital,
            signal_close=float(winner["close"]),
            signal_atr20=float(winner["atr20"]),
        )
        stop_ref = float(winner["close"]) - 2.0 * float(winner["atr20"])
        enriched_candidates_c.append(
            {
                "rank": len(enriched_candidates_c) + 1,
                "code": winner["code"],
                "name": winner["name"],
                "region": obs.region,
                "sector": obs.sector,
                "sector_etf_code": obs.code,
                "sector_etf_name": obs.name,
                "sector_rs20": round(float(obs.rs20), 6),
                "sector_rs_transition_label": obs.rs_transition_label,
                "signal_close": round(float(winner["close"]), 2),
                "signal_sma20": round(float(winner["sma20"]), 2),
                "signal_sma60": round(float(winner["sma60"]), 2),
                "signal_atr20": round(float(winner["atr20"]), 2),
                "score": round(float(winner["rs20_vs_sector"]), 6),
                "rs20_vs_sector": round(float(winner["rs20_vs_sector"]), 6),
                "rs20_vs_sector_improvement_5d": (
                    round(float(winner["rs20_vs_sector_imp_5d"]), 6)
                    if winner["rs20_vs_sector_imp_5d"] is not None
                    else None
                ),
                "turnover20": round(float(winner["turnover20"]), 2),
                "e1_check_lower_exclusive_sma20": band["lower_exclusive_sma20"],
                "e1_check_upper_inclusive": band["upper_inclusive_close_plus_half_atr"],
                "reference_stop_close_minus_2atr": round(stop_ref, 2),
                "reference_qty_30m_krw": int(ref_qty),
                "weekly_action_status": (
                    "WEEKEND_CANDIDATE_CONFIRMED"
                    if weekly_confirmed
                    else "MIDWEEK_PREVIEW_WAIT_FOR_WEEKEND_CLOSE"
                ),
            }
        )

    obs_sorted = sorted(
        snapshot.observations,
        key=lambda r: (
            -int(r.is_entry_qualified and r.regime_bullish and r.region_coverage_ok),
            -int(r.is_observed),
            -r.rs20,
            r.code,
        ),
    )
    observations_serialized = [
        {
            "eval_date": r.eval_date,
            "region": r.region,
            "sector": r.sector,
            "code": r.code,
            "name": r.name,
            "close": round(r.close, 2),
            "sma20": round(r.sma20, 2),
            "sma60": round(r.sma60, 2),
            "atr20": round(r.atr20, 2),
            "rs20": round(r.rs20, 6),
            "rs20_improvement_5d": round(r.rs20_improvement_5d, 6),
            "ret60_total": round(r.ret60_total, 6),
            "is_observed": bool(r.is_observed),
            "is_entry_qualified": bool(r.is_entry_qualified),
            "rs_transition_label": r.rs_transition_label,
            "regime_bullish": bool(r.regime_bullish),
            "region_coverage_ok": bool(r.region_coverage_ok),
            "gate_failure_reasons": _gate_failure_reasons(r),
            "leader_observation": leader_by_sector_key[(r.region, r.sector)],
        }
        for r in obs_sorted
    ]

    benchmarks_out: dict[str, dict] = {}
    for region, bspec in sorted(eff_mapping.benchmarks.items()):
        bmeta = dict(snapshot.benchmark_metrics.get(region, {}))
        bmeta["benchmark_code"] = bspec.code
        bmeta["benchmark_name"] = bspec.name
        bmeta["region_status"] = snapshot.region_status.get(region, "UNKNOWN")
        benchmarks_out[region] = bmeta

    kr_obs = [r for r in observations_serialized if r["region"] == "KR"]
    kr_with_leader = sum(
        1 for r in kr_obs if r["leader_observation"].get("leader") is not None
    )
    kr_with_stock_qualified_leader = sum(
        1
        for r in kr_obs
        if r["leader_observation"].get("status") == "QUALIFIED_STOCK_GATE"
    )

    return {
        "spec_task_id": SPEC_TASK_ID,
        "spec_version": SPEC_VERSION,
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "mapping_version": eff_mapping.mapping_version,
        "mapping_valid_from": eff_mapping.valid_from.strftime("%Y-%m-%d"),
        "mapping_valid_until": eff_mapping.valid_until.strftime("%Y-%m-%d"),
        "eval_date": snapshot.eval_date,
        "weekly_rebalance_confirmed": bool(weekly_confirmed),
        "weekly_selection_mode": (
            "WEEKEND_REBALANCE_CONFIRMED" if weekly_confirmed else "MIDWEEK_OBSERVATION_ONLY"
        ),
        "reference_capital_krw": float(reference_capital),
        "region_status": dict(snapshot.region_status),
        "benchmarks": benchmarks_out,
        "weekly_candidates_b": enriched_candidates_b,
        "weekly_candidates_c": enriched_candidates_c,
        "c_region_notes": c_region_notes,
        "observations": observations_serialized,
        "blocked_representatives": list(snapshot.blocked_representatives),
        "counts": {
            "scheduled_sectors": len(eff_mapping.representatives),
            "evaluated_sectors": len(observations_serialized),
            "observed_improving_sectors": sum(1 for r in observations_serialized if r["is_observed"]),
            "entry_qualified_before_regime": sum(
                1 for r in observations_serialized if r["is_entry_qualified"]
            ),
            "weekly_actionable_candidates": len(enriched_candidates_b) if weekly_confirmed else 0,
            "weekly_preview_candidates": len(enriched_candidates_b),
            "kr_sectors_evaluated": len(kr_obs),
            "kr_sectors_with_outperforming_leader": kr_with_leader,
            "kr_sectors_with_stock_gate_leader": kr_with_stock_qualified_leader,
            "weekly_actionable_candidates_c": len(enriched_candidates_c) if weekly_confirmed else 0,
            "weekly_preview_candidates_c": len(enriched_candidates_c),
        },
    }


def _safe_md(text: object) -> str:
    return (
        escape(str(text))
        .replace("|", "&#124;")
        .replace("\n", " ")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )


def render_sector_v1_panel(record: Mapping[str, object]) -> str:
    """Render Markdown panel for `<!-- SECTOR_V1:START --> ... <!-- SECTOR_V1:END -->`."""
    status = str(record.get("status", "ERROR"))
    status_labels = {
        "OK": "정상 확인",
        "PARTIAL": "일부 조회 실패 · 수신된 항목만 평가",
        "STALE": "기준 일봉 지연 · 신규 후보 보류",
        "INSUFFICIENT_COVERAGE": "동일 날짜 수신율 미달 · 신규 후보 보류",
        "ERROR": "조회 오류 · 신규 후보 보류",
    }
    as_of = record.get("as_of") or "미확인"
    fetched_at = record.get("fetched_at") or "미확인"
    total_req = int(record.get("requested_symbol_count", 0) or 0)
    fresh_cnt = int(record.get("fresh_symbol_count", 0) or 0)
    const_req = int(record.get("requested_constituent_stock_count", 0) or 0)
    const_ok = int(record.get("fetched_constituent_stock_count", 0) or 0)
    result = record.get("result") if isinstance(record.get("result"), Mapping) else {}

    weekly_confirmed = bool(result.get("weekly_rebalance_confirmed", False))
    cadence_label = (
        "🟢 주간 정기 선발 마감일 확정 (다음 거래일 1회 수동 확인 대상)"
        if weekly_confirmed
        else "🕒 주중 관찰일 (정기 신규 편입은 주간 마지막 거래일 마감 1회만 확정)"
    )

    const_meta_str = (
        f" · 국내 구성종목 일봉 `{const_ok}/{const_req}`개 수신" if const_req > 0 else ""
    )

    lines = [
        f"### 🧭 섹터 상대강도(RS20) · 국내 대장주 관찰 및 주간 후보 ({SPEC_TASK_ID} v{SPEC_VERSION})",
        "",
        f"> 갱신: `{fetched_at}` · 기준 완료 일봉: `{as_of}` · 실행 상태: {status_labels.get(status, status)} (ETF `{total_req}`개 중 `{fresh_cnt}`개 최신 확인{const_meta_str}) · 매핑: `{PUBLIC_MAPPING_VERSION}` (`{PUBLIC_MAPPING_VALID_FROM}~{PUBLIC_MAPPING_VALID_UNTIL}` 6개월 고정)",
        "",
        f"> **편입 주기:** {cadence_label} · **데이터 한계 고지:** 공개 KRX 원가격 일봉 및 조회일 공개 ETF 상위 구성종목 스냅샷 기반 전진 관찰용 패널입니다(분배금 재투자 총수익·과거 상폐 PIT 미검증, 해외 개별주 미실시, 전략 수익성 미확정, 자동 주문 없음).",
        "",
    ]

    errors = record.get("errors")
    if isinstance(errors, list) and errors:
        lines.append(
            f"> 조회 실패 `{len(errors)}`건 · 실패 시세를 0이나 과거 가격으로 대체하지 않았습니다."
        )
        lines.append("")

    if status not in ("OK", "PARTIAL") or not result:
        lines += [
            "| 구분 | 상태 | 비고 |",
            "|:---|:---|:---|",
            "| 섹터 상대강도 패널 | 자료 확인 후 산출 | 완료 일봉 수신율 또는 최신성 기준 미충족으로 후보 산출을 보류합니다. |",
            "",
        ]
        return "\n".join(lines)

    # 1. Regional Regime Table (SMA120)
    benchmarks = result.get("benchmarks", {})
    lines += [
        "#### 1) 지역별 시장 레짐 (`종가 ≥ SMA120` 신규 진입 허용 · 약세장에도 관찰 지속)",
        "",
        "| 지역 | 광범위 주식 벤치마크 | 완료일 종가 | SMA120 (이격) | 섹터 대표 수신 | 지역 신규 편입 판정 |",
        "|:---|:---|---:|---:|---:|:---|",
    ]
    for region in ("KR", "US"):
        bm = benchmarks.get(region, {}) if isinstance(benchmarks, Mapping) else {}
        bcode = _safe_md(bm.get("benchmark_code", "-"))
        bname = _safe_md(bm.get("benchmark_name", region))
        r_status = str(bm.get("region_status", "UNKNOWN"))
        b_close = bm.get("close")
        b_sma120 = bm.get("sma120")
        sched_n = int(bm.get("scheduled_representatives", 0) or 0)
        valid_n = int(bm.get("valid_representatives", 0) or 0)
        if isinstance(b_close, (int, float)) and isinstance(b_sma120, (int, float)) and b_sma120 > 0:
            gap_pct = (float(b_close) / float(b_sma120) - 1.0) * 100.0
            close_str = f"{float(b_close):,.0f}원"
            sma_str = f"{float(b_sma120):,.0f}원 ({gap_pct:+.2f}%)"
        else:
            close_str = "-"
            sma_str = "-"

        if r_status == "ACTIVE":
            verdict_str = "🟢 신규 편입 허용 (`종가 ≥ SMA120`)"
        elif r_status == "HALTED_BEAR_REGIME":
            verdict_str = "🔴 현금 유지 · 신규 차단 (`종가 < SMA120`, 관찰 지속)"
        else:
            verdict_str = f"⚪ 신규 선발 보류 (`{r_status}`)"

        lines.append(
            f"| `{region}` | [{bname}](https://finance.naver.com/item/main.naver?code={bcode}) (`{bcode}`) | "
            f"{close_str} | {sma_str} | `{valid_n}/{sched_n}` | {verdict_str} |"
        )
    lines.append("")

    # 2. Weekly Sector ETF Candidates (`B`) & E1 Manual Check Price Band
    candidates_b = list(result.get("weekly_candidates_b", []))
    candidates_c = list(result.get("weekly_candidates_c", []))
    counts = result.get("counts", {}) if isinstance(result.get("counts"), Mapping) else {}
    obs_cnt = int(counts.get("observed_improving_sectors", 0) or 0)
    qual_cnt = int(counts.get("entry_qualified_before_regime", 0) or 0)
    eval_cnt = int(counts.get("evaluated_sectors", 0) or 0)

    cand_title_suffix = (
        "주간 확정 후보" if weekly_confirmed else "주중 예비 후보 (주말 마감 전 신규 매수 보류)"
    )
    lines += [
        f"#### 2) 대표 섹터 ETF 주간 후보 (`B`, 최대 3슬롯) — {cand_title_suffix}",
        "",
        f"> **섹터 게이트 요약 (`{eval_cnt}`개 섹터 평가):** 관찰 강세(`RS20>0` & `5일 개선>0`) `{obs_cnt}`개 → 추세·비과열 자격(`>SMA60` & `≤SMA20+2ATR20`) `{qual_cnt}`개 → 지역 레짐(`≥SMA120`) 통과 상위 최대 3개 **`{len(candidates_b)}`건**",
        "",
        "| 순위 | 지역·섹터 | 대표 ETF | 완료일 종가 | RS20 (5일 변화 · 상태) | E1 다음 거래일 1회 확인 허용가 | 이탈 기준선 / 3,000만원 참고수량 |",
        "|---:|:---|:---|---:|:---|:---|:---|",
    ]

    if candidates_b:
        for cand in candidates_b:
            rk = int(cand["rank"])
            reg = _safe_md(cand["region"])
            sec = _safe_md(cand["sector"])
            code = _safe_md(cand["code"])
            name = _safe_md(cand["name"])
            c_px = float(cand["signal_close"])
            rs20_pct = float(cand["rs20"]) * 100.0
            imp_pct = float(cand["rs20_improvement_5d"]) * 100.0
            label = _safe_md(cand["rs_transition_label"])
            low_ex = float(cand["e1_check_lower_exclusive_sma20"])
            high_in = float(cand["e1_check_upper_inclusive"])
            stop_px = float(cand["reference_stop_close_minus_2atr"])
            sma20_px = float(cand["signal_sma20"])
            ref_qty = int(cand["reference_qty_30m_krw"])
            mode_tag = "" if weekly_confirmed else " *(주중 예비)*"
            qty_str = f"`{ref_qty:,}주` (0.5% 위험예산)" if ref_qty > 0 else "`0주 (미진입)`"
            lines.append(
                f"| {rk} | `{reg}` · **{sec}**{mode_tag} | "
                f"[{name}](https://finance.naver.com/item/main.naver?code={code}) (`{code}`) | "
                f"{c_px:,.0f}원 | **{rs20_pct:+.2f}%** ({imp_pct:+.2f}%p · `{label}`) | "
                f"`{low_ex:,.0f}원 초과 ~ {high_in:,.0f}원 이하` | "
                f"2일<SMA20(`{sma20_px:,.0f}원`) · 진입가−2ATR(`{stop_px:,.0f}원` 참고) · {qty_str} |"
            )
    else:
        lines.append("| - | 조건 충족 섹터 없음 (억지 대체 편입 금지 · 현금 유지) | - | - | - | - | - |")
    lines.append("")

    # 2-1. Leading stocks of the sectors in section 2 (`C`: top-5 turnover, highest RS20 vs sector ETF)
    observations = list(result.get("observations", []))
    obs_by_key = {
        (str(r.get("region")), str(r.get("sector"))): r for r in observations if isinstance(r, Mapping)
    }
    c_by_key = {(str(c.get("region")), str(c.get("sector"))): c for c in candidates_c}

    leader_rows: list[tuple[Mapping[str, object], bool]] = []  # (observation row, is_preview)
    if candidates_b:
        seen_keys: set[tuple[str, str]] = set()
        for cand in candidates_b:
            key = (str(cand["region"]), str(cand["sector"]))
            if key in obs_by_key and key not in seen_keys:
                seen_keys.add(key)
                leader_rows.append((obs_by_key[key], False))
        # `C` ranks KR sectors only, so a KR sector outside `B` slots can still hold a `C` pick (§4)
        for cand in candidates_c:
            key = (str(cand["region"]), str(cand["sector"]))
            if key in obs_by_key and key not in seen_keys:
                seen_keys.add(key)
                leader_rows.append((obs_by_key[key], False))
        lines += [
            "#### 2-1) 위 섹터의 대장주 (`C` · 섹터 ETF 구성종목 중 20일 거래대금 상위 5개 → 섹터 대비 RS20 1위)",
            "",
        ]
    else:
        preview_pool = [
            r
            for r in observations
            if str(r.get("region")) == "KR" and bool(r.get("is_observed"))
        ]
        preview_pool.sort(key=lambda r: (-float(r.get("rs20", 0.0)), str(r.get("code", ""))))
        leader_rows = [(r, True) for r in preview_pool[:3]]
        lines += [
            "#### 2-1) 강세 관찰 상위 국내 섹터의 대장주 (미리보기 · 신규 매수 대상 아님)",
            "",
            "> 2번 후보가 `0`건이므로, 관찰 강세(`RS20>0` & `5일 개선>0`) 국내 섹터 중 RS20 상위 최대 3개의 대장주를 미리 보여줍니다. 지역 레짐·섹터 자격·개별주 자격이 모두 충족되기 전에는 확인 허용가와 수량을 산출하지 않습니다.",
            "",
        ]

    lines += [
        "| 섹터 (섹터 RS20) | 대장주 | 완료일 종가 | 섹터 대비 RS20 | 판정 | E1 다음 거래일 1회 확인 허용가 | 이탈 기준선 / 3,000만원 참고수량 |",
        "|:---|:---|---:|---:|:---|:---|:---|",
    ]
    if not leader_rows:
        lines.append("| - | 표시할 섹터 없음 (관찰 강세 국내 섹터 없음) | - | - | - | - | - |")
    for row, is_preview in leader_rows:
        reg = str(row.get("region", ""))
        sec = _safe_md(row.get("sector", ""))
        sec_rs_pct = float(row.get("rs20", 0.0)) * 100.0
        sec_cell = f"`{_safe_md(reg)}` · **{sec}** ({sec_rs_pct:+.2f}%)"
        if reg != "KR":
            lines.append(
                f"| {sec_cell} | 해외 개별주 미실시 (사양 §4 · 대표 ETF로 대응) | - | - | - | - | - |"
            )
            continue
        l_obs = row.get("leader_observation") if isinstance(row.get("leader_observation"), Mapping) else {}
        leader = l_obs.get("leader") if isinstance(l_obs, Mapping) else None
        if not isinstance(leader, Mapping) or not leader.get("code"):
            st = str(l_obs.get("status", "")) if isinstance(l_obs, Mapping) else ""
            reason = (
                "거래대금 상위 5개 중 섹터 대비 우위 종목 없음"
                if st == "NO_OUTPERFORMING_TOP5_STOCK"
                else "구성종목 시세 확인 보류"
            )
            lines.append(f"| {sec_cell} | {reason} | - | - | - | - | - |")
            continue

        l_code = _safe_md(leader["code"])
        l_name = _safe_md(leader["name"])
        l_close = float(leader["close"])
        l_rs_pct = float(leader["rs20_vs_sector"]) * 100.0
        stock_cell = f"[{l_name}](https://finance.naver.com/item/main.naver?code={l_code}) (`{l_code}`)"
        c_pick = c_by_key.get((reg, str(row.get("sector", ""))))

        if c_pick is not None and not is_preview:
            # Actionable `C` pick (KR regime + sector qualification + stock trend/non-overheat gates)
            stock_cell = (
                f"[{_safe_md(c_pick['name'])}](https://finance.naver.com/item/main.naver?code={_safe_md(c_pick['code'])}) "
                f"(`{_safe_md(c_pick['code'])}`)"
            )
            low_ex = float(c_pick["e1_check_lower_exclusive_sma20"])
            high_in = float(c_pick["e1_check_upper_inclusive"])
            stop_px = float(c_pick["reference_stop_close_minus_2atr"])
            sma20_px = float(c_pick["signal_sma20"])
            ref_qty = int(c_pick["reference_qty_30m_krw"])
            qty_str = f"`{ref_qty:,}주` (0.5% 위험예산)" if ref_qty > 0 else "`0주 (미진입)`"
            verdict = "✅ 대장주 후보" + ("" if weekly_confirmed else " *(주중 예비)*")
            lines.append(
                f"| {sec_cell} | {stock_cell} | {float(c_pick['signal_close']):,.0f}원 | "
                f"**{float(c_pick['rs20_vs_sector']) * 100.0:+.2f}%** | {verdict} | "
                f"`{low_ex:,.0f}원 초과 ~ {high_in:,.0f}원 이하` | "
                f"2일<SMA20(`{sma20_px:,.0f}원`) · 진입가−2ATR(`{stop_px:,.0f}원` 참고) · {qty_str} |"
            )
            continue

        fails = [_safe_md(x) for x in (leader.get("stock_gate_failures") or [])]
        if is_preview:
            region_blocked = "지역 레짐 차단(<SMA120)" in list(row.get("gate_failure_reasons") or [])
            block_note = "레짐 차단" if region_blocked else "섹터 자격 미달"
            if bool(leader.get("is_stock_qualified", False)):
                verdict = f"👀 개별 추세 통과 · `{block_note}`"
            else:
                verdict = f"👀 관찰 · `{', '.join(fails) or block_note}`"
        elif bool(leader.get("is_stock_qualified", False)):
            verdict = "👀 관찰 (`중복 종목 제외`)"
        else:
            verdict = f"👀 관찰 · `{', '.join(fails) or '개별주 자격 미달'}`"
        lines.append(
            f"| {sec_cell} | {stock_cell} | {l_close:,.0f}원 | **{l_rs_pct:+.2f}%** | {verdict} | - | - |"
        )

    blocked = list(result.get("blocked_representatives", []))
    if blocked:
        lines.append("")
        blocked_desc = ", ".join(
            f"`{b.get('region')}:{b.get('sector')}({b.get('code')})={b.get('reason')}`"
            for b in blocked
        )
        lines.append(f"> 비적격·결측 대표 자동 대체 금지 보존: {blocked_desc}")

    lines.append("")
    return "\n".join(lines)


def update_sector_v1_readme(path: Path, panel: str) -> None:
    """Atomically update ONLY `<!-- SECTOR_V1:START --> ... <!-- SECTOR_V1:END -->` in `README.md`."""
    readme_path = Path(path)
    text = readme_path.read_text(encoding="utf-8")
    block = f"{SECTOR_V1_START}\n{panel.rstrip()}\n{SECTOR_V1_END}"

    if text.count(SECTOR_V1_START) == 1 and text.count(SECTOR_V1_END) == 1:
        a = text.index(SECTOR_V1_START)
        b = text.index(SECTOR_V1_END, a) + len(SECTOR_V1_END)
        new_text = text[:a] + block + text[b:]
    elif SECTOR_V1_START in text or SECTOR_V1_END in text:
        raise ValueError("MALFORMED_SECTOR_V1_MARKERS")
    elif "<!-- ETF_OBSERVATION:END -->" in text:
        anchor = "<!-- ETF_OBSERVATION:END -->"
        pos = text.index(anchor) + len(anchor)
        new_text = text[:pos] + "\n\n" + block + "\n" + text[pos:].lstrip("\r\n")
    elif "<!-- DUAL_FORWARD:END -->" in text:
        anchor = "<!-- DUAL_FORWARD:END -->"
        pos = text.index(anchor) + len(anchor)
        new_text = text[:pos] + "\n\n" + block + "\n" + text[pos:].lstrip("\r\n")
    else:
        new_text = text.rstrip() + "\n\n" + block + "\n"

    if readme_path.read_text(encoding="utf-8") != text:
        raise RuntimeError("README_CHANGED_DURING_SECTOR_V1_UPDATE")

    temp = readme_path.with_name(readme_path.name + ".sector_v1.tmp")
    temp.write_text(new_text, encoding="utf-8")
    temp.replace(readme_path)
