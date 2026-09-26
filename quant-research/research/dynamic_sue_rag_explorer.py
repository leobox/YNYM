"""Read-only Mode 2 daily decision explorer with audited earnings context.

T-094's short, present-day finance snapshot supplies an EPS growth proxy only.
It is never treated as standardized SUE or used to change portfolio actions.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from research.sue_engine import SueEngine
from scripts.pure_quant_portfolio_manager import PortfolioManager, compute_factor_rankings


DEFAULT_EPS_PATH = Path(__file__).resolve().parents[1] / "data/fundamental/quarterly_sue_raw.json"


def earnings_context(engine: SueEngine, code: str, as_of: date,
                     calendar: pd.DatetimeIndex) -> dict[str, Any]:
    """Never reveal a present-day snapshot in an earlier historical decision."""
    stock = engine.raw_data.get(code)
    base = {"status": "UNAVAILABLE", "standardized_sue": None,
            "score_delta": 0.0, "score_eligible": False}
    if not stock:
        return {**base, "reason": "NO_EPS_SOURCE"}
    fetched = pd.to_datetime(stock.get("fetched_at"), errors="coerce")
    if pd.isna(fetched):
        return {**base, "reason": "FETCH_TIME_UNKNOWN"}
    # A date-only comparison is deliberately conservative about timezone and
    # intraday ingestion. The snapshot can enter a completed-close report only
    # after its collection date.
    if as_of <= fetched.date():
        return {**base, "reason": "SNAPSHOT_AFTER_SIGNAL",
                "fetched_at": str(fetched)}

    events = [e for e in engine.get_events_for_stock(code)
              if e.disclosure_date <= as_of.isoformat() and e.period[4:] in {"03", "06", "09", "12"}]
    if not events:
        return {**base, "reason": "NO_MATCHED_QUARTER", "fetched_at": str(fetched)}
    event = max(events, key=lambda e: (e.disclosure_date, e.period))
    if not event.raw_title:
        quality = "FALLBACK_DATE_UNVERIFIED"
        days = None
    else:
        quality = "KEYWORD_MATCH_UNVERIFIED"
        days = int(((calendar > pd.Timestamp(event.disclosure_date)) &
                    (calendar <= pd.Timestamp(as_of))).sum())
    cohort = ("0_20d" if days is not None and days <= 20 else
              "21_40d" if days is not None and days <= 40 else
              "41_60d" if days is not None and days <= 60 else
              "OUTSIDE_60D" if days is not None else None)
    return {**base, "status": "YOY_EPS_PROXY_UNVERIFIED",
            "reason": "NO_PRIOR_ERROR_STD_OR_VERIFIED_FIRST_DISCLOSURE",
            "fetched_at": str(fetched), "period": event.period,
            "actual_eps": event.actual_eps, "prior_year_eps": event.prior_eps,
            "delta_eps": event.delta_eps, "growth_direction": "UP" if event.delta_eps > 0 else "DOWN_OR_FLAT",
            "claimed_disclosure_date": event.disclosure_date,
            "disclosure_title": event.raw_title, "date_quality": quality,
            "indicative_trading_days_elapsed": days, "indicative_cohort": cohort}


def explore(manager: PortfolioManager, as_of_date: str | None = None,
            eps_path: Path | str = DEFAULT_EPS_PATH, limit: int = 10) -> dict[str, Any]:
    if not 1 <= limit <= 30:
        raise ValueError("limit must be between 1 and 30")
    plan = manager.generate_daily_plan(as_of_date)
    dt = pd.Timestamp(plan["as_of"])
    manager.load_data()
    ranking = compute_factor_rankings(manager.universe, dt)
    if not ranking.empty:
        ranking = ranking.sort_values(["composite_score", "code"],
                                      ascending=[False, True]).reset_index(drop=True)
    rank_by_code = {row["code"]: (index + 1, row)
                    for index, row in ranking.iterrows()}
    state = manager.load_state()
    codes = list(dict.fromkeys(([] if ranking.empty else ranking.head(limit)["code"].tolist()) +
                               sorted(state.positions)))
    path = Path(eps_path)
    engine = SueEngine(path)
    calendar = manager.breadth.dropna().index
    actions = {a["code"]: a for a in plan["actions"]}
    candidates = []
    for code in codes:
        rank_entry = rank_by_code.get(code)
        rank, row = rank_entry if rank_entry else (None, None)
        if row is not None:
            thesis = manager.rag.generate_thesis(
                code, float(row["mom60_5"]), float(row["risk_adj_mom"]),
                float(row["cmf20"]), float(row["composite_score"]),
                float(row["close"]))
            factors = {"mom60_5": float(row["mom60_5"]),
                       "risk_adj_mom": float(row["risk_adj_mom"]),
                       "cmf20": float(row["cmf20"]),
                       "composite_score": float(row["composite_score"])}
        else:
            profile = manager.rag.get_stock_profile(code)
            thesis = {"name": profile.get("name", code),
                      "thesis_full": "팩터 적격 조건 밖; 보유 판정은 일일 계획 참조"}
            factors = None
        action = actions.get(code)
        decision = ("DATA_INCOMPLETE" if plan["status"] == "DATA_INCOMPLETE"
                    else action["action"] if action else "WATCH")
        decision_reason = ("HELD_PRICE_MISSING" if plan["status"] == "DATA_INCOMPLETE"
                           else action.get("reason") if action else "NO_PORTFOLIO_ACTION")
        candidates.append({"code": code, "name": thesis["name"],
                           "rank": rank, "held": code in state.positions,
                           "decision": decision, "decision_reason": decision_reason,
                           "factors": factors, "rag_reason": thesis["thesis_full"],
                           "earnings": earnings_context(engine, code, dt.date(), calendar)})
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return {"as_of": plan["as_of"], "data_last_date": plan.get("data_last_date"),
            "plan_status": plan["status"], "breadth_pct": plan.get("breadth_pct"),
            "rebalance_due": plan.get("rebalance_due"),
            "source": "Naver current finance snapshot via T-094",
            "source_sha256": source_hash,
            "eps_snapshot_available": path.exists(),
            "sue_score_applied": False,
            "sue_reason": "No prior-error standard deviation or verified first EPS disclosure; T-094 boost had zero measured effect.",
            "actions": plan["actions"], "candidates": candidates}


def render_markdown(result: dict[str, Any]) -> str:
    lines = ["# 모드 2 동적 EPS·RAG 판정 탐색기", "",
             f"- 평가 종가: {result['as_of']} / 가격 마지막 저장일: {result['data_last_date']}",
             f"- 일일 판정: {result['plan_status']} / 시장 breadth: {result['breadth_pct']}",
             "- EPS는 표준화 SUE가 아닌 검증 전 전년 동기 증감 참고값입니다. 운영 점수 가산점: 0.",
             "- `SNAPSHOT_AFTER_SIGNAL`: EPS 캐시 수집일이 평가일 이후이므로 그 날짜의 EPS를 표시하지 않습니다.",
             f"- EPS 캐시 SHA-256: `{result['source_sha256'] or '없음'}`", "",
             "| 순위 | 종목 | 일일 판정 | 3팩터 RAG 근거 | EPS 참고 상태 |",
             "|---:|---|---|---|---|"]
    for item in result["candidates"]:
        e = item["earnings"]
        if e["status"] == "YOY_EPS_PROXY_UNVERIFIED":
            eps = (f"{e['period']} EPS {e['actual_eps']:g} / 전년 {e['prior_year_eps']:g}; "
                   f"{e['growth_direction']}; {e['date_quality']}; {e['indicative_cohort'] or '경과일 미확인'}")
        else:
            eps = e["reason"]
        lines.append(f"| {item['rank'] or '-'} | {item['name']} ({item['code']}) | "
                     f"{item['decision']} · {item['decision_reason']} | "
                     f"{item['rag_reason']} | {eps} |")
    return "\n".join(lines) + "\n"
