"""GitHub Actions 및 로컬 실행용 VCP 슈퍼 신고가 스캐너 실행 스크립트.

기능:
1. 네이버 중소형주 유니버스(150개) 수신
2. 야후 파이낸스 60분봉 완료봉 수신
3. scanner.vcp.detect_vcp를 통한 VCP 수축+피봇돌파+거래량폭발 판정
4. Top 5 최종 후보 및 관찰 후보 선정
5. quant-research/data/vcp_snapshots/ 및 README.md에 대시보드 마크다운 기록
6. [절대 불변식] 주문/매수 API 일체 없음 (순수 관찰/기록)
"""
import os
import sys
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

from scanner.vcp import detect_vcp

SCAN_LIMIT = 150
TOP_N = 5
WORKERS = 6
MCAP_MIN, MCAP_MAX = 100_000_000_000, 5_000_000_000_000
MIN_PRICE = 2000
OUTPUT_DIR = ROOT / "quant-research" / "data" / "vcp_snapshots"
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

def completed_bars(item, now):
    df = pd.DataFrame(item["indicators"]["quote"][0],
                      index=pd.to_datetime(item["timestamp"], unit="s", utc=True).tz_convert("Asia/Seoul"))
    df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df = df[~df.index.duplicated()].sort_index()
    df = df[(df.index.minute == 0) & (df.index.second == 0) & (df.index.hour >= 9) & (df.index.hour <= 15)]
    df = df[~((df.index.hour == 15) & (df.Volume == 0) & (df.High == df.Low))]
    ends = df.index + pd.to_timedelta(np.where(df.index.hour == 15, 30, 60), unit="m")
    return df[ends <= now]

def fetch_bars(rec, now):
    symbol = rec["code"] + (".KQ" if rec["market"] == "KOSDAQ" else ".KS")
    payload = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                       {"range": "60d", "interval": "60m"}, timeout=10)["chart"]
    if not payload.get("result"):
        return None
    return completed_bars(payload["result"][0], now)

def run_scan():
    now = datetime.now(KST)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    timestamp_key = now.strftime("%Y%m%d_%H%M")
    
    print(f"[{now_str} KST] VCP 슈퍼 신고가 스캔 실행 시작 (대상 {SCAN_LIMIT}종목)...")
    uni = get_universe()
    now_ts = pd.Timestamp(now)
    
    scored = []
    def process(rec):
        try:
            df = fetch_bars(rec, now_ts)
            if df is None or len(df) < 60 or df.Volume.iloc[-1] <= 0:
                return None
            res = detect_vcp(df).iloc[-1]
            c = df.Close.iloc[-1]
            # 일일 환산 20일 평균 거래량 * 1.5
            daily_vol = df.Volume.groupby(df.index.date).sum()
            daily_v_ma20 = daily_vol.iloc[:-1].tail(20).mean() if len(daily_vol) > 1 else daily_vol.mean()
            req_vol_daily = int(daily_v_ma20 * 1.5) if pd.notna(daily_v_ma20) else 0

            return {
                "code": rec["code"], "name": rec["name"], "price": int(c),
                "eligible": bool(res["eligible"]), "score": float(res["score"]),
                "vcp_stage": str(res["vcp_stage"]), "vcp_ratio": float(res["vcp_ratio"]),
                "vol_spike": float(res["vol_spike"]), "vol_dryup": float(res["vol_dryup"]),
                "pivot_level": int(res["pivot_level"]), "extension_atr": float(res["extension_atr"]),
                "near_pivot": bool(c >= res["pivot_level"] * 0.96 and c < res["pivot_level"] and res["vcp_ratio"] <= 0.65),
                "req_vol_daily": req_vol_daily,
                "timestamp": now_str
            }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = pool.map(process, uni)
        for r in results:
            if r is not None:
                scored.append(r)
                
    matches = [r for r in scored if r["eligible"]]
    top5 = sorted(matches, key=lambda x: (-x["score"], x["code"]))[:TOP_N]
    watches = [r for r in scored if not r["eligible"] and r["near_pivot"]]
    watchlist = sorted(watches, key=lambda x: (x["vcp_ratio"], x["code"]))[:TOP_N]
    
    print(f"스캔 완료: 총 {len(scored)}개 분석 | 최종 후보 {len(top5)}개 | 관찰 종목 {len(watchlist)}개")
    
    # 디렉토리 생성
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # 1. 마크다운 보고서 작성
    md_lines = [
        f"# 🎯 VCP 슈퍼 신고가 스캐너 결과 ({now_str} KST)\n",
        f"- **스캔 시각**: {now_str} KST",
        f"- **유니버스 분석 완료**: {len(scored)}개 중소형주\n",
        "## 🏆 최종 후보 (VCP 수축 + 거래량 폭발 + 피봇 돌파 완료)",
    ]
    if top5:
        md_lines.append("| 종목(코드) | 현재가 | 피봇돌파선 | 거래량폭발 | VCP수축 | 점수 |")
        md_lines.append("|:---|:---:|:---:|:---:|:---:|:---:|")
        for r in top5:
            md_lines.append(f"| **{r['name']}** ({r['code']}) | {r['price']:,}원 | {r['pivot_level']:,}원 | **{r['vol_spike']}배** | {format_stage(r['vcp_stage'], r['vcp_ratio'])} | **{r['score']}점** |")
    else:
        md_lines.append("\n*현재 5중 안전 기준을 100% 충족한 최종 후보가 없습니다. (무리한 뇌동매매 방지)*\n")
        
    md_lines.append("\n## 👀 관찰 종목 (VCP 수축 완료, 피봇 4% 턱밑 대기)")
    if watchlist:
        md_lines.append("| 종목(코드) | 현재가 | 피봇돌파선 | 돌파필요 거래량(일일) | VCP수축 |")
        md_lines.append("|:---|:---:|:---:|:---:|:---:|")
        for r in watchlist:
            md_lines.append(f"| **{r['name']}** ({r['code']}) | {r['price']:,}원 | {r['pivot_level']:,}원 | **{format_vol(r['req_vol_daily'])}** | {format_stage(r['vcp_stage'], r['vcp_ratio'])} |")
    else:
        md_lines.append("\n*관찰 후보 없음*\n")
        
    md_lines.append("\n---\n*※ 본 스캐너는 완료봉 기준 연구용 지표이며, 주문/매수 API를 포함하지 않습니다.*")
    
    report_content = "\n".join(md_lines)
    
    # 최신 보고서 저장
    with open(OUTPUT_DIR / "latest_vcp.md", "w", encoding="utf-8") as f:
        f.write(report_content)
        
    # CSV 스냅샷 저장
    if top5 or watchlist:
        df_all = pd.DataFrame(top5 + watchlist)
        df_all.to_csv(OUTPUT_DIR / f"{timestamp_key}.csv", index=False, encoding="utf-8-sig")
        
    # 2. 루트 README.md 대시보드 갱신
    embed_md = generate_embed_markdown(now_str, len(scored), top5, watchlist)
    update_root_readme(embed_md)

    print(f"결과 파일 저장 완료: {OUTPUT_DIR / 'latest_vcp.md'}", flush=True)
    return len(top5), len(watchlist)

