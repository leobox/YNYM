"""
Quant Data Collector & Pattern Scanner
---------------------------------------
GitHub Actions(1시간 주기) 및 모바일 수동(workflow_dispatch)으로 실행되는 실전 수집기입니다.
네이버/야후 공개 시세를 기반으로 150개 유니버스를 스캔하여:
1. '조건 충족' Top 5 후보 및 '다음 봉 확인' 관찰 종목을 도출
2. 상세 스냅샷(universe, scores, top5, watch)을 멱등적으로 영구 저장
3. GitHub 모바일 앱 첫 화면(README.md)에 최신 결과 표를 자동 갱신

[절대 안전 불변식]
- 실제 거래, 매수, 매도, 계좌 연동 로직은 작성하지 않으며 일체 호출하지 않습니다.
"""

import os
import sys
import time
import json
import importlib
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
README_PATH = BASE_DIR / "README.md"

SCAN_LIMIT = 150
TOP_N = 5
WORKERS = 6
MCAP_MIN, MCAP_MAX = 100_000_000_000, 5_000_000_000_000
MIN_PRICE = 2000
VOLUME_WEIGHT = 0.2
MAX_EXTENSION_ATR = None


def get_current_kst() -> datetime:
    return datetime.now(KST)


def get_json(url: str, params: Optional[Dict[str, Any]] = None, max_retries: int = 3, timeout: int = 10) -> Any:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    backoff = 1.0
    last_err = None

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2.0

    raise RuntimeError(f"HTTP 요청 실패: {url} | {str(last_err)[:100]}")


def get_universe() -> List[Dict[str, Any]]:
    """공식 상장사 화이트리스트 기반 중소형주(1천억~5조, 거래대금 상위 150개) 추출"""
    try:
        fdr = importlib.import_module("FinanceDataReader")
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "finance-datareader"])
        fdr = importlib.import_module("FinanceDataReader")

    listing = fdr.StockListing("KRX")
    valid = set(listing.loc[listing.Market.isin(["KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"]), "Code"].astype(str))
    if not valid:
        raise RuntimeError("상장 종목 목록이 비어 있습니다.")

    rows = []
    number = lambda x: pd.to_numeric(str(x).replace(",", ""), errors="coerce")

    for market in ("KOSPI", "KOSDAQ"):
        for page in range(1, 31):
            try:
                stocks = get_json(
                    f"https://m.stock.naver.com/api/stocks/marketValue/{market}",
                    {"page": page, "pageSize": 100},
                    timeout=10,
                ).get("stocks", [])
            except Exception as e:
                print(f"[경고] {market} 유니버스 {page}페이지 조회 실패: {e}")
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
                        "code": code,
                        "name": name,
                        "market": market,
                        "price": price,
                        "market_cap": cap,
                        "quote_time": x.get("localTradedAt"),
                        "amount": number(x["accumulatedTradingValue"]),
                    })

            if number(stocks[-1]["marketValue"]) * 1e8 < MCAP_MIN:
                break

    if not rows:
        raise RuntimeError("조회 가능한 중소형주 유니버스가 없습니다.")

    df_u = (
        pd.DataFrame(rows)
        .dropna(subset=["amount"])
        .sort_values("amount", ascending=False)
        .drop_duplicates("code")
        .head(SCAN_LIMIT)
    )
    return df_u.to_dict("records")


def completed_bars(item: Dict[str, Any], now: pd.Timestamp) -> pd.DataFrame:
    df = pd.DataFrame(
        item["indicators"]["quote"][0],
        index=pd.to_datetime(item["timestamp"], unit="s", utc=True).tz_convert("Asia/Seoul"),
    )
    df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df = df[~df.index.duplicated()].sort_index()
    df = df[(df.index.minute == 0) & (df.index.second == 0) & (df.index.hour >= 9) & (df.index.hour <= 15)]
    # 15:00 더미 봉 제외
    df = df[~((df.index.hour == 15) & (df.Volume == 0) & (df.High == df.Low))]
    ends = df.index + pd.to_timedelta(np.where(df.index.hour == 15, 30, 60), unit="m")
    return df[ends <= now]


