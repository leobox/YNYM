"""Public daily observation & README panel builder for T-145 v1.0 Sector RS (`RS20`).

Strictly adheres to `docs/tasks/T-145-spec-v1.md` (§§1-7, §9) and `quant-research/AGENTS.md`:
- Zero order/trading execution APIs.
- Pre-reviewed, 6-month frozen `KR` & `US` non-leveraged equity sector representative mapping
  (`v1.0-public-obs-2026H2`) with currency/FX-hedge matched regional benchmarks (`069500`, `360750`).
- Uninterrupted daily observation & `RS20` transition tracking even when regional `SMA120` regime is bearish.
- Weekly candidate table (`B`, max 3 slots) never pads with unqualified ETFs (`0` candidates allowed)
  and separates confirmed week-end rebalance closes from mid-week observation previews.
- Explicit provenance banner stating public KRX price bars lack verified total-return dividend
  reinvestment and historical survivorship-free PIT constituents (forward observation only, not profitability proof).
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from . import SPEC_TASK_ID, SPEC_VERSION
from .execution_e0 import compute_target_qty
from .journal_e1 import compute_e1_entry_check_band
from .models import (
    FrozenSectorMapping,
    RegionalBenchmarkSpec,
    SectorETFSpec,
    clean_and_validate_bars,
)
from .signals import (
    DailySignalSnapshot,
    SectorObservationRow,
    evaluate_daily_snapshot,
    select_weekly_etf_candidates_b,
)

KST = ZoneInfo("Asia/Seoul")
DEFAULT_REFERENCE_CAPITAL_KRW = 30_000_000.0
PUBLIC_MAPPING_VERSION = "v1.0-public-obs-2026H2"
PUBLIC_MAPPING_FREEZE_DATE = "2026-11-06"
PUBLIC_MAPPING_VALID_FROM = "2026-11-06"
PUBLIC_MAPPING_VALID_UNTIL = "2027-05-07"

SECTOR_V1_START = "<!-- SECTOR_V1:START -->"
SECTOR_V1_END = "<!-- SECTOR_V1:END -->"


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


def evaluate_public_sector_snapshot(
    raw_frames: Mapping[str, pd.DataFrame],
    as_of: str,
    fetched_at: datetime,
    mapping: FrozenSectorMapping | None = None,
    reference_capital: float = DEFAULT_REFERENCE_CAPITAL_KRW,
) -> dict:
    """Evaluate daily `RS20` observations, regional `SMA120` regime, and weekly `B` candidates."""
    eff_mapping = mapping if mapping is not None else build_frozen_public_sector_mapping()
    cleaned_bars = {
        code: clean_and_validate_bars(df, code=code)
        for code, df in sorted(raw_frames.items())
        if df is not None and not df.empty
    }

    eval_ts = pd.Timestamp(as_of).normalize()
    snapshot: DailySignalSnapshot = evaluate_daily_snapshot(eval_ts, eff_mapping, cleaned_bars)
    weekly_confirmed = is_confirmed_weekly_rebalance_close(eval_ts, fetched_at)
    raw_candidates = select_weekly_etf_candidates_b(
        snapshot=snapshot,
        free_slots=3,
        excluded_sector_keys=set(),
        max_total_picks=3,
    )

    enriched_candidates: list[dict] = []
    for rank, cand in enumerate(raw_candidates, start=1):
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
        enriched_candidates.append(
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

    return {
        "spec_task_id": SPEC_TASK_ID,
        "spec_version": SPEC_VERSION,
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
        "weekly_candidates_b": enriched_candidates,
        "observations": observations_serialized,
        "blocked_representatives": list(snapshot.blocked_representatives),
        "counts": {
            "scheduled_sectors": len(eff_mapping.representatives),
            "evaluated_sectors": len(observations_serialized),
            "observed_improving_sectors": sum(1 for r in observations_serialized if r["is_observed"]),
            "entry_qualified_before_regime": sum(
                1 for r in observations_serialized if r["is_entry_qualified"]
            ),
            "weekly_actionable_candidates": len(enriched_candidates) if weekly_confirmed else 0,
            "weekly_preview_candidates": len(enriched_candidates),
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
        "PARTIAL": "일부 조회 실패 · 수신된 대표만 평가",
        "STALE": "기준 일봉 지연 · 신규 후보 보류",
        "INSUFFICIENT_COVERAGE": "동일 날짜 수신율 미달 · 신규 후보 보류",
        "ERROR": "조회 오류 · 신규 후보 보류",
    }
    as_of = record.get("as_of") or "미확인"
    fetched_at = record.get("fetched_at") or "미확인"
    total_req = int(record.get("requested_symbol_count", 0) or 0)
    fresh_cnt = int(record.get("fresh_symbol_count", 0) or 0)
    result = record.get("result") if isinstance(record.get("result"), Mapping) else {}

    weekly_confirmed = bool(result.get("weekly_rebalance_confirmed", False))
    cadence_label = (
        "🟢 주간 정기 선발 마감일 확정 (다음 거래일 1회 수동 확인 대상)"
        if weekly_confirmed
        else "🕒 주중 관찰일 (정기 신규 편입은 주간 마지막 거래일 마감 1회만 확정)"
    )

    lines = [
        f"### 🧭 섹터 상대강도(RS20) 관찰 · 주간 후보 ({SPEC_TASK_ID} v{SPEC_VERSION})",
        "",
        f"> 갱신: `{fetched_at}` · 기준 완료 일봉: `{as_of}` · 실행 상태: {status_labels.get(status, status)} (`{total_req}`개 중 `{fresh_cnt}`개 최신 확인) · 매핑: `{PUBLIC_MAPPING_VERSION}` (`{PUBLIC_MAPPING_VALID_FROM}~{PUBLIC_MAPPING_VALID_UNTIL}` 6개월 고정)",
        "",
        f"> **편입 주기:** {cadence_label} · **데이터 한계 고지:** 공개 KRX 원가격 일봉 기반 전진 관찰용 패널입니다(분배금 재투자 총수익·과거 상폐 PIT 미검증, 전략 수익성 미확정, 자동 주문 없음).",
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
    candidates = list(result.get("weekly_candidates_b", []))
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
        f"> **섹터 게이트 요약 (`{eval_cnt}`개 섹터 평가):** 관찰 강세(`RS20>0` & `5일 개선>0`) `{obs_cnt}`개 → 추세·비과열 자격(`>SMA60` & `≤SMA20+2ATR20`) `{qual_cnt}`개 → 지역 레짐(`≥SMA120`) 통과 상위 최대 3개 **`{len(candidates)}`건**",
        "",
        "| 순위 | 지역·섹터 | 대표 ETF | 완료일 종가 | RS20 (5일 변화 · 상태) | E1 다음 거래일 1회 확인 허용가 | 이탈 기준선 / 3,000만원 참고수량 |",
        "|---:|:---|:---|---:|:---|:---|:---|",
    ]

    if candidates:
        for cand in candidates:
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

    # 3. Full Sector RS20 Daily Observation Ledger (including Bear Regime sectors)
    observations = list(result.get("observations", []))
    lines += [
        "#### 3) 전 섹터 상대강도(`RS20`) 일일 관찰표 (약세장 구간 전환 추적 포함)",
        "",
        "| 지역·섹터 | 대표 ETF | 완료일 종가 | RS20 (벤치마크 대비) | 5일 변화 | 전환/지속 표기 | 자격 판정 |",
        "|:---|:---|---:|---:|---:|:---|:---|",
    ]
    for row in observations:
        reg = _safe_md(row["region"])
        sec = _safe_md(row["sector"])
        code = _safe_md(row["code"])
        name = _safe_md(row["name"])
        c_px = float(row["close"])
        rs20_pct = float(row["rs20"]) * 100.0
        imp_pct = float(row["rs20_improvement_5d"]) * 100.0
        raw_label = str(row["rs_transition_label"])
        label = _safe_md("상대강도 열위 (≤0)" if raw_label == "RS_NON_POSITIVE" else raw_label)
        failures = list(row.get("gate_failure_reasons", []))
        if not failures:
            gate_str = "✅ 신규 자격 충족"
        elif row.get("is_observed") and failures == ["지역 레짐 차단(<SMA120)"]:
            gate_str = "🟡 강세 관찰 중 (`레짐 <SMA120 차단`)"
        elif row.get("is_observed"):
            gate_str = "👀 관찰 (`" + ", ".join(_safe_md(x) for x in failures) + "`)"
        else:
            gate_str = "⚪ 미충족 (`" + ", ".join(_safe_md(x) for x in failures[:3]) + "`)"
        lines.append(
            f"| `{reg}` · {sec} | [{name}](https://finance.naver.com/item/main.naver?code={code}) (`{code}`) | "
            f"{c_px:,.0f}원 | {rs20_pct:+.2f}% | {imp_pct:+.2f}%p | `{label}` | {gate_str} |"
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
