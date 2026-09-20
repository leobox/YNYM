"""GitHub Actions 및 로컬 실행용 '추세 돌파 신호판'(구 VCP 슈퍼 신고가 스캐너) 실행 스크립트. (T-052 재설계)

기능:
1. 네이버 중소형주 유니버스(150개) 수신
2. 야후 파이낸스 **일봉**(완료된 봉만) 수신 — 장 마감(16:00 KST) 전에는 오늘 일봉을 쓰지 않는다
3. scanner.daily_breakout.daily_checks 로 4체크(추세/60일 돌파/거래량/과열 아님) 판정
4. 🟢 신호(최대 5) / 🟡 관찰(최대 5) 카드 렌더링, quant-research/data/vcp_snapshots/ 및 루트 README.md 갱신
5. 신호 원장(signal_ledger.csv)에 기록하고 지난 신호의 실제 결과(목표/손절/만기)를 일봉으로 확정
6. 실행별 manifest / universe 스냅샷 보존
7. [절대 불변식] 주문/매수 API 일체 없음 (순수 관찰/기록)
"""
import os
import sys
import json
import time
import importlib
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "quant-research"))

from scanner import daily_breakout as db
from scanner import signal_board as sb

SCAN_LIMIT = 150
WORKERS = 6
MCAP_MIN, MCAP_MAX = 100_000_000_000, 5_000_000_000_000
MIN_PRICE = db.MIN_PRICE
MIN_SESSIONS = 100  # 60일 피봇 + MA60/120 워밍업에 필요한 최소 완료 일봉 수
OUTPUT_DIR = ROOT / "quant-research" / "data" / "vcp_snapshots"
LEDGER_PATH = OUTPUT_DIR / "signal_ledger.csv"
ROOT_README_PATH = ROOT / "README.md"
VCP_MARK_START = "<!-- VCP_DASHBOARD:START -->"
VCP_MARK_END = "<!-- VCP_DASHBOARD:END -->"


def get_json(url: str, params: dict | None = None, max_retries: int = 3, timeout: int = 10):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    backoff = 1.0
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2.0
    raise RuntimeError(f"HTTP 실패: {url} | {str(last_err)[:100]}")


def get_universe():
    try:
        fdr = importlib.import_module("FinanceDataReader")
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "finance-datareader"])
        fdr = importlib.import_module("FinanceDataReader")
    listing = fdr.StockListing("KRX")
    valid = set(listing.loc[listing.Market.isin(["KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"]), "Code"].astype(str))

    rows = []
    number = lambda x: pd.to_numeric(str(x).replace(",", ""), errors="coerce")
    for market in ("KOSPI", "KOSDAQ"):
        for page in range(1, 31):
            try:
                stocks = get_json(
                    f"https://m.stock.naver.com/api/stocks/marketValue/{market}",
                    {"page": page, "pageSize": 100}, timeout=10
                ).get("stocks", [])
            except Exception:
                break
            if not stocks:
                break
            for x in stocks:
                code, name = x["itemCode"], x["stockName"]
                cap, price = number(x["marketValue"]) * 1e8, number(x["closePrice"])
                if code not in valid or not code.endswith("0") or "스팩" in name or "리츠" in name:
                    continue
                if MCAP_MIN <= cap <= MCAP_MAX and price >= MIN_PRICE:
                    rows.append({
                        "code": code, "name": name, "market": market,
                        "price": price, "market_cap": cap,
                        "amount": number(x["accumulatedTradingValue"])
                    })
            if number(stocks[-1]["marketValue"]) * 1e8 < MCAP_MIN:
                break
    if not rows:
        raise RuntimeError("유니버스 조회 실패")
    return (pd.DataFrame(rows).dropna(subset=["amount"]).sort_values("amount", ascending=False)
            .drop_duplicates("code").head(SCAN_LIMIT).to_dict("records"))


def yahoo_daily(item) -> pd.DataFrame:
    """야후 chart 응답(1d)을 KST 일자 index의 일봉으로 변환(수정 전 OHLCV, 정합성 검증 포함)."""
    quote = item["indicators"]["quote"][0]
    index = (pd.to_datetime(item["timestamp"], unit="s", utc=True)
             .tz_convert("Asia/Seoul").tz_localize(None).normalize())
    df = pd.DataFrame({"Open": quote["open"], "High": quote["high"], "Low": quote["low"],
                       "Close": quote["close"], "Volume": quote["volume"]}, index=index)
    return db.clean_daily_bars(df)


