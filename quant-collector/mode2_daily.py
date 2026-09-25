"""After-close Mode 2 factor panel. Public daily prices and paper records only."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from datetime import datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-research" / "scripts"))
from pure_quant_portfolio_manager import (  # noqa: E402
    PortfolioManager,
    calculate_market_breadth,
    compute_factor_rankings,
)
from factor_evidence import explain_rank, profile_fact

KST = ZoneInfo("Asia/Seoul")
DATA = ROOT / "quant-collector" / "data"
README = ROOT / "README.md"
START = "<!-- MODE2_DAILY:START -->"
END = "<!-- MODE2_DAILY:END -->"
USER_AGENT = "Mozilla/5.0 (compatible; LeoboxMode2Research/1.0)"
MIN_SYMBOLS = 120
UNIVERSE_SIZE = 150


def latest_universe(data_dir: Path = DATA) -> list[dict]:
    files = sorted(data_dir.glob("20??????/*_universe.csv"))
    if not files:
        raise RuntimeError("collector universe snapshot missing")
    frame = pd.read_csv(files[-1], dtype={"code": str})
    required = {"code", "name", "market", "market_cap"}
    if not required.issubset(frame.columns):
        raise ValueError(f"universe columns missing: {required - set(frame.columns)}")
    frame = frame.dropna(subset=list(required)).drop_duplicates("code")
    frame = frame[frame["market"].isin(["KOSPI", "KOSDAQ"])]
    frame = frame.sort_values(["market_cap", "code"], ascending=[False, True])
    return frame.head(UNIVERSE_SIZE).to_dict("records")


def chart_to_frame(payload: dict, expected_symbol: str) -> pd.DataFrame:
    chart = payload.get("chart", {})
    if chart.get("error") or not chart.get("result"):
        raise ValueError(f"chart unavailable: {chart.get('error')}")
    item = chart["result"][0]
    meta = item.get("meta", {})
    if meta.get("symbol") != expected_symbol or meta.get("exchangeTimezoneName") != "Asia/Seoul":
        raise ValueError("symbol or exchange timezone mismatch")
    stamp = item.get("timestamp") or []
    quote = item["indicators"]["quote"][0]
    names = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
    if not stamp or any(len(quote.get(src, [])) != len(stamp) for src in names.values()):
        raise ValueError("incomplete daily chart arrays")
    dates = pd.to_datetime(stamp, unit="s", utc=True).tz_convert(KST).normalize().tz_localize(None)
    frame = pd.DataFrame({dst: quote[src] for dst, src in names.items()}, index=dates)
    frame.index.name = "Date"
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError("duplicate or unsorted daily dates")
    values = frame[list(names)].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (frame[["Open", "High", "Low", "Close"]] <= 0).any().any():
        raise ValueError("non-finite or non-positive OHLCV")
    if (frame["Volume"] < 0).any() or (frame["High"] < frame[["Open", "Low", "Close"]].max(axis=1)).any() or (frame["Low"] > frame[["Open", "High", "Close"]].min(axis=1)).any():
        raise ValueError("invalid daily OHLCV")
    return frame


def fetch_symbol(row: dict, session: requests.Session | None = None) -> tuple[str, pd.DataFrame, bytes]:
    code = str(row["code"]).zfill(6)
    symbol = code + (".KQ" if row["market"] == "KOSDAQ" else ".KS")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    last_error = None
    for attempt in range(3):
        try:
            getter = requests.get if session is None else session.get
            response = getter(url, params={"range": "1y", "interval": "1d"},
                              headers={"User-Agent": USER_AGENT}, timeout=10)
            response.raise_for_status()
            payload = response.json()
            frame = chart_to_frame(payload, symbol)
            return code, frame, response.content
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"{symbol}: {last_error}")


def choose_session(frames: dict[str, pd.DataFrame], now: datetime) -> tuple[pd.Timestamp, dict[str, pd.DataFrame]]:
    eligible = {}
    for code, frame in frames.items():
        clean = frame
        # Yahoo's same-day candle may still be forming before the final close settles.
        if now.timetz().replace(tzinfo=None) < clock_time(16, 10):
            clean = clean[clean.index.date < now.date()]
        if len(clean) >= 130:
            eligible[code] = clean
    if len(eligible) < MIN_SYMBOLS:
        raise ValueError(f"only {len(eligible)} symbols with 130+ completed daily bars")
    dates = sorted(set().union(*(set(frame.index) for frame in eligible.values())), reverse=True)
    candidate_dates = [date for date in dates if sum(date in frame.index for frame in eligible.values()) >= MIN_SYMBOLS]
    if not candidate_dates:
        raise ValueError("no common completed session covering 120 symbols")
    decision_date = pd.Timestamp(candidate_dates[0])
    aligned = {code: frame.loc[:decision_date] for code, frame in eligible.items()
               if decision_date in frame.index}
    if len(aligned) < MIN_SYMBOLS:
        raise ValueError("common session coverage fell below 120")
    return decision_date, aligned


def render_panel(now: datetime, decision_date: pd.Timestamp, universe: dict[str, pd.DataFrame],
                 names: dict[str, str], paper_state: Path, plan_state: dict) -> tuple[str, dict]:
    breadth = calculate_market_breadth(universe)
    b_val = float(breadth.loc[decision_date])
    if not math.isfinite(b_val):
        raise ValueError("market breadth undefined")
    ranking = compute_factor_rankings(universe, decision_date)
    regime = "현금 방어 검토" if b_val < 40.0 else "신규 편입 검토 가능"
    manager = PortfolioManager(state_file=str(paper_state))
    manager.universe = universe
    manager.breadth = breadth
    state = manager.load_state()
    missing_holdings = [code for code in state.positions if code not in universe]
    triggers = [] if missing_holdings else manager.check_catastrophic_stops(str(decision_date.date()))
    stale = decision_date.date() != now.date()
    if stale:
        regime = "새 일봉 전까지 판단 보류"
    dates = list(breadth.dropna().index)
    previous = plan_state.get("last_plan_date")
    last_idx = dates.index(pd.Timestamp(previous)) if previous and pd.Timestamp(previous) in dates else None
    due = last_idx is None or len(dates) - 1 - last_idx >= 20
    plan_updated = False
    if not stale and due and b_val >= 40 and len(ranking) >= 10 and not missing_holdings:
        plan_state = {
            "last_plan_date": str(decision_date.date()),
            "top_codes": ranking.head(10)["code"].tolist(),
            "reference_closes": {row.code: float(row.close) for row in ranking.head(10).itertuples()},
        }
        plan_updated = True
    lines = [
        "### 🧮 모드 2 · 일봉 팩터 관찰 (가상)", "",
        f"> 일봉 기준 `{decision_date.date()}` · 확인 `{now.strftime('%Y-%m-%d %H:%M')} KST` · "
        f"유효 종목 `{len(universe)}/{UNIVERSE_SIZE}` · 자격 통과 `{len(ranking)}종목` · "
        f"Breadth(SMA60 위) `{b_val:.1f}%` · **{regime}**", "",
        "> 팩터: 60일 모멘텀(최근 5일 제외) 30% · 변동성 조정 모멘텀 40% · CMF20 30%. "
        "현재 시총 상위 종목군 기준이며 과거 3년 성과를 재현한 표본은 아닙니다.", "",
        "> 아래 이유는 [수신 원본·실패·해시](quant-collector/data/mode2_latest_manifest.json)와 "
        "[계산식](quant-research/scripts/pure_quant_portfolio_manager.py)에 연결된 수치 설명입니다. "
        "회사 설명은 공식 출처와 확인일이 있는 항목만 별도 참고로 붙입니다. "
        "출처 없는 473개 정적 설명은 사용하지 않습니다.", "",
        "> 변동성 조정 모멘텀은 일반적인 샤프 지수가 아닙니다. CMF는 종가 위치·거래량 지표이며 "
        "기관·외국인 순매수를 식별하지 않습니다. 점수는 자격 통과 종목끼리의 상대 순위입니다.", "",
    ]
    if stale:
        lines += ["> ⚠️ 오늘 날짜의 새 완료 일봉이 없습니다(휴장 또는 공급 지연). "
                  "위에 표시된 과거 거래일 기준값만 관측하고 새 계획은 만들지 않았습니다.", ""]
    if not ranking.empty:
        lines += ["**완료 일봉 팩터 상위 5종목 · 관찰 순위**", "",
                  "| 순위 | 종목·평가일 종가·상대점수 | 수치 기반 판정이유·출처 있는 회사 참고 |",
                  "|---:|:---|:---|"]
        for idx, row in enumerate(ranking.head(5).itertuples(), 1):
            company_context = profile_fact(row.code)
            detail = explain_rank(row)
            if company_context:
                detail += f"<br>{company_context}"
            lines.append(f"| {idx} | **{names.get(row.code, row.code)}** ({row.code})<br>"
                         f"{row.close:,.0f}원 · **{row.composite_score:.3f}** | "
                         f"{detail} |")
        lines.append("")
    if missing_holdings:
        lines += [f"> ⚠️ 가상 보유 `{', '.join(missing_holdings)}`의 일봉이 없어 에어백 판단을 보류했습니다.", ""]
    elif state.positions:
        lines += [f"> -15% 에어백 관찰: 가상 보유 `{len(state.positions)}건`, "
                  f"당일 저가 기준 접촉 `{len(triggers)}건` (장마감 후 관측, 실시간 체결 아님).", ""]
        for hit in triggers:
            lines += [f"> ⚠️ `{hit['code']}`: 저가가 기준 `{hit['stop_price']:,.0f}원`에 접촉. "
                      "실제 체결 가능 가격은 확인되지 않았습니다.", ""]
    else:
        lines += ["> -15% 에어백 관찰: 가상 보유 기록 0건 · 검사 대상 없음.", ""]
    if b_val < 40:
        lines += ["> 월간 검토표: 시장 국면 40% 미만으로 신규 편입 후보를 표시하지 않습니다.", ""]
    elif plan_state.get("top_codes"):
        label = "이번 완료 일봉에 갱신" if plan_updated else "마지막 확정 계획 유지"
        lines += [f"> 20거래일 리밸런싱 검토표: `{plan_state['last_plan_date']}` 생성 · {label} · "
                  "종목당 목표 비중 10%. 실제 주문·체결 기록이 아닙니다.", "",
                  "| 순위 | 종목(코드) | 계획 당시 종가 | 목표 비중 |", "|---:|:---|---:|---:|"]
        for idx, code in enumerate(plan_state["top_codes"], 1):
            px = plan_state["reference_closes"][code]
            lines.append(f"| {idx} | {names.get(code, code)} ({code}) | {px:,.0f}원 | 10% |")
        lines.append("")
    else:
        lines += ["> 월간 검토표: 완료된 최신 일봉과 충분한 팩터 후보가 확보될 때 생성합니다.", ""]
    lines += ["> 실거래 주문은 생성·전송하지 않습니다. 수익률은 후속 기간 검증 전까지 미확정입니다.", ""]
    return "\n".join(lines), plan_state


def update_readme(panel: str, path: Path = README) -> None:
    content = path.read_text(encoding="utf-8")
    if content.count(START) != 1 or content.count(END) != 1:
        raise ValueError("MODE2_DAILY README markers missing or duplicated")
    before, tail = content.split(START, 1)
    _, after = tail.split(END, 1)
    path.write_text(before + START + "\n\n" + panel.rstrip() + "\n" + END + after,
                    encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-root", type=Path, default=DATA / "mode2_snapshots")
    parser.add_argument("--plan-state", type=Path, default=DATA / "mode2_plan_state.json")
    parser.add_argument("--paper-state", type=Path, default=DATA / "mode2_portfolio_state.json")
    parser.add_argument("--manifest", type=Path, default=DATA / "mode2_latest_manifest.json")
    args = parser.parse_args()
    now = datetime.now(KST)
    run_id = now.strftime("%Y%m%dT%H%M%S%z")
    rows = latest_universe()
    names = {str(row["code"]).zfill(6): str(row["name"]) for row in rows}
    manifest = {"run_id": run_id, "fetched_at_kst": now.isoformat(),
                "source": "Yahoo Finance chart API, range=1y interval=1d; raw OHLCV",
                "universe": "latest collector market-cap snapshot, top 150 current names",
                "requested": len(rows), "succeeded": [], "failed": []}
    frames = {}
    run_dir = args.snapshot_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    pool = ThreadPoolExecutor(max_workers=16)
    try:
        jobs = {pool.submit(fetch_symbol, row): row for row in rows}
        seen = set()
        try:
            for future in as_completed(jobs, timeout=75):
                seen.add(future)
                row = jobs[future]
                code = str(row["code"]).zfill(6)
                try:
                    code, frame, raw = future.result()
                    with gzip.open(run_dir / f"{code}.json.gz", "wb") as out:
                        out.write(raw)
                    frames[code] = frame
                    manifest["succeeded"].append({"code": code, "rows": len(frame),
                        "last_date": str(frame.index[-1].date()), "sha256": hashlib.sha256(raw).hexdigest()})
                except Exception as exc:
                    manifest["failed"].append({"code": code, "error": str(exc)[:180]})
        except FuturesTimeout:
            pass
        for future, row in jobs.items():
            if future not in seen:
                future.cancel()
                manifest["failed"].append({"code": str(row["code"]).zfill(6),
                                           "error": "fetch deadline exceeded"})
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    try:
        decision_date, aligned = choose_session(frames, now)
        previous = json.loads(args.plan_state.read_text(encoding="utf-8")) if args.plan_state.exists() else {}
        panel, plan = render_panel(now, decision_date, aligned, names, args.paper_state, previous)
        manifest["decision_date"] = str(decision_date.date())
        manifest["aligned"] = len(aligned)
        update_readme(panel)
        args.plan_state.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    except (ValueError, KeyError, IndexError) as exc:
        manifest["evaluation_error"] = str(exc)
        # Keep the last validated panel and record the failed fetch in the manifest.
        print(f"[mode2] panel unchanged: {exc}", flush=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[mode2] source {len(frames)}/{len(rows)}, decision {manifest.get('decision_date', 'none')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