def fetch_bars(rec: Dict[str, Any], now: pd.Timestamp) -> pd.DataFrame:
    symbol = rec["code"] + (".KQ" if rec["market"] == "KOSDAQ" else ".KS")
    payload = get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        {"range": "60d", "interval": "60m"},
        timeout=10,
    )["chart"]
    if not payload["result"]:
        raise RuntimeError(str(payload.get("error")))
    return completed_bars(payload["result"][0], now)


def _atr(df: pd.DataFrame) -> pd.Series:
    prev = df.Close.shift(1)
    tr = pd.concat([df.High - df.Low, (df.High - prev).abs(), (df.Low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean().replace(0, np.nan)


def hourly_pattern(df: pd.DataFrame, require_volume: bool = True, volume_weight: float = 0.2, max_extension_atr: Optional[float] = None) -> pd.DataFrame:
    c, v = df["Close"], df["Volume"]
    f, m = c.rolling(12).mean(), c.rolling(26).mean()
    atr = _atr(df)
    gap = (f - m) / atr
    fs, ms = (f - f.shift(3)) / atr, (m - m.shift(3)) / atr

    minutes = pd.Series(np.where(df.index.hour == 15, 30, 60), index=df.index)
    rate = v / minutes
    vr = rate.rolling(3).mean() / rate.shift(3).rolling(20).mean().replace(0, np.nan)

    clip = lambda x: x.clip(0, 100)
    proximity = clip(100 - gap.abs() * 40)
    direction = clip(50 + fs * 60 + ms * 30)
    volume = clip((vr - 0.7) / 1.8 * 100)
    room = clip(100 - ((c - m) / atr - 1).clip(lower=0) * 25)
    contraction = clip(50 + (gap.shift(6).abs() - gap.abs()) * 30)
    rebound = (proximity + direction + volume + room + contraction) / 5

    prior_width = (df.High.shift(1).rolling(6).max() - df.Low.shift(1).rolling(6).min()) / atr
    base = clip(100 - prior_width * 15)
    breakout = clip(50 + (c - df.High.shift(1).rolling(6).max()) / atr * 50)
    expansion = (base + breakout + direction + volume + room) / 5

    eligible = (fs > 0) & (ms > ms.shift(3)) & (c > f) & (c > m)
    if require_volume:
        eligible &= vr >= 1
    eligible &= gap.shift(1).rolling(20).min() < 0
    eligible &= c >= 2000

    score = np.maximum(rebound, expansion)
    if volume_weight != 0.2:
        score = (score - 0.2 * volume) * (1 - volume_weight) / 0.8 + volume_weight * volume
    if max_extension_atr is not None:
        eligible &= (c - m) / atr <= max_extension_atr

    out = pd.DataFrame(
        {
            "score": score,
            "volume_ratio": vr,
            "pattern": np.where(rebound >= expansion, "rebound", "base_breakout"),
            "above60": c > c.rolling(60).mean(),
            "eligible": eligible,
        },
        index=df.index,
    )
    out.loc[~np.isfinite(out.score), "eligible"] = False
    return out


def confirmed_breakout(df: pd.DataFrame) -> pd.DataFrame:
    c = df.Close
    fast = c.rolling(12).mean()
    medium = c.rolling(26).mean()
    trend = (fast > fast.shift(3)) & (medium > medium.shift(3)) & (c > fast) & (c > medium)
    level = df.High.shift(1).rolling(6).max()
    location = (c - df.Low) / (df.High - df.Low).replace(0, np.nan)
    extension = (c - medium) / _atr(df)
    amount = c * df.Volume
    typical = amount.groupby(df.index.hour).transform(lambda x: x.shift(1).rolling(20, min_periods=5).median())
    ratio = amount / typical.replace(0, np.nan)

    breakout = trend & (c > level) & (c > df.Open) & (location >= 0.7) & (extension <= 4)
    liquid = breakout & (amount >= 1e9)
    trigger = liquid & (amount >= typical * 2)
    hold_price = trend & (df.Low >= level.shift(1)) & (c >= c.shift(1)) & (extension <= 4)
    held = trigger.shift(1, fill_value=False) & hold_price

    return pd.DataFrame(
        {
            "hold": held,
            "previous_breakout": breakout.shift(1, fill_value=False),
            "previous_liquid": liquid.shift(1, fill_value=False),
            "previous_trigger": trigger.shift(1, fill_value=False),
            "waiting": trigger,
            "hold_price": hold_price,
            "current_amount": amount / 1e8,
            "current_ratio": ratio,
            "current_level": level,
            "trigger_amount": amount.shift(1) / 1e8,
            "trigger_ratio": ratio.shift(1),
            "breakout_level": level.shift(1),
        },
        index=df.index,
    )


def valid_ohlcv(df: pd.DataFrame) -> bool:
    if df.empty or df.index.has_duplicates or not df.index.is_monotonic_increasing:
        return False
    if not np.isfinite(df[["Open", "High", "Low", "Close", "Volume"]].to_numpy(dtype=float)).all():
        return False
    bad = (
        (df[["Open", "High", "Low", "Close"]] <= 0).any(axis=1)
        | (df.Volume < 0)
        | (df.High < df[["Open", "Close"]].max(axis=1))
        | (df.Low > df[["Open", "Close"]].min(axis=1))
    )
    return not bool(bad.any())


def score_stock(rec: Dict[str, Any], now: pd.Timestamp) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        df = fetch_bars(rec, now)
        if not valid_ohlcv(df):
            return None, "가격/거래량 정합성 오류"
        if len(df) < 120 or df.Volume.iloc[-1] <= 0 or df.Close.iloc[-1] < MIN_PRICE:
            return None, "봉 부족/거래정지/가격조건 미달"
        if now - df.index[-1] > pd.Timedelta(days=7):
            return None, "최근 7일 데이터 없음"

        s = hourly_pattern(df, volume_weight=VOLUME_WEIGHT, max_extension_atr=MAX_EXTENSION_ATR).iloc[-1]
        conf = confirmed_breakout(df).iloc[-1]
        matched = bool(s.eligible and conf.hold)
        if not np.isfinite(s.score):
            return None, "지표 계산 불가"

        c = df.Close
        fast, medium = c.rolling(12).mean(), c.rolling(26).mean()

        return {
            "종목": rec["name"],
            "코드": rec["code"],
            "시장": rec["market"],
            "구분": "일치" if matched else "제외",
            "점수": float(s.score),
            "이격ATR": round(float((c.iloc[-1] - medium.iloc[-1]) / _atr(df).iloc[-1]), 2),
            "돌파선": round(float(conf.breakout_level), 2),
            "돌파봉종가": float(c.iloc[-2]),
            "돌파봉대금_억": round(float(conf.trigger_amount), 1),
            "돌파봉대금배수": round(float(conf.trigger_ratio), 2),
            "기준봉(KST)": df.index[-1].strftime("%m-%d %H:%M"),
            "돌파봉(KST)": df.index[-2].strftime("%m-%d %H:%M"),
            "_base": bool(s.eligible),
            "_match": matched,
            "_waiting": bool(s.eligible and conf.waiting),
            "_current_amount": float(conf.current_amount),
            "_current_ratio": float(conf.current_ratio),
            "_current_level": float(conf.current_level),
            "_current_close": float(c.iloc[-1]),
            "_bar_time": df.index[-1].isoformat(),
        }, None
    except Exception as e:
        return None, str(e)[:100]


def naver_quote(code: str) -> Dict[str, Any]:
    number = lambda x: pd.to_numeric(str(x).replace(",", ""), errors="coerce")
    res = {"현재가": np.nan, "당일등락률": np.nan, "당일고가": np.nan, "당일누적대금": "미제공", "시세시각": "시각 미제공"}
    try:
        q = get_json(f"https://m.stock.naver.com/api/stock/{code}/basic", timeout=10)
        p = number(q.get("closePrice"))
        if np.isfinite(p) and p > 0:
            res["현재가"] = float(p)
            res["당일등락률"] = number(q.get("fluctuationsRatio"))
            res["시세시각"] = q.get("localTradedAt") or "시각 미제공"
    except Exception:
        pass

    try:
        info = get_json(f"https://m.stock.naver.com/api/stock/{code}/integration", timeout=10)
        vals = {x["code"]: x.get("value") for x in info.get("totalInfos", [])}
        res["당일고가"] = number(vals.get("highPrice"))
        res["당일누적대금"] = vals.get("accumulatedTradingValue") or "미제공"
    except Exception:
        pass

    return res


def attach_quotes(tables: List[pd.DataFrame]) -> List[pd.DataFrame]:
    codes = sorted({str(c) for t in tables if not t.empty for c in t["코드"]})
    quotes = {c: naver_quote(c) for c in codes}
    out = []
    for t in tables:
        if t.empty:
            out.append(t)
            continue
        extra = pd.DataFrame([quotes[str(c)] for c in t["코드"]], index=t.index)
        t_new = pd.concat([t, extra], axis=1)
        out.append(t_new)
    return out


def build_results(rows: List[Dict[str, Any]]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if not rows:
        return pd.DataFrame(), pd.DataFrame()

    df_rows = pd.DataFrame(rows)
    # 1. Top 5 최종 조건 충족
    top = (
        df_rows[df_rows["_match"]]
        .sort_values(["점수", "코드"], ascending=[False, True])
        .head(TOP_N)
        .reset_index(drop=True)
    )
    if not top.empty:
        top["점수"] = top["점수"].round(1)
        top.insert(0, "순위", range(1, len(top) + 1))

    # 2. Watch 다음 봉 지지 대기
    watch_rows = []
    for r in rows:
        if r["_match"] or not r["_base"]:
            continue
        if r["_waiting"]:
            watch_rows.append({
                "종목": r["종목"],
                "코드": r["코드"],
                "단계": "돌파·대금 충족 (다음 봉 확인 대기)",
                "점수": round(r["점수"], 1),
                "돌파선": round(r["_current_level"], 2),
                "돌파봉종가": r["_current_close"],
                "돌파봉대금_억": round(r["_current_amount"], 1),
                "동시간배수": round(r["_current_ratio"], 2),
                "돌파봉(KST)": r["기준봉(KST)"],
                "기준봉(KST)": r["기준봉(KST)"],
            })
    watch = pd.DataFrame(watch_rows)

    return top, watch


def render_markdown_table(top: pd.DataFrame, watch: pd.DataFrame, now_str: str, scan_count: int) -> str:
    """GitHub 모바일 앱 및 웹 첫 화면(README.md)에 표시될 깔끔한 마크다운 리포트"""
    lines = [
        "# ⏱️ Quant Data Collector & Scanner",
        "",
        f"> **최근 스캔 시각**: `{now_str} KST` | **대상 유니버스**: `{scan_count}종목` | **조건 충족**: `{len(top)}건` | **관찰**: `{len(watch)}건`",
        "",
        "이 페이지는 GitHub Actions(1시간 주기)를 통해 한국 정규장 시간에 자동 스캔·갱신됩니다.",
        "스마트폰 GitHub 앱 또는 모바일 웹에서 언제든지 최신 후보를 확인하실 수 있습니다.",
        "",
        "---",
        "",
        "## 🟢 조건 충족 종목 (최대 5개)",
        "",
    ]

    if top.empty:
        lines.append("*현재 60분봉 돌파 후 지지 조건을 충족한 종목이 없습니다.* (0개가 정상입니다)\n")
    else:
        lines.append("| 순위 | 종목명 (코드) | 현재가 | 돌파선 대비 | 돌파봉종가 대비 | 당일등락 | 당일고가 | 거래대금 | 기준봉 |")
        lines.append("|:---:|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
        for _, r in top.iterrows():
            p = r.get("현재가")
            price_str = f"**{p:,.0f}원**" if pd.notna(p) else "—"
            level = r.get("돌파선")
            c_prev = r.get("돌파봉종가")
            gap_lvl = f"{(p/level-1)*100:+.2f}%" if pd.notna(p) and pd.notna(level) and level > 0 else "—"
            gap_c = f"{(p/c_prev-1)*100:+.2f}%" if pd.notna(p) and pd.notna(c_prev) and c_prev > 0 else "—"
            chg = f"{r.get('당일등락률'):+.2f}%" if pd.notna(r.get("당일등락률")) else "—"
            high = f"{r.get('당일고가'):,.0f}원" if pd.notna(r.get("당일고가")) else "—"
            amt = f"{r.get('돌파봉대금_억'):,.1f}억 ({r.get('돌파봉대금배수'):,.1f}배)"
            lines.append(f"| {r['순위']} | **{r['종목']}** ({r['코드']}) | {price_str} | {gap_lvl} | {gap_c} | {chg} | {high} | {amt} | {r['기준봉(KST)']} |")
        lines.append("")

    lines.extend([
        "## 🟡 다음 봉 확인 관찰 종목",
        "",
        "> 기본 패턴 + 돌파 + 동시간 대금 2배를 통과하고, **다음 60분봉 지지 확인만 남은 종목**입니다.",
        "",
    ])

    if watch.empty:
        lines.append("*현재 관찰 대기 종목이 없습니다.*\n")
    else:
        lines.append("| 종목명 (코드) | 현재가 | 돌파선 | 돌파봉종가 | 돌파대금 | 당일등락 | 돌파봉시각 |")
        lines.append("|:---|:---:|:---:|:---:|:---:|:---:|:---:|")
        for _, r in watch.iterrows():
            p = r.get("현재가")
            price_str = f"**{p:,.0f}원**" if pd.notna(p) else "—"
            chg = f"{r.get('당일등락률'):+.2f}%" if pd.notna(r.get("당일등락률")) else "—"
            lines.append(f"| **{r['종목']}** ({r['코드']}) | {price_str} | {r['돌파선']:,.0f}원 | {r['돌파봉종가']:,.0f}원 | {r['돌파봉대금_억']:,.1f}억 ({r['동시간배수']:,.1f}배) | {chg} | {r['돌파봉(KST)']} |")
        lines.append("")

    lines.extend([
        "---",
        "",
        "### ⚠️ 알림 & 안전 원칙",
        "- **거래 주문 로직 없음**: 본 수집기는 순수 시세 관측·패턴 분석 도구이며, 실제 매수/매도는 사용자가 MTS에서 직접 판단합니다.",
        "- **과거 스냅샷 보존**: 모든 스캔의 원본 데이터(universe, scores, top5, watch)는 `data/{YYYYMMDD}/`에 영구 보존됩니다.",
        "",
    ])

    return "\n".join(lines)


def run_collector():
    now_dt = get_current_kst()
    now_ts = pd.Timestamp.now(tz="Asia/Seoul")
    stamp_day = now_dt.strftime("%Y%m%d")
    stamp_time = now_dt.strftime("%H%M")
    now_str = now_dt.strftime("%Y-%m-%d %H:%M")

    print(f"=== [Quant Collector] 150종목 스캔 시작: {now_str} KST ===")
    stocks = get_universe()
    print(f"-> 유니버스 {len(stocks)}종목 추출 완료. 60분봉 수집 및 채점 진행 중...")

    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for row, err in pool.map(lambda s: score_stock(s, now_ts), stocks):
            if row is not None:
                rows.append(row)
            else:
                errors.append(err)

    print(f"-> 채점 완료: 성공 {len(rows)} / 제외·오류 {len(errors)}")

    top, watch = build_results(rows)
    top_quoted, watch_quoted = attach_quotes([top, watch])

    # 1. 스냅샷 파일 멱등 저장
    day_dir = DATA_DIR / stamp_day
    day_dir.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(stocks).to_csv(day_dir / f"{stamp_time}_universe.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(rows).to_csv(day_dir / f"{stamp_time}_scores.csv", index=False, encoding="utf-8-sig")
    top_quoted.to_csv(day_dir / f"{stamp_time}_top5.csv", index=False, encoding="utf-8-sig")
    watch_quoted.to_csv(day_dir / f"{stamp_time}_watch.csv", index=False, encoding="utf-8-sig")

    # 2. README.md에 최신 표 갱신 (모바일 앱 메인 뷰)
    md_content = render_markdown_table(top_quoted, watch_quoted, now_str, len(stocks))
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"=== [Quant Collector] 스캔 및 저장 완료 ===")
    print(f"-> Top 5 조건충족: {len(top_quoted)}건 | 관찰 대기: {len(watch_quoted)}건")
    print(f"-> 스냅샷 보존: {day_dir}/{stamp_time}_*.csv")
    print(f"-> README.md 모바일 표 갱신 완료\n")


if __name__ == "__main__":
    run_collector()