def fetch_daily(code: str, market: str, now_ts: pd.Timestamp) -> pd.DataFrame | None:
    symbol = code + (".KQ" if market == "KOSDAQ" else ".KS")
    payload = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                       {"range": "1y", "interval": "1d"}, timeout=10)["chart"]
    if not payload.get("result"):
        return None
    return db.drop_incomplete_today(yahoo_daily(payload["result"][0]), now_ts)


def code_version() -> dict:
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    try:
        return {"git_sha": git("rev-parse", "--short", "HEAD"), "dirty": bool(git("status", "--porcelain"))}
    except Exception:
        return {"git_sha": None, "dirty": None}


def load_ledger() -> pd.DataFrame:
    if not LEDGER_PATH.exists():
        return sb.new_ledger()
    return pd.read_csv(LEDGER_PATH, dtype={"code": str, "market": str, "signal_date": str,
                                           "entry_date": str, "exit_date": str}, encoding="utf-8-sig",
                       keep_default_na=False, na_values=[""])[sb.LEDGER_COLS]


def run_scan():
    now = datetime.now(KST)
    now_ts = pd.Timestamp(now)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    run_id = now.strftime("%Y%m%d_%H%M")
    version = code_version()  # 산출물을 쓰기 전에 기록(dirty 판정 왜곡 방지)

    print(f"[{now_str} KST] 추세 돌파 신호판 스캔 시작 (대상 {SCAN_LIMIT}종목, 일봉 완료봉 기준)...", flush=True)
    uni = get_universe()

    def process(rec):
        try:
            df = fetch_daily(rec["code"], rec["market"], now_ts)
        except Exception:
            return {"rec": rec, "state": "fail"}
        if df is None or len(df) < MIN_SESSIONS:
            return {"rec": rec, "state": "short"}
        return {"rec": rec, "state": "ok", "daily": df}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fetched = list(pool.map(process, uni))

    ok = [f for f in fetched if f["state"] == "ok"]
    n_fail = sum(f["state"] == "fail" for f in fetched)
    n_short = sum(f["state"] == "short" for f in fetched)
    if not ok:
        raise RuntimeError(f"일봉 수신 전부 실패 또는 부족 (실패 {n_fail}, 부족 {n_short})")

    # 시장 마지막 완료 세션 = 종목별 마지막 봉 일자의 최빈값(거래정지 종목 등 낡은 봉은 제외)
    last_dates = pd.Series([f["daily"].index[-1] for f in ok])
    last_session = last_dates.mode().max()

    greens, yellows, stale, n_trend = [], [], 0, 0
    daily_by_code = {}
    for f in ok:
        rec, df = f["rec"], f["daily"]
        daily_by_code[rec["code"]] = df
        if df.index[-1] != last_session:
            stale += 1
            continue
        checks = db.daily_checks(df)
        last = checks.iloc[-1]
        n_trend += int(bool(last["trend_ok"]))
        close = float(df["Close"].iloc[-1])
        if bool(last["signal"]):
            greens.append({
                "code": rec["code"], "name": rec["name"], "market": rec["market"], "close": close,
                "pivot_level": float(last["pivot_level"]), "vol_spike": float(last["vol_spike"]),
                "extension_atr": float(last["extension_atr"]),
                "turnover": close * float(df["Volume"].iloc[-1]), "signal_date": last_session,
            })
            continue
        need = db.next_session_requirements(df, last)
        if need:
            yellows.append({"code": rec["code"], "name": rec["name"], "market": rec["market"],
                            "close": close, **need})

    top_green, green_more = sb.pick_greens(greens)
    top_yellow, yellow_more = sb.pick_yellows(yellows)

    # 전진 기록 원장: 신호는 전부(잘린 것 포함) 기록, 지난 신호의 결과를 일봉으로 확정
    ledger = sb.append_signals(load_ledger(), greens, run_id)
    for code, market in sb.unresolved_codes(ledger):
        if code in daily_by_code:
            continue
        try:
            df = fetch_daily(code, market, now_ts)
            if df is not None:
                daily_by_code[code] = df
        except Exception:
            print(f"[경고] 원장 종목 {code} 일봉 수신 실패 - 결과 확정 보류", flush=True)
    ledger = sb.resolve_ledger(ledger, daily_by_code, db.resolve_outcome)
    summary = sb.summarize_ledger(ledger)

    meta = {"now_str": now_str, "last_session": str(last_session.date()), "n_total": len(uni),
            "n_ok": len(ok) - stale, "n_fail": n_fail,
            "n_trend": n_trend}
    board = sb.render_board(meta, top_green, green_more, top_yellow, yellow_more, summary)

    print(f"스캔 완료: 분석 {meta['n_ok']}/{len(uni)} (실패 {n_fail}, 데이터부족 {n_short}, 낡은봉 {stale}) | "
          f"🟢 {len(greens)}개 | 🟡 {len(yellows)}개 | 기준일 {meta['last_session']}", flush=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    header = (f"# 🚦 추세 돌파 신호판 ({now_str} KST)\n\n")
    (OUTPUT_DIR / "latest_vcp.md").write_text(
        header + board + "\n---\n*※ 완료된 일봉 기준 연구용 지표이며, 주문/매수 API를 포함하지 않습니다.*\n",
        encoding="utf-8")

    if greens or yellows:
        rows = [{"tier": "GREEN", **{k: v for k, v in g.items() if k != "signal_date"},
                 "signal_date": str(last_session.date())} for g in greens]
        rows += [{"tier": "YELLOW", **y} for y in yellows]
        pd.DataFrame(rows).to_csv(OUTPUT_DIR / f"{run_id}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(uni).to_csv(OUTPUT_DIR / f"universe_{run_id}.csv", index=False, encoding="utf-8-sig")
    ledger.to_csv(LEDGER_PATH, index=False, encoding="utf-8-sig")
    manifest = {
        "run_id": run_id, "fetched_at": now.isoformat(), "last_session": meta["last_session"],
        "universe": {"source": "naver marketValue + FinanceDataReader KRX listing", "count": len(uni),
                     "rule": f"시총 {MCAP_MIN:,}~{MCAP_MAX:,}원, 거래대금 상위 {SCAN_LIMIT}, 종목군 기준 시각=실행 시각",
                     "survivorship": "실행 시점 종목군(과거 종목군·상폐 미포함)"},
        "prices": {"source": "yahoo finance chart v8", "interval": "1d", "range": "1y", "adjusted": False,
                   "today_bar_rule": f"{db.MARKET_DONE_HOUR}:00 KST 전에는 오늘 일봉 제외"},
        "params": {"lookback": db.LOOKBACK, "min_vol_spike": db.MIN_VOL_SPIKE,
                   "max_extension_atr": db.MAX_EXTENSION_ATR, "watch_band": db.WATCH_BAND,
                   "target": db.TARGET_PCT, "stop": db.STOP_PCT, "max_sessions": db.MAX_SESSIONS},
        "counts": {"analyzed": meta["n_ok"], "fetch_fail": n_fail, "short_history": n_short, "stale_bar": stale,
                   "trend_ok": n_trend, "green": len(greens), "yellow": len(yellows)},
        "code": version, "ledger": summary,
    }
    (OUTPUT_DIR / f"manifest_{run_id}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    update_root_readme(board)
    print(f"결과 파일 저장 완료: {OUTPUT_DIR / 'latest_vcp.md'}", flush=True)
    return len(greens), len(yellows)


def update_root_readme(embed_md: str) -> None:
    if not ROOT_README_PATH.exists():
        print(f"[경고] 루트 README.md가 없습니다: {ROOT_README_PATH}", flush=True)
        return
    content = ROOT_README_PATH.read_text(encoding="utf-8")
    if VCP_MARK_START not in content or VCP_MARK_END not in content:
        print("[경고] 루트 README.md에 VCP_DASHBOARD 마커가 없습니다.", flush=True)
        return
    pre, _, rest = content.partition(VCP_MARK_START)
    _, _, post = rest.partition(VCP_MARK_END)
    new_content = f"{pre}{VCP_MARK_START}\n\n{embed_md}\n\n{VCP_MARK_END}{post}"
    ROOT_README_PATH.write_text(new_content, encoding="utf-8")
    print(f"루트 README.md VCP 대시보드 마커 갱신 완료 ({ROOT_README_PATH})", flush=True)


if __name__ == "__main__":
    run_scan()