def format_vol(v: int) -> str:
    if not v or pd.isna(v):
        return "-"
    if v >= 100_000_000:
        return f"약 {v / 100_000_000:.1f}억 주"
    elif v >= 10_000:
        return f"약 {v / 10_000:.1f}만 주"
    return f"{v:,}주"

def format_stage(stage: str, ratio: float) -> str:
    short_stage = "3T" if "3T" in stage else ("2T" if "2T" in stage else stage)
    return f"{short_stage} ({ratio:.2f})"

def generate_embed_markdown(now_str: str, total_count: int, top5: list, watchlist: list) -> str:
    lines = [
        f"> **최근 스캔**: `{now_str} KST` | **유니버스 분석**: `{total_count}종목` | **최종 후보(돌파)**: `{len(top5)}건` | **관찰 종목(수축)**: `{len(watchlist)}건`\n",
        "### 🏆 최종 후보 (VCP 수축 + 거래량 폭발 + 피봇 돌파 완료)\n",
    ]
    if top5:
        lines.append("| 종목(코드) | 현재가 | 피봇돌파선 | 거래량폭발 | VCP수축 | 점수 |")
        lines.append("|:---|:---:|:---:|:---:|:---:|:---:|")
        for r in top5:
            lines.append(f"| **{r['name']}** ({r['code']}) | {r['price']:,}원 | {r['pivot_level']:,}원 | **{r['vol_spike']}배** | {format_stage(r['vcp_stage'], r['vcp_ratio'])} | **{r['score']}점** |")
    else:
        lines.append("*현재 5중 안전 기준을 100% 충족한 최종 후보가 없습니다. (무리한 뇌동매매 방지)*")
        
    lines.append("\n### 👀 관찰 종목 (VCP 수축 완료, 피봇 4% 턱밑 대기)\n")
    if watchlist:
        lines.append("| 종목(코드) | 현재가 | 피봇돌파선 | 돌파필요 거래량(일일) | VCP수축 |")
        lines.append("|:---|:---:|:---:|:---:|:---:|")
        for r in watchlist:
            lines.append(f"| **{r['name']}** ({r['code']}) | {r['price']:,}원 | {r['pivot_level']:,}원 | **{format_vol(r['req_vol_daily'])}** | {format_stage(r['vcp_stage'], r['vcp_ratio'])} |")
    else:
        lines.append("*관찰 후보 없음*")
        
    lines.append("\n---\n*※ 본 스캐너는 완료봉 기준 연구용 지표이며, 주문/매수 API를 일체 포함하지 않습니다.*")
    return "\n".join(lines)

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
