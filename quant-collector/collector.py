"""
Quant Data Collector & Forward Labeling Engine
---------------------------------------------
GitHub Actions(장중 09:30~16:30, 30분 주기) 및 모바일 수동(workflow_dispatch)으로 실행되는 실전 수집기 & 전진 라벨러입니다.
1. 네이버 150개 유니버스 + 기존 추적 중인 pending 종목의 60분봉 수집
2. '조건 충족' 및 '관찰' 후보 포착 및 특징값 고정 (Snapshot)
3. 향후 3~5거래일 완료봉 추적을 통한 다중 목표(+3/5/7/10%)/손절(-3/5%) 선접촉 라벨링 확정
4. 2차 판독기(위험 필터 / 메타 모델) 학습용 원본 데이터셋 자동 축적
5. GitHub 모바일 앱(README.md)에 실시간 스캔 및 추적 진행 현황 자동 갱신

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
from typing import Dict, Any, List, Optional, Tuple, Set

import numpy as np
import pandas as pd
import requests

from tracker import SignalTracker
from exit_engine import evaluate_position_exit, ExitSignal

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
README_PATH = BASE_DIR / "README.md"
STRATEGY_VERSION = "algorithm260917_v1"

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


def fetch_bars(code: str, market: str, now: pd.Timestamp) -> pd.DataFrame:
    symbol = code + (".KQ" if market == "KOSDAQ" else ".KS")
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


def score_stock(rec: Dict[str, Any], now: pd.Timestamp) -> Tuple[Optional[Dict[str, Any]], Optional[pd.DataFrame], Optional[str]]:
    try:
        df = fetch_bars(rec["code"], rec["market"], now)
        if not valid_ohlcv(df):
            return None, None, "가격/거래량 정합성 오류"
        if len(df) < 120 or df.Volume.iloc[-1] <= 0 or df.Close.iloc[-1] < MIN_PRICE:
            return None, None, "봉 부족/거래정지/가격조건 미달"
        if now - df.index[-1] > pd.Timedelta(days=7):
            return None, None, "최근 7일 데이터 없음"

        s = hourly_pattern(df, volume_weight=VOLUME_WEIGHT, max_extension_atr=MAX_EXTENSION_ATR).iloc[-1]
        conf = confirmed_breakout(df).iloc[-1]
        matched = bool(s.eligible and conf.hold)
        if not np.isfinite(s.score):
            return None, None, "지표 계산 불가"

        c = df.Close
        fast, medium = c.rolling(12).mean(), c.rolling(26).mean()
        atr_val = _atr(df).iloc[-1]

        row = {
            "종목": rec["name"],
            "코드": rec["code"],
            "시장": rec["market"],
            "구분": "일치" if matched else "제외",
            "점수": float(s.score),
            "이격ATR": round(float((c.iloc[-1] - medium.iloc[-1]) / atr_val), 2) if pd.notna(atr_val) and atr_val > 0 else np.nan,
            "돌파선": round(float(conf.breakout_level), 2),
            "돌파봉종가": float(c.iloc[-2]),
            "돌파봉대금_억": round(float(conf.trigger_amount), 1),
            "돌파봉대금배수": round(float(conf.trigger_ratio), 2),
            "기준봉(KST)": df.index[-1].strftime("%m-%d %H:%M"),
            "돌파봉(KST)": df.index[-2].strftime("%m-%d %H:%M"),
            "_bar_time_full": df.index[-1].strftime("%Y-%m-%d %H:%M"),
            "_base": bool(s.eligible),
            "_match": matched,
            "_waiting": bool(s.eligible and conf.waiting),
            "_current_amount": float(conf.current_amount),
            "_current_ratio": float(conf.current_ratio),
            "_current_level": float(conf.current_level),
            "_current_close": float(c.iloc[-1]),
            "_bar_time": df.index[-1].isoformat(),
        }

        # 2차 판독기용 세부 특징값 딕셔너리
        features = {
            "score": float(s.score),
            "pattern_type": str(s.pattern),
            "volume_ratio": float(s.volume_ratio),
            "above60": bool(s.above60),
            "extension_atr": float(row["이격ATR"]),
            "trigger_amount_e8": float(conf.trigger_amount),
            "trigger_ratio": float(conf.trigger_ratio),
            "breakout_level": float(conf.breakout_level),
            "trigger_close": float(c.iloc[-2]),
            "current_close": float(c.iloc[-1]),
            "atr_14": float(atr_val) if pd.notna(atr_val) else 0.0,
            "sma12": float(fast.iloc[-1]),
            "sma26": float(medium.iloc[-1]),
        }
        row["_features"] = features

        return row, df, None
    except Exception as e:
        return None, None, str(e)[:100]


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
                "시장": r.get("시장", "KOSPI"),
                "_bar_time_full": r.get("_bar_time_full", f"2026-{r['기준봉(KST)']}"),
                "_features": r.get("_features", {}),
            })
    watch = pd.DataFrame(watch_rows)

    return top, watch


def render_markdown_dashboard(
    top: pd.DataFrame,
    watch: pd.DataFrame,
    exit_evaluations: List[Dict[str, Any]],
    tracker_stats: Dict[str, Any],
    pending_list: List[Dict[str, Any]],
    now_str: str,
    scan_count: int,
) -> str:
    """GitHub 모바일 앱 및 웹 첫 화면(README.md)에 표시될 종합 대시보드 리포트"""
    sell_alerts = [e for e in exit_evaluations if e["action_type"] in ("TAKE_PROFIT", "CUT_LOSS")]

    lines = [
        "# ⏱️ Quant Pattern Scanner & Position Exit Monitor",
        "",
        f"> **최근 스캔**: `{now_str} KST` | **유니버스**: `{scan_count}종목` | **조건 충족**: `{len(top)}건` | **관찰**: `{len(watch)}건` | **보유 추적**: `{len(pending_list)}건`",
        "",
        "한국 정규장 30분 주기(09:30~16:30)로 실행되며, **매수 진입 포지션에 대한 실시간 매도·청산 신호**와 **신규 후보**를 동시에 모니터링합니다.",
        "",
        "---",
        "",
    ]

    # [최우선 알림] 긴급 매도/청산 신호 발생 시 상단에 강조 표시
    if sell_alerts:
        lines.extend([
            "## 🚨 [긴급] 실시간 매도·청산 권고 신호 발생!",
            "",
            "> **조건 충족으로 매수했던 종목 중 청산 조건(익절/손절/돌파선붕괴)이 감지되었습니다. MTS에서 확인 후 대응하세요.**",
            "",
            "| 구분 | 종목명 (코드) | 진입가 | 현재가 (수익률) | 매도 판정 | 사유 및 대응 가이드 |",
            "|:---:|:---|:---:|:---:|:---:|:---|",
        ])
        for a in sell_alerts:
            tag = "🔴 익절" if a["action_type"] == "TAKE_PROFIT" else "⚠️ 손절·탈출"
            lines.append(
                f"| **{tag}** | **{a['name']}** ({a['code']}) | {a['entry_price']:,.0f}원 | **{a['current_price']:,.0f}원 ({a['pnl_pct']:+.2f}%)** | `{a['decision']}` | {a['reason']} |"
            )
        lines.extend(["", "---", ""])

    # 1. 보유 포지션 매도 판정 모니터링 표
    lines.extend([
        "## 💼 보유 포지션 실시간 매도 모니터링 (가상 매수 100만원 가정)",
        "",
        "> 조건 충족 시 100만원 매수 진입했다고 가정한 종목들의 **봉 단위 청산 판정 상태**입니다.",
        "",
    ])

    if not exit_evaluations:
        lines.append("*현재 보유 중인 가상 포지션이 없습니다.*\n")
    else:
        lines.append("| 종목명 (코드) | 진입가 | 현재가 (손익) | 최고수익 | 돌파선 | **매도 판정** | **대응 가이드** |")
        lines.append("|:---|:---:|:---:|:---:|:---:|:---:|:---|")
        for e in exit_evaluations:
            status_icon = "🟢 HOLD"
            if e["decision"] == ExitSignal.CAUTION:
                status_icon = "🟡 CAUTION"
            elif e["decision"] == ExitSignal.BREAKOUT_AMBIGUOUS:
                status_icon = "🟠 RECHECK (다음 봉 확인)"
            elif e["action_type"] == "TAKE_PROFIT":
                status_icon = "🔴 SELL (익절)"
            elif e["action_type"] == "CUT_LOSS":
                status_icon = "🔴 SELL (손절)"

            lines.append(
                f"| **{e['name']}** ({e['code']}) | {e['entry_price']:,.0f}원 | {e['current_price']:,.0f}원 (**{e['pnl_pct']:+.2f}%**) | +{e['max_gain_pct']:.1f}% | {e['breakout_level']:,.0f}원 | **{status_icon}** | {e['reason']} |"
            )
        lines.append("")

    lines.extend([
        "---",
        "",
        "## 🟢 신규 조건 충족 종목 (신규 매수 후보)",
        "",
    ])

    if top.empty:
        lines.append("*현재 신규로 돌파 후 지지 조건을 충족한 종목이 없습니다.* (0개가 정상입니다)\n")
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

    # 3. 실시간 추적 중인 Pending 신호 목록
    lines.extend([
        "---",
        "",
        "## 🔵 실시간 전진 추적 중인 신호 (Pending)",
        "",
        f"> 신호 발생 후 현재까지의 가격 흐름을 추적 중인 목록입니다. (총 **{len(pending_list)}건**)",
        "",
    ])

    if not pending_list:
        lines.append("*현재 추적 중인 활성 신호가 없습니다.*\n")
    else:
        lines.append("| 종목명 (코드) | 신호발생시각 | 진입기준가 | 최고가 | 최저가 | 경과일수 | +10%목표가 | -5%손절가 |")
        lines.append("|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
        for sig in pending_list[-10:]:  # 최근 10개 표시
            name = sig["name"]
            code = sig["code"]
            t_str = sig["signal_time_kst"]
            e_p = sig["entry_reference_price"]
            h_p = sig["highest_seen"]
            l_p = sig["lowest_seen"]
            days = sig["trading_days_observed"]
            tgt_10 = sig["targets"].get("tgt_10", e_p * 1.1)
            stop_5 = sig["stops"].get("stop_5", e_p * 0.95)

            h_gap = f"{(h_p/e_p - 1)*100:+.2f}%"
            l_gap = f"{(l_p/e_p - 1)*100:+.2f}%"
            lines.append(f"| **{name}** ({code}) | {t_str} | {e_p:,.0f}원 | {h_p:,.0f}원 ({h_gap}) | {l_p:,.0f}원 ({l_gap}) | {days}일차 | {tgt_10:,.0f}원 | {stop_5:,.0f}원 |")
        lines.append("")

    # 4. 누적 통계 박스
    tot_res = tracker_stats["total_resolved"]
    win_r = tracker_stats["win_rate"]
    win_str = f"**{win_r}%**" if win_r is not None else "데이터 축적 중"

    lines.extend([
        "---",
        "",
        "## 📈 누적 전진 검증 성과 (+10% 익절 vs -5% 손절 / 5일 기준)",
        "",
        f"- **완료된 평가 표본 수**: `{tot_res}건` (목표: 독립 표본 300건 이상)",
        f"- **TARGET_FIRST (익절 선접촉)**: `{tracker_stats['target_first']}건`",
        f"- **STOP_FIRST (손절 선접촉)**: `{tracker_stats['stop_first']}건`",
        f"- **TIMEOUT (만기 종료)**: `{tracker_stats['timeout']}건`",
        f"- **익절 성공률 (Win Rate)**: {win_str}",
        "",
        "---",
        "",
        "### 🔬 2차 판독기 (Meta-Classifier) 파이프라인 안내",
        "- 본 수집기에서 생성되는 `data/resolved_signals.jsonl`은 향후 로지스틱 회귀 및 Gradient Boosting 기반의 **2차 위험 필터 모델 학습**에 사용됩니다.",
        "- 목표: 전진 검증에서 손절률의 95% 신뢰 상한을 최소화하고 위험 후보를 사전에 '판단 보류'로 필터링.",
        "",
    ])

    return "\n".join(lines)


def run_collector():
    now_dt = get_current_kst()
    now_ts = pd.Timestamp.now(tz="Asia/Seoul")
    stamp_day = now_dt.strftime("%Y%m%d")
    stamp_time = now_dt.strftime("%H%M")
    now_str = now_dt.strftime("%Y-%m-%d %H:%M")

    print(f"=== [Quant Collector] 전진 라벨러 & 스캔 시작: {now_str} KST ===")
    tracker = SignalTracker(DATA_DIR)

    # 1. 대상 유니버스 150개 확보
    universe_stocks = get_universe()
    universe_codes = {s["code"] for s in universe_stocks}

    # 2. 유니버스에서 빠졌더라도 현재 추적 중인 Pending 종목 코드도 함께 수집 대상에 포함
    tracked_codes = tracker.get_tracked_codes()
    missing_tracked = tracked_codes - universe_codes
    all_scan_targets = list(universe_stocks)

    if missing_tracked:
        print(f"-> 유니버스 외 추적 중인 Pending 종목 {len(missing_tracked)}개 추가 수집 목록 포함")
        for sig in tracker.pending_signals.values():
            if sig["code"] in missing_tracked:
                all_scan_targets.append({
                    "code": sig["code"],
                    "name": sig["name"],
                    "market": sig["market"],
                })

    print(f"-> 총 {len(all_scan_targets)}개 대상 60분봉 수집 및 패턴 채점 진행 중...")

    rows = []
    errors = []
    collected_candles_by_code = {}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = pool.map(lambda s: (s, score_stock(s, now_ts)), all_scan_targets)
        for rec, (row, df_bars, err) in results:
            if row is not None and df_bars is not None:
                rows.append(row)
                # 최근 20개 완료봉을 추적용으로 보관
                candle_list = []
                for idx, b in df_bars.tail(20).iterrows():
                    candle_list.append({
                        "time_kst": idx.strftime("%Y-%m-%d %H:%M"),
                        "open": float(b.Open),
                        "high": float(b.High),
                        "low": float(b.Low),
                        "close": float(b.Close),
                        "volume": int(b.Volume),
                    })
                collected_candles_by_code[rec["code"]] = candle_list
            else:
                errors.append(err)

    print(f"-> 채점 완료: 성공 {len(rows)} / 제외·오류 {len(errors)}")

    # 3. Top 5 및 Watch 종목 도출
    top, watch = build_results(rows)
    top_quoted, watch_quoted = attach_quotes([top, watch])

    # 4. 신규 신호 등록 (Top 5 조건 충족 및 Watch 돌파 종목)
    registered_count = 0
    # A. Top 5 등록
    for _, r in top_quoted.iterrows():
        sig_id = tracker.register_signal(
            strategy_version=STRATEGY_VERSION,
            code=r["코드"],
            name=r["종목"],
            market=r["시장"],
            bar_time_kst=r.get("_bar_time_full", r["기준봉(KST)"]),
            features=r.get("_features", {}),
            entry_reference_price=float(r["현재가"] if pd.notna(r.get("현재가")) else r["돌파봉종가"]),
        )
        if sig_id:
            registered_count += 1

    # B. Watch 등록 (다음 봉 마감 대기지만 돌파 당시의 특징값 고정)
    for _, r in watch_quoted.iterrows():
        sig_id = tracker.register_signal(
            strategy_version=STRATEGY_VERSION,
            code=r["코드"],
            name=r["종목"],
            market=r.get("시장", "KOSPI"),
            bar_time_kst=r.get("_bar_time_full", r["기준봉(KST)"]),
            features=r.get("_features", {}),
            entry_reference_price=float(r["돌파봉종가"]),
        )
        if sig_id:
            registered_count += 1

    print(f"-> 신규 추적 등록 신호: {registered_count}건 (현재 총 pending: {len(tracker.pending_signals)}건)")

    # 5. 기존 Pending 신호들의 미래봉 접촉 판정 및 라벨 확정
    newly_resolved = tracker.update_with_bars(collected_candles_by_code, now_dt)
    if newly_resolved:
        print(f"-> [라벨 확정] 종결 신호: {len(newly_resolved)}건!")
        for res in newly_resolved:
            print(f"   [{res['code']} {res['name']}] 확정 라벨: {res['evaluations']['h5_t10_s5']['status']}")

    # 6. 스냅샷 파일 멱등 저장
    day_dir = DATA_DIR / stamp_day
    day_dir.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(universe_stocks).to_csv(day_dir / f"{stamp_time}_universe.csv", index=False, encoding="utf-8-sig")
    # _features 등 객체 컬럼 정리 후 CSV 저장
    df_scores = pd.DataFrame(rows).drop(columns=["_features"], errors="ignore")
    df_scores.to_csv(day_dir / f"{stamp_time}_scores.csv", index=False, encoding="utf-8-sig")
    top_clean = top_quoted.drop(columns=["_features"], errors="ignore")
    watch_clean = watch_quoted.drop(columns=["_features"], errors="ignore")
    top_clean.to_csv(day_dir / f"{stamp_time}_top5.csv", index=False, encoding="utf-8-sig")
    watch_clean.to_csv(day_dir / f"{stamp_time}_watch.csv", index=False, encoding="utf-8-sig")

    # 7. 실시간 보유 포지션 매도·청산 신호 평가
    exit_evaluations = []
    for sig_id, pos in tracker.pending_signals.items():
        c_code = pos["code"]
        candles = collected_candles_by_code.get(c_code, [])
        cur_quote = naver_quote(c_code)
        cur_p = cur_quote.get("현재가")
        if not pd.notna(cur_p) or cur_p <= 0:
            cur_p = candles[-1]["close"] if candles else pos["entry_reference_price"]
        eval_res = evaluate_position_exit(pos, candles, float(cur_p), now_str)
        exit_evaluations.append(eval_res)

        if eval_res["action_type"] in ("TAKE_PROFIT", "CUT_LOSS"):
            print(f"-> [매도 권고] {eval_res['name']}({c_code}): {eval_res['decision']} | {eval_res['reason']}")

    # 8. README.md 모바일 대시보드 갱신
    tracker_stats = tracker.get_summary_stats()
    pending_list = list(tracker.pending_signals.values())
    md_dashboard = render_markdown_dashboard(
        top_clean,
        watch_clean,
        exit_evaluations,
        tracker_stats,
        pending_list,
        now_str,
        len(universe_stocks),
    )

    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(md_dashboard)

    print(f"=== [Quant Collector] 전진 라벨링 및 스캔 전체 완료 ===")
    print(f"-> Top 5: {len(top_clean)}건 | 관찰: {len(watch_clean)}건 | 추적 중: {len(pending_list)}건")
    print(f"-> 저장소 README.md 모바일 대시보드 갱신 완료\n")


if __name__ == "__main__":
    run_collector()
