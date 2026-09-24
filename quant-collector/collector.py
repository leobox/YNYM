"""
Quant Data Collector & Forward Labeling Engine
---------------------------------------------
GitHub Actions(평일 장중 15분 예약 주기) 및 모바일 수동(workflow_dispatch)으로 실행되는 시세 수집기 & 전진 라벨러입니다.
1. 네이버 시총 랭킹 기반 KOSPI200+KOSDAQ150 근사 유니버스(350종목) + 기존 추적 중인 pending 종목의 5m/10m/30m/60m 다중 분봉 수집 및 보관
2. 60분봉 기반 '조건 충족' 및 '관찰' 후보 포착 및 스마트 랭킹(스마트점수·돌파건전도·유동성) 산출
3. 5분봉 정밀 추적을 통한 다중 목표(+3/5/5.35/7/10%)/손절(-3/5%) 선접촉 라벨링 확정
4. 2차 판독기(위험 필터 / 메타 모델) 학습용 원본 데이터셋 자동 축적
5. LRM-60 v2.0 완료봉 4대 게이트와 3슬롯 가상 계좌를 별도 기록
6. GitHub 모바일 앱(README.md)에 15분 주기 스캔 및 추적 진행 현황 자동 갱신

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
from lrm60 import LRM60PaperBook

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BARS_HISTORY_DIR = DATA_DIR / "bars_history"
README_PATH = BASE_DIR / "README.md"
ROOT_README_PATH = BASE_DIR.parent / "README.md"
LRM_STATE_PATH = DATA_DIR / "lrm60_state.json"
LRM_EXPERIMENT_STATE_PATH = DATA_DIR / "lrm60_experiment_state.json"
DASHBOARD_MARK_START = "<!-- QUANT_DASHBOARD:START -->"
DASHBOARD_MARK_END = "<!-- QUANT_DASHBOARD:END -->"
STRATEGY_VERSION = "algorithm260917_v1"
# [T-041, 2026-09-18] 병렬 실험 3종(눌림목 trend_pullback_v1, 모멘텀·유동성 momentum_liquidity_v1,
# 거래대금 이상탐지 volume_zscore_accel_v1, 매물대+RSI resistance_breakout_rsi_v1)을 전부 검증한
# 결과 — 표본이 늘어나도 손익분기 근처를 벗어나지 못하거나(z-score) 구조적 결함이 확인돼(매물대+RSI,
# 단일봉 급등 추격 구조) 전부 폐기했다. quant-research 정식 프레임워크로 재검증(T-036~T-039)한 운영
# 전략 자체도 아직 엣지가 확정되지 않았지만, 실험을 더 늘리기보다 운영 전략 단일 구성으로 정리하고
# 지수 국면 필터 등 다음 개선을 기다리기로 했다. 과거 실험 상세 경위는 docs/tasks/T-023~T-033 참고.

# [T-033, 2026-09-18 폐기] 매물대 돌파 + 거래량 급증 + RSI 과매도 반등(resistance_breakout_rsi_v1)은
# 과거 60일 60분봉 워크포워드 검증에서 목표/손절/기간 16개 조합 전부 평균수익률이 마이너스로 나와
# 폐기했다. 원인 분석(실제 발동 80건 기준): RSI 과매도(35 이하) 시점~돌파봉 시차가 중앙값 0봉,
# 80%가 1~2봉 이내였고 돌파봉 자체 등락률도 중앙값 +5.67%로, "매물대를 서서히 소화하며 돌파"가
# 아니라 사실상 "단일봉 급반등 직후 추격 매수"를 잡는 구조였다(그래서 손절 비중이 높았음). 참고로
# "매물대가 항상 하단에 있는 하락 잡주만 잡는다"는 가설은 검증해보니 틀렸다 — POC 위치는 120봉
# range의 상/하단에 고르게 분포(중앙값 53%ile)했다. 재설계 없이 코드는 완전히 제거한다.

# 시가총액 상위 종목으로 KOSPI200/KOSDAQ150 공식 지수 구성종목을 근사한다.
# (KRX 공식 지수 구성종목 API는 세션 인증이 필요해 이 환경에서 직접 수집이 안 됨 —
#  네이버 시가총액 랭킹으로 근사하되 정확히 일치하지는 않는다.)
KOSPI_TOP_N = 200
KOSDAQ_TOP_N = 150
TOP_N = 5
WORKERS = 6
MIN_PRICE = 2000
VOLUME_WEIGHT = 0.2
MAX_EXTENSION_ATR = None
# 돌파봉 거래대금이 평소(typical) 대비 이 배수 이상이면 "과열 진입"으로 경고만 표시한다.
# 평소 관측 범위가 2~9배인데 43배짜리 이상치가 나와 잡은 임계값 — 확정 표본이 쌓이면 재검토.
OVERHEAT_TRIGGER_RATIO = 15.0


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


def fetch_index_feeds() -> Dict[str, pd.DataFrame]:
    """Naver daily index history; today's unfinished candle is never a regime input."""
    feeds = {}
    for market in ("KOSPI", "KOSDAQ"):
        try:
            payload = get_json(
                f"https://api.stock.naver.com/chart/domestic/index/{market}",
                {"periodType": "dayCandle"}, timeout=10,
            )
            rows = payload["priceInfos"]
            frame = pd.DataFrame(rows)
            frame.index = pd.to_datetime(frame["localDate"], format="%Y%m%d", errors="raise")
            frame["Close"] = pd.to_numeric(frame["closePrice"], errors="coerce")
            frame = frame[["Close"]].sort_index()
            if frame.index.has_duplicates or (frame["Close"] <= 0).any():
                raise ValueError("중복 날짜 또는 유효하지 않은 종가")
            feeds[market] = frame
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            print(f"[경고] {market} 지수 일봉 조회 실패: {exc}; 실험 계좌 신규 진입 차단")
    return feeds


def get_universe() -> List[Dict[str, Any]]:
    """KOSPI 시가총액 상위 200 + KOSDAQ 시가총액 상위 150 (KOSPI200/KOSDAQ150 근사)"""
    try:
        fdr = importlib.import_module("FinanceDataReader")
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "finance-datareader"])
        fdr = importlib.import_module("FinanceDataReader")

    listing = fdr.StockListing("KRX")
    valid = set(listing.loc[listing.Market.isin(["KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"]), "Code"].astype(str))
    if not valid:
        raise RuntimeError("상장 종목 목록이 비어 있습니다.")

    number = lambda x: pd.to_numeric(str(x).replace(",", ""), errors="coerce")
    rows = []

    for market, top_n in (("KOSPI", KOSPI_TOP_N), ("KOSDAQ", KOSDAQ_TOP_N)):
        market_rows = []
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
                if price >= MIN_PRICE:
                    market_rows.append({
                        "code": code,
                        "name": name,
                        "market": market,
                        "price": price,
                        "market_cap": cap,
                        "quote_time": x.get("localTradedAt"),
                        "amount": number(x["accumulatedTradingValue"]),
                    })

            if len(market_rows) >= top_n:
                break

        rows.extend(market_rows[:top_n])

    if not rows:
        raise RuntimeError("조회 가능한 유니버스가 없습니다.")

    df_u = pd.DataFrame(rows).dropna(subset=["amount"]).drop_duplicates("code")
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


def parse_multiframe_bars(item: Dict[str, Any], now: pd.Timestamp) -> Dict[str, pd.DataFrame]:
    """5분봉 원본 데이터셋으로부터 5m, 10m, 30m, 60m 완료봉을 정합성 있게 생성한다."""
    quotes = item["indicators"]["quote"][0]
    raw_df = pd.DataFrame(
        quotes,
        index=pd.to_datetime(item["timestamp"], unit="s", utc=True).tz_convert("Asia/Seoul"),
    )
    raw_df = raw_df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]].dropna()
    raw_df = raw_df[~raw_df.index.duplicated()].sort_index()

    # 한국 정규장 필터 (09:00 ~ 15:30)
    # 15:25~15:30 is the final five-minute input; 15:30 is outside the regular session.
    raw_df = raw_df[(raw_df.index.hour >= 9) & ((raw_df.index.hour < 15) | ((raw_df.index.hour == 15) & (raw_df.index.minute < 30)))]
    # 거래량 0인 더미 봉 제외
    raw_df = raw_df[~((raw_df.Volume == 0) & (raw_df.High == raw_df.Low))]
    # 5분봉 완료 여부 판정 (ends = index + 5m)
    ends_5m = raw_df.index + pd.Timedelta(minutes=5)
    df_5m = raw_df[ends_5m <= now].copy()

    def resample_by_day(df: pd.DataFrame, rule_min: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        res_list = []
        for _, g in df.groupby(df.index.date):
            agg = g.resample(f"{rule_min}min", origin="start").agg({
                "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
            }).dropna()
            # 60분봉의 경우 15:00은 30분짜리 장마감 봉
            ends = agg.index + pd.to_timedelta(
                np.where((rule_min == 60) & (agg.index.hour == 15), 30, rule_min), unit="m"
            )
            agg = agg[ends <= now]
            # 더미 봉 제외
            agg = agg[~((agg.Volume == 0) & (agg.High == agg.Low))]
            if not agg.empty:
                res_list.append(agg)
        if not res_list:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        out = pd.concat(res_list)
        return out[~out.index.duplicated()].sort_index()

    df_10m = resample_by_day(df_5m, 10)
    df_30m = resample_by_day(df_5m, 30)
    df_60m = resample_by_day(df_5m, 60)

    return {
        "5m": df_5m,
        "10m": df_10m,
        "30m": df_30m,
        "60m": df_60m,
    }


def fetch_all_bars(code: str, market: str, now: pd.Timestamp) -> Dict[str, pd.DataFrame]:
    """단 1회의 5m API 요청으로 5m, 10m, 30m, 60m 완료봉 데이터셋을 일괄 추출한다."""
    symbol = code + (".KQ" if market == "KOSDAQ" else ".KS")
    try:
        payload = get_json(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            {"range": "60d", "interval": "5m"},
            timeout=10,
        )["chart"]
        if payload.get("result"):
            return parse_multiframe_bars(payload["result"][0], now)
    except Exception as e:
        # 5m 조회 실패 시 60m 원본 fallback
        pass

    payload_60m = get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        {"range": "60d", "interval": "60m"},
        timeout=10,
    )["chart"]
    if not payload_60m.get("result"):
        raise RuntimeError(str(payload_60m.get("error")))
    df_60m = completed_bars(payload_60m["result"][0], now)
    return {"60m": df_60m, "30m": pd.DataFrame(), "10m": pd.DataFrame(), "5m": pd.DataFrame()}


def fetch_bars(code: str, market: str, now: pd.Timestamp) -> pd.DataFrame:
    """기존 단일 60분봉 조회 하위 호환 함수"""
    bars_dict = fetch_all_bars(code, market, now)
    return bars_dict.get("60m", pd.DataFrame())


def save_bar_history(code: str, df_bars: pd.DataFrame, timeframe: str = "60m") -> None:
    """[T-040, T-067] 종목별 분봉(60m, 30m, 10m, 5m) 원본 이력을 증분 저장한다.

    60m은 기존 호환성을 위해 {code}.csv에 유지하며, 5m/10m/30m은 {code}_{timeframe}.csv에
    마지막 타임스탬프 이후 신규 행만 증분 append하여 데이터 정합성을 유지한다.
    """
    if df_bars.empty:
        return
    BARS_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    if timeframe == "60m":
        path = BARS_HISTORY_DIR / f"{code}.csv"
    else:
        path = BARS_HISTORY_DIR / f"{code}_{timeframe}.csv"

    if not path.exists():
        df_bars.to_csv(path)
        return

    try:
        last_ts = pd.Timestamp(pd.read_csv(path, usecols=[0], index_col=0).index[-1])
    except Exception:
        df_bars.to_csv(path)  # 손상되었거나 빈 파일이면 통째로 다시 씀
        return

    new_rows = df_bars[df_bars.index > last_ts]
    if not new_rows.empty:
        new_rows.to_csv(path, mode="a", header=False)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Smoothing 기반 표준 ATR (증권사 MTS와 동일 방식).

    [T-034] 원래는 tr.rolling(14).mean()(단순이동평균)으로 근사했었다. RSI를
    검증하다가 같은 방식의 근사가 실제 지표와 값·타이밍이 달라진다는 걸 확인해서
    (resistance_breakout_rsi_v1 백테스트 결론이 무효화된 사례), ATR도 같은 문제가
    있어 표준 방식으로 교체했다. ewm(alpha=1/period, adjust=False)가 Wilder의
    재귀식(avg_today = (avg_어제*(period-1) + today)/period)과 수학적으로 동일하다.
    """
    prev = df.Close.shift(1)
    tr = pd.concat([df.High - df.Low, (df.High - prev).abs(), (df.Low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean().replace(0, np.nan)


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
        eligible &= vr >= 1.5
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


def score_stock(rec: Dict[str, Any], now: pd.Timestamp) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, pd.DataFrame]], Optional[str]]:
    try:
        bars_dict = fetch_all_bars(rec["code"], rec["market"], now)
        df = bars_dict.get("60m", pd.DataFrame())
        if not valid_ohlcv(df):
            return None, None, "가격/거래량 정합성 오류"
        if len(df) < 120 or df.Volume.iloc[-1] <= 0 or df.Close.iloc[-1] < MIN_PRICE:
            return None, bars_dict, "봉 부족/거래정지/가격조건 미달"
        if now - df.index[-1] > pd.Timedelta(days=7):
            return None, bars_dict, "최근 7일 데이터 없음"

        s = hourly_pattern(df, volume_weight=VOLUME_WEIGHT, max_extension_atr=MAX_EXTENSION_ATR).iloc[-1]
        conf = confirmed_breakout(df).iloc[-1]

        # 하위 분봉(10m, 5m, 30m) 기반 조기 돌파(Early Trigger) 감지
        early_trigger = False
        early_tf = ""
        brk_lvl = float(conf.breakout_level)

        if brk_lvl > 0:
            for tf_candidate in ["10m", "5m", "30m"]:
                df_sub = bars_dict.get(tf_candidate, pd.DataFrame())
                if len(df_sub) >= 5:
                    last_sub = df_sub.iloc[-1]
                    sub_c = float(last_sub.Close)
                    sub_v = float(last_sub.Volume)
                    sub_amt = sub_c * sub_v
                    sub_gap = (sub_c - brk_lvl) / brk_lvl * 100.0
                    vol_avg = float(df_sub.Volume.tail(15).mean())
                    # 돌파선 안착(+0.8~+4.0%) & 거래량 증가 & 거래대금 1억 이상
                    if (0.8 <= sub_gap <= 4.0) and (vol_avg > 0 and sub_v >= vol_avg * 1.3) and sub_amt >= 1e8:
                        early_trigger = True
                        early_tf = tf_candidate
                        break

        matched = bool(s.eligible and (conf.hold or early_trigger))
        if not np.isfinite(s.score):
            return None, bars_dict, "지표 계산 불가"

        c = df.Close
        fast, medium = c.rolling(12).mean(), c.rolling(26).mean()
        atr_val = _atr(df).iloc[-1]

        mode_badge = f"⚡ 조기({early_tf})" if early_trigger and not conf.hold else ("🟢 확정(60m)" if conf.hold else "제외")

        row = {
            "종목": rec["name"],
            "코드": rec["code"],
            "시장": rec["market"],
            "구분": "일치" if matched else "제외",
            "돌파모드": mode_badge,
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
            "_early_trigger": early_trigger,
            "_early_tf": early_tf,
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
            "early_trigger": early_trigger,
            "early_tf": early_tf,
        }
        row["_features"] = features

        return row, bars_dict, None
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


def calculate_smart_rank(
    features: Optional[Dict[str, Any]],
    signal_time_str: Optional[str] = None,
    entry_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    돌파 건전도, 유동성(거래대금), 진입 시간대, 패턴을 종합 평가한 스마트 랭킹 메트릭 산출
    - 돌파이격 골디락스 구간 (+1.2% ~ +4.0% 안착) 가산 (+15점)
    - 턱걸이 돌파 (< +0.8%) 감점 (-20점, 붕괴 위험)
    - 과열 상투 돌파 (> +5.5%) 감점 (-15점, 고점 추격매수 위험)
    - 오전 주도주 슬롯 (09:00, 10:00) 가산 (+10점)
    - 1시간 거래대금 (>= 20억: +10점, >= 50억: +15점) 및 VR (>= 2.0: +5점) 가산
    - 박스권 돌파 (base_breakout) 가산 (+10점)
    """
    feat = features or {}
    base_score = float(feat.get("score", 50.0))
    brk_lvl = float(feat.get("breakout_level", 0.0))
    amt = float(feat.get("trigger_amount_e8", feat.get("trigger_amount", 0.0)))
    vr = float(feat.get("volume_ratio", 0.0))
    pattern = str(feat.get("pattern_type", ""))

    p = entry_price if (entry_price is not None and entry_price > 0) else float(feat.get("current_close", feat.get("trigger_close", 0.0)))

    # 1. 돌파 이격률
    if brk_lvl > 0 and p > 0:
        gap_pct = (p - brk_lvl) / brk_lvl * 100.0
    else:
        gap_pct = 0.0

    # 건전도 판정 및 가산/감점
    if 1.2 <= gap_pct <= 4.0:
        health = "안착🟢"
        gap_score = 15.0
    elif gap_pct < 0.8:
        health = "턱걸이⚠️"
        gap_score = -20.0
    elif gap_pct > 5.5:
        health = "과열⚠️"
        gap_score = -15.0
    else:
        health = "보통⚪"
        gap_score = 0.0

    # 2. 시간대 가산점 (오전 09:00, 10:00 장 초반 주도주 우대)
    time_score = 0.0
    t_str = str(signal_time_str or "")
    if "09:00" in t_str or "10:00" in t_str:
        time_score = 10.0
    elif "11:00" in t_str or "12:00" in t_str:
        time_score = 5.0

    # 3. 거래대금 및 VR 가산점
    liq_score = 0.0
    if amt >= 50.0:
        liq_score += 15.0
    elif amt >= 20.0:
        liq_score += 10.0
    elif amt >= 10.0:
        liq_score += 5.0

    if vr >= 2.0:
        liq_score += 5.0

    # 4. 패턴 가산점
    pat_score = 10.0 if pattern == "base_breakout" else 0.0

    # 5. 조기 돌파 가산점 (10m/5m/30m 빠른 타점 우대)
    early_score = 10.0 if feat.get("early_trigger", False) else 0.0

    raw_score = base_score * 0.4 + 30.0 + gap_score + time_score + liq_score + pat_score + early_score
    smart_score = round(max(0.0, min(100.0, raw_score)), 1)

    return {
        "smart_score": smart_score,
        "gap_pct": round(gap_pct, 2),
        "health": health,
        "amount_e8": round(amt, 1),
        "vr": round(vr, 2),
        "pattern": pattern,
    }


def build_results(rows: List[Dict[str, Any]]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if not rows:
        return pd.DataFrame(), pd.DataFrame()

    df_rows = pd.DataFrame(rows)
    # 1. Top 5 최종 조건 충족 (스마트 랭킹 종합 점수 우선 순위 정렬)
    df_matched = df_rows[df_rows["_match"]].copy() if not df_rows.empty else pd.DataFrame()
    if not df_matched.empty:
        smart_ranks = []
        for _, r in df_matched.iterrows():
            feat = r.get("_features", {})
            sig_time = r.get("_bar_time_full", r.get("기준봉(KST)", ""))
            p = float(r.get("현재가", r.get("돌파봉종가", 0.0)))
            sr = calculate_smart_rank(feat, sig_time, p)
            smart_ranks.append(sr)

        df_matched["스마트점수"] = [sr["smart_score"] for sr in smart_ranks]
        df_matched["돌파이격"] = [sr["gap_pct"] for sr in smart_ranks]
        df_matched["건전도"] = [sr["health"] for sr in smart_ranks]
        df_matched["VR"] = [sr["vr"] for sr in smart_ranks]

        top = (
            df_matched
            .sort_values(["스마트점수", "점수", "코드"], ascending=[False, False, True])
            .head(TOP_N)
            .reset_index(drop=True)
        )
        top["점수"] = top["점수"].round(1)
        rank_badges = ["🥇 1위", "🥈 2위", "🥉 3위", "4위", "5위"]
        top.insert(0, "순위", [rank_badges[i] if i < len(rank_badges) else f"{i+1}위" for i in range(len(top))])
    else:
        top = pd.DataFrame()

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


def _render_signal_rows(
    sig_list: List[Dict[str, Any]],
    eval_by_sigid: Dict[str, Dict[str, Any]],
    new_codes: Set[str],
    empty_message: str,
) -> List[str]:
    """전략 하나에 속한 pending 신호 목록을 긴급도순 및 스마트 랭킹 지표와 함께 렌더링한다."""
    if not sig_list:
        return [f"*{empty_message}*\n"]

    priority = {"SELL": 0, "RECHECK": 1, "CAUTION": 2, "NEW": 3, "HOLD": 4}
    rows = []
    for sig in sig_list:
        code = sig["code"]
        ev = eval_by_sigid.get(sig["signal_id"])
        is_new = sig.get("trading_days_observed", 0) == 0 and code in new_codes

        if ev and ev["action_type"] in ("TAKE_PROFIT", "CUT_LOSS"):
            group, badge = "SELL", "🔴 SELL"
        elif ev and ev["decision"] == ExitSignal.BREAKOUT_AMBIGUOUS:
            group, badge = "RECHECK", "🟠 RECHECK"
        elif ev and ev["decision"] == ExitSignal.CAUTION:
            group, badge = "CAUTION", "🟡 CAUTION"
        elif is_new:
            group, badge = "NEW", "🆕 신규"
        else:
            group, badge = "HOLD", "🟢 HOLD"

        trigger_ratio = sig.get("features", {}).get("trigger_ratio")
        if trigger_ratio is not None and trigger_ratio >= OVERHEAT_TRIGGER_RATIO:
            badge = f"{badge} 🔥"

        entry_p = sig["entry_reference_price"]
        cur_p = ev["current_price"] if ev else entry_p
        pnl = ev["pnl_pct"] if ev else 0.0
        pnl_color = "🔴" if pnl >= 0 else "🔵"
        stop_5 = sig.get("stops", {}).get("stop_5", entry_p * 0.95)
        days = sig.get("trading_days_observed", 0)

        # 스마트 랭킹 및 세부 지표 산출
        feat = sig.get("features", {})
        sig_time = sig.get("signal_time_kst", "")
        sig_time_short = sig_time[5:] if len(sig_time) >= 16 else sig_time
        sr = calculate_smart_rank(feat, sig_time, entry_p)

        gap_str = f"{sr['gap_pct']:+.2f}% ({sr['health']})"
        amt_vr_str = f"{sr['amount_e8']:.1f}억 ({sr['vr']:.1f}x)"
        time_days_str = f"{sig_time_short} ({days}일차)"

        # 정렬: 긴급도(SELL>RECHECK>CAUTION>NEW>HOLD) -> SELL은 -pnl, 나머지는 스마트점수 내림차순
        sort_sub = -pnl if group == "SELL" else -sr["smart_score"]

        rows.append((priority[group], sort_sub, {
            "badge": badge, "name": sig["name"], "code": code,
            "cur_p": cur_p, "pnl": pnl, "pnl_color": pnl_color,
            "stop_5": stop_5, "days": days,
            "gap_str": gap_str, "amt_vr_str": amt_vr_str,
            "time_days_str": time_days_str,
            "smart_score": sr["smart_score"],
        }))

    rows.sort(key=lambda x: (x[0], x[1]))

    out = [
        "| 상태 | 종목(코드) | 현재가(수익률) | 손절가 | 돌파이격 (판정) | 대금 · VR | 신호시각 (경과) |",
        "|:---:|:---|:---:|:---:|:---:|:---:|:---:|",
    ]
    for _, _, d in rows:
        out.append(
            f"| {d['badge']} | **{d['name']}** ({d['code']}) | {d['cur_p']:,.0f} ({d['pnl_color']}{d['pnl']:+.2f}%) | {d['stop_5']:,.0f} | {d['gap_str']} | {d['amt_vr_str']} | {d['time_days_str']} |"
        )
    out.append("")
    return out


def render_lrm60_panel(state: Dict[str, Any], heading: str = "##",
                       experiment: Optional[Dict[str, Any]] = None,
                       now_str: Optional[str] = None) -> List[str]:
    """Timestamped LRM-60 panel for the GitHub mobile README."""
    last = state.get("last_bar_ts")
    if not last:
        lines = [
            f"{heading} 🧭 LRM-60 v2.0 · 완료봉 신호",
            "",
            "> 첫 완료 60분봉 수집을 기다리고 있습니다. 가상 계좌는 첫 실행부터 전진 기록합니다.",
            "", "---", "",
        ]
        if experiment:
            lines += [f"{heading} 🧪 후속안 · 분리 가상 계좌", "",
                      "> 아직 첫 완료봉을 기다리는 중입니다. 개선 효과는 검증되지 않았습니다.",
                      "", "---", ""]
        return lines

    bar_time = pd.Timestamp(last).strftime("%m-%d %H:%M")
    coverage = state.get("coverage", {})
    counts = state.get("gate_counts", {})
    positions = state.get("positions", {})
    pending = state.get("pending", [])
    equity = state.get("equity")
    equity_text = f"{equity:,.0f}원" if equity is not None else "평가 보류(보유 종목 봉 결측/무거래)"
    lines = [
        f"{heading} 🧭 LRM-60 v2.0 · 4대 게이트 / 3슬롯",
        "",
        f"> **기준 완료봉:** `{bar_time} KST` · **4대 게이트 통과:** `{coverage.get('passed', 0)}건` "
        f"· **다음 봉 시가 대기:** `{len(pending)}건` · **가상 보유:** `{len(positions)}/3` "
        f"· **가상 순자산:** `{equity_text}`",
        "",
        f"> 게이트별 통과: 정배열 `{counts.get('trend', 0)}` · 직전 20봉 돌파 `{counts.get('breakout', 0)}` "
        f"· 10억/2.5배 `{counts.get('liquidity', 0)}` · CLI/D_base `{counts.get('candle', 0)}` "
        f"(평가 가능 `{coverage.get('evaluated', 0)}`종목, 봉 결측 `{coverage.get('missing_bar', 0)}`종목)",
        "",
    ]
    last_point = pd.Timestamp(last)
    now_point = pd.Timestamp(now_str) if now_str else None
    missing_close = (now_point is not None and last_point.hour == 14 and
                     (now_point.date() > last_point.date() or
                      (now_point.date() == last_point.date() and
                       (now_point.hour > 15 or
                        (now_point.hour == 15 and now_point.minute >= 45)))))
    if missing_close:
        lines += [
            "> ⚠️ 마지막 세션의 **15:00~15:30 완료봉이 공급되지 않아** 14:00 봉까지만 평가했습니다. "
            "종가를 현재가로 복원하거나 15시 신호를 추정하지 않습니다.",
            "",
        ]
    if state.get("last_candidates"):
        pending_codes = {item["code"] for item in pending}
        lines += [
            "| 상태 | 종목 | 신호봉 종가 | 거래대금 | 상대 대금 | D_base | 다음 단계 |",
            "|:---:|:---|---:|---:|---:|---:|:---|",
        ]
        for item in state["last_candidates"][:5]:
            selected = item["code"] in pending_codes
            status = "🟡 3슬롯 대기" if selected else "⚪ 순위 밖/슬롯 없음"
            next_step = "다음 완료봉 시가의 갭·거래 여부 확인" if selected else "이번 봉 진입 대상 아님"
            lines.append(
                f"| {status} | **{item['name']}** ({item['code']}) | "
                f"{item['signal_close']:,.0f}원 | {item['amount_e8']:.1f}억 | "
                f"{item['r_vol']:.2f}배 | {item['d_base_pct']:+.2f}% | {next_step} |"
            )
    else:
        lines.append("> 이번 기준봉에서 4대 게이트를 모두 통과한 종목이 없습니다.")
    lines.append("")

    if positions:
        lines += ["**가상 보유 슬롯**", "", "| 종목 | 가상 진입가 | 구조 손절선 | 목표가 | 보유 완료봉 |",
                  "|:---|---:|---:|---:|---:|"]
        for code, pos in sorted(positions.items()):
            stop = max(pos["entry_price"] * .96, pos["breakout_level"] * .99)
            target = pos["entry_price"] * 1.08
            lines.append(f"| {pos['name']} ({code}) | {pos['entry_price']:,.0f}원 | "
                         f"{stop:,.0f}원 | {target:,.0f}원 | {pos['bars_held']} |")
        lines.append("")

    lines += [
        "> 신호는 완료봉 기준 관측값입니다. 체결·순자산은 가상 계산이며 실제 주문이나 수익 보장이 아닙니다. "
        "15:00 봉 누락·무거래·데이터 지연은 별도 확인하세요.",
        "", "---", "",
    ]
    if experiment:
        ex_regime = experiment.get("regime", {})
        regime_text = " · ".join(
            f"{market} {'통과' if ex_regime.get(market) is True else '차단' if ex_regime.get(market) is False else '데이터 없음'}"
            for market in ("KOSPI", "KOSDAQ")
        )
        ex_equity = experiment.get("equity")
        lines += [
            f"{heading} 🧪 백테스트 후속안 · 분리 가상 계좌",
            "",
            f"> 손절 버퍼 3% · +5% 1/2 분할 익절(잔여 +8%) · 전일 확정 지수 종가 > 20일 평균",
            f"> 지수 국면: {regime_text} · 대기 {len(experiment.get('pending', []))}건 · "
            f"보유 {len(experiment.get('positions', {}))}/3 · "
            f"가상 순자산 {f'{ex_equity:,.0f}원' if ex_equity is not None else '평가 보류'}",
            "",
            "> 아직 재백테스트·전진 검증되지 않은 가설입니다. 공식 v2.0과 성과를 합산하지 않습니다.",
            "", "---", "",
        ]
    return lines


def render_markdown_dashboard(
    top: pd.DataFrame,
    watch: pd.DataFrame,
    exit_evaluations: List[Dict[str, Any]],
    tracker_stats: Dict[str, Any],
    pending_list: List[Dict[str, Any]],
    now_str: str,
    scan_count: int,
    experiments: Optional[List[Dict[str, Any]]] = None,
    embed: bool = False,
    lrm_state: Optional[Dict[str, Any]] = None,
    lrm_experiment_state: Optional[Dict[str, Any]] = None,
) -> str:
    """GitHub 모바일 앱 및 웹 첫 화면(README.md)에 표시될 종합 대시보드 리포트

    보유/관찰/전진추적이 모두 같은 tracker.pending_signals를 참조하는 동일 종목이라
    표 3개에 중복 표시되던 것을 종목당 1행짜리 단일 표로 통합했다.

    운영 신호(STRATEGY_VERSION)와 병렬 실험 전략들은 같은 코드가 동시에 걸릴 수
    있어 signal_id 기준으로 분리 집계한다.

    embed=True면 루트 README.md의 QUANT_DASHBOARD 마커 구간에 삽입할 용도로,
    문서 제목(H1)과 중복 설명 문단·2차 판독기 안내 문단만 생략하고 하위 헤딩을
    한 단계 낮춘다. 모바일 앱에서 루트 README만 열어도 실험 전략까지 바로 보이도록
    병렬 실험 섹션은 embed 모드에서도 그대로 포함한다.
    """
    eval_by_sigid = {e["signal_id"]: e for e in exit_evaluations}
    sell_alerts = [
        e for e in exit_evaluations
        if e["action_type"] in ("TAKE_PROFIT", "CUT_LOSS") and e.get("strategy_version") == STRATEGY_VERSION
    ]

    main_list = [s for s in pending_list if s.get("strategy_version") == STRATEGY_VERSION]

    # 이번 스캔에서 신규 등록된(조건 충족/관찰) 종목 코드 집합
    new_codes = set(top["코드"]) if "코드" in top.columns else set()
    new_codes |= set(watch["코드"]) if "코드" in watch.columns else set()

    H2, H3 = ("###", "####") if embed else ("##", "###")

    lines = []
    if not embed:
        lines.extend(["# ⏱️ Quant Collector · LRM-60 v2.0", ""])

    lines.append(
        f"> ⏱️ **수집 시각**: `{now_str} KST (15분 예약 주기)` | 📊 **감시 유니버스**: `{scan_count}종목` | "
        f"⚡ **기존 패턴 포착**: `{len(top)}건` | 🎯 **기존 활성 추적**: `{len(main_list)}건`"
    )
    lines.append("")

    if not embed:
        lines.extend([
            "GitHub Actions가 **평일 장중 15분 간격으로 예약 실행**됩니다. LRM-60 신호는 완료된 60분봉에서만 확정하며, 아래의 기존 추적 표는 별도 전략의 과거 기록입니다.",
            "",
            "---",
            "",
        ])

    lines.extend(render_lrm60_panel(lrm_state or {}, H2, lrm_experiment_state, now_str))

    # 병렬 실험 전략들을 최상단에 노출한다 (운영 신호와는 표·통계 모두 분리 유지)
    if experiments:
        for exp in experiments:
            exp_list = [s for s in pending_list if s.get("strategy_version") == exp["strategy_version"]]
            stats = exp["stats"]
            tot = stats["total_resolved"]
            win_r = stats["win_rate"]
            win_str2 = f"**{win_r}%**" if win_r is not None else "데이터 축적 중"

            lines.extend([
                f"{H2} {exp['heading']}",
                "",
                exp["description"],
                "",
            ])
            lines.extend(_render_signal_rows(exp_list, eval_by_sigid, new_codes, exp["empty_message"]))
            lines.extend([
                f"- **완료된 평가 표본 수**: `{tot}건`",
                f"- **TARGET_FIRST (익절 선접촉)**: `{stats['target_first']}건`",
                f"- **STOP_FIRST (손절 선접촉)**: `{stats['stop_first']}건`",
                f"- **TIMEOUT (만기 종료)**: `{stats['timeout']}건`",
                f"- **익절 성공률 (Win Rate)**: {win_str2}",
                "",
                "---",
                "",
            ])

    # [최우선 알림] 긴급 매도/청산 신호는 테이블 형태로 깔끔하게 상단 표출
    if sell_alerts:
        lines.append(f"{H2} 🚨 [긴급] 실시간 매도·청산 권고 신호 ({len(sell_alerts)}건)")
        lines.append("")
        lines.append("> 5분 단위 실시간 청산 조건(목표 익절 / 이익 보존 / 돌파선 붕괴 / 절대 손절)이 감지되었습니다. MTS에서 신속히 대응하세요.")
        lines.append("")
        lines.append("| 구분 | 종목(코드) | 현재가(수익률) | 권고 사유 |")
        lines.append("|:---:|:---|:---:|:---|")
        for a in sell_alerts:
            tag = "🔴 익절" if a["action_type"] == "TAKE_PROFIT" else "🔴 손절"
            pnl_color = "🔴" if a["pnl_pct"] >= 0 else "🔵"
            rsn = a.get("reason", "청산 조건 도달")
            lines.append(f"| **{tag}** | **{a['name']}** ({a['code']}) | {a['current_price']:,.0f}원 ({pnl_color}{a['pnl_pct']:+.2f}%) | {rsn} |")
        lines.extend(["", "---", ""])

    # 1. 신규 조건 충족 후보 (스마트 랭킹 우선순위 Top 5)
    # A. 실시간 스캔에서 신규 조건 충족(top)이 발생했을 때
    if not top.empty:
        lines.append(f"{H2} 🎯 금일 조건 충족 신규 진입 후보 (스마트 랭킹 우선순위)")
        lines.append("")
        lines.append("> 돌파 건전도(이격 +1.2~+4.0% 안착 🟢), 오전 골든타임(09~10시), 거래대금 및 수급을 종합 평가한 진입 추천 순위입니다.")
        lines.append("")
        lines.append("| 순위 | 종목(코드) | 돌파모드 | 현재가 | 돌파이격 (판정) | 거래대금 · VR | 신호시각 | 스마트점수 |")
        lines.append("|:---:|:---|:---:|:---:|:---:|:---:|:---:|:---:|")
        for _, r in top.iterrows():
            c_code = r["코드"]
            c_name = r["종목"]
            p = r["현재가"] if pd.notna(r.get("현재가")) else r["돌파봉종가"]
            p_str = f"{p:,.0f}원" if pd.notna(p) and p > 0 else "-"
            rank_str = r.get("순위", "-")
            mode_str = r.get("돌파모드", "🟢 확정(60m)")
            gap_val = r.get("돌파이격")
            gap_str = f"{gap_val:+.2f}% ({r.get('건전도', '-')})" if pd.notna(gap_val) else "-"
            amt = r.get("돌파봉대금_억", 0.0)
            vr = r.get("VR", 0.0)
            amt_vr = f"{amt:.1f}억 ({vr:.1f}x)" if pd.notna(amt) and pd.notna(vr) else "-"
            sig_t = r.get("기준봉(KST)", "-")
            s_score_val = r.get("스마트점수")
            s_score = f"{s_score_val:.1f}점" if pd.notna(s_score_val) else "-"
            lines.append(f"| {rank_str} | **{c_name}** ({c_code}) | {mode_str} | {p_str} | {gap_str} | {amt_vr} | {sig_t} | {s_score} |")
        lines.extend(["", "---", ""])
    else:
        # B. 장 마감 후나 스캔 간격 중에는 최근 거래일의 신규 포착 종목들을 스마트 랭킹 상단 표로 상시 노출
        dates = sorted({s.get("signal_time_kst", "")[:10] for s in main_list if s.get("signal_time_kst")}, reverse=True)
        latest_date = dates[0] if dates else ""
        today_sigs = [s for s in main_list if s.get("signal_time_kst", "").startswith(latest_date)]
        if today_sigs:
            ranked_today = []
            for s in today_sigs:
                feat = s.get("features", {})
                sig_time = s.get("signal_time_kst", "")
                entry = float(s.get("entry_reference_price", 0.0))
                sr = calculate_smart_rank(feat, sig_time, entry)
                ev = eval_by_sigid.get(s["signal_id"]) or {}
                cur_p = ev.get("current_price", entry)
                pnl = ev.get("pnl_pct", 0.0)
                ranked_today.append({
                    "name": s["name"],
                    "code": s["code"],
                    "cur_p": cur_p,
                    "pnl": pnl,
                    "smart_score": sr["smart_score"],
                    "health": sr["health"],
                    "gap_pct": sr["gap_pct"],
                    "amount_e8": sr["amount_e8"],
                    "vr": sr["vr"],
                    "sig_time": sig_time[-5:],
                })
            ranked_today.sort(key=lambda x: (x["smart_score"], x["pnl"]), reverse=True)
            rank_badges = ["🥇 1위", "🥈 2위", "🥉 3위", "4위", "5위"]

            lines.append(f"{H2} 🎯 최근 신규 포착 종목 ({latest_date} 스마트 랭킹 순위)")
            lines.append("")
            lines.append(f"> 가장 최근 거래일(`{latest_date}`)에 신규 포착된 종목들을 스마트 랭킹 점수순으로 정렬한 진입 추천 순위입니다. 장 시작 전/장 마감 후에도 1~2위를 쉽게 판단할 수 있습니다.")
            lines.append("")
            lines.append("| 순위 | 종목(코드) | 현재가(수익률) | 돌파이격 (판정) | 거래대금 · VR | 신호시각 | 스마트점수 |")
            lines.append("|:---:|:---|:---:|:---:|:---:|:---:|:---:|")
            for idx, r in enumerate(ranked_today[:5]):
                badge = rank_badges[idx] if idx < len(rank_badges) else f"{idx+1}위"
                pnl_c = "🔴" if r["pnl"] >= 0 else "🔵"
                p_str = f"{r['cur_p']:,.0f} ({pnl_c}{r['pnl']:+.2f}%)"
                gap_str = f"{r['gap_pct']:+.2f}% ({r['health']})"
                amt_vr = f"{r['amount_e8']:.1f}억 ({r['vr']:.1f}x)"
                lines.append(f"| {badge} | **{r['name']}** ({r['code']}) | {p_str} | {gap_str} | {amt_vr} | {r['sig_time']} | **{r['smart_score']:.1f}점** |")
            lines.extend(["", "---", ""])

    # 2. 추적 중인 모든 신호(보유+관찰+신규)를 종목당 1행으로 통합한 마스터 표
    lines.extend([
        f"{H2} 📊 추적 중인 신호 현황 (가상 매수 100만원 가정)",
        "",
        "> 조건 충족·관찰 등록된 모든 신호를 한 표로 모아 긴급도순(매도 > 재확인 > 주의 > 신규 > 보유) 및 스마트점수순으로 정렬했습니다. "
        "돌파이격은 골디락스 안착(+1.2%~+4.0% 🟢), 턱걸이 위험(<0.8% ⚠️), 과열 추격위험(>5.5% ⚠️)으로 구분됩니다.",
        "",
    ])

    lines.extend(_render_signal_rows(main_list, eval_by_sigid, new_codes, "현재 추적 중인 활성 신호가 없습니다"))

    # 2. 누적 통계 박스
    tot_res = tracker_stats["total_resolved"]
    win_r = tracker_stats["win_rate"]
    win_str = f"**{win_r}%**" if win_r is not None else "데이터 축적 중"

    lines.extend([
        "---",
        "",
        f"{H2} 📈 누적 전진 검증 성과 (+5.35% 익절 vs -5% 손절 / 5일 기준)",
        "",
        f"- **완료된 평가 표본 수**: `{tot_res}건` (목표: 독립 표본 300건 이상)",
        f"- **TARGET_FIRST (익절 선접촉)**: `{tracker_stats['target_first']}건`",
        f"- **STOP_FIRST (손절 선접촉)**: `{tracker_stats['stop_first']}건`",
        f"- **TIMEOUT (만기 종료)**: `{tracker_stats['timeout']}건`",
        f"- **익절 성공률 (Win Rate)**: {win_str}",
    ])

    if not embed:
        lines.extend([
            "",
            "---",
            "",
            f"{H3} 🔬 2차 판독기 (Meta-Classifier) 파이프라인 안내",
            "- 본 수집기에서 생성되는 `data/resolved_signals.jsonl`은 향후 로지스틱 회귀 및 Gradient Boosting 기반의 **2차 위험 필터 모델 학습**에 사용됩니다.",
            "- 목표: 전진 검증에서 손절률의 95% 신뢰 상한을 최소화하고 위험 후보를 사전에 '판단 보류'로 필터링.",
        ])

    lines.append("")
    return "\n".join(lines)


def update_root_readme(embed_md: str) -> None:
    """루트 README.md의 QUANT_DASHBOARD 마커 구간만 최신 현황으로 교체한다."""
    if not ROOT_README_PATH.exists():
        print("[경고] 루트 README.md가 없어 실시간 현황 반영을 건너뜁니다.")
        return

    content = ROOT_README_PATH.read_text(encoding="utf-8")
    if DASHBOARD_MARK_START not in content or DASHBOARD_MARK_END not in content:
        print("[경고] 루트 README.md에 QUANT_DASHBOARD 마커가 없어 실시간 현황 반영을 건너뜁니다.")
        return

    pre, _, rest = content.partition(DASHBOARD_MARK_START)
    _, _, post = rest.partition(DASHBOARD_MARK_END)
    new_content = f"{pre}{DASHBOARD_MARK_START}\n\n{embed_md}\n{DASHBOARD_MARK_END}{post}"
    ROOT_README_PATH.write_text(new_content, encoding="utf-8")


def run_collector():
    now_dt = get_current_kst()
    now_ts = pd.Timestamp.now(tz="Asia/Seoul")
    stamp_day = now_dt.strftime("%Y%m%d")
    stamp_time = now_dt.strftime("%H%M")
    now_str = now_dt.strftime("%Y-%m-%d %H:%M")

    print(f"=== [Quant Collector] 전진 라벨러 & 스캔 시작: {now_str} KST ===")
    tracker = SignalTracker(DATA_DIR)
    lrm_book = LRM60PaperBook(LRM_STATE_PATH)
    lrm_experiment = LRM60PaperBook(LRM_EXPERIMENT_STATE_PATH, experimental=True)
    index_feeds = fetch_index_feeds()

    # 1. 대상 유니버스 확보 (KOSPI200+KOSDAQ150 근사, 최대 350종목)
    universe_stocks = get_universe()
    universe_codes = {s["code"] for s in universe_stocks}

    # 2. 유니버스에서 빠졌더라도 현재 추적 중인 Pending 종목 코드도 함께 수집 대상에 포함
    target_by_code = {rec["code"]: rec for rec in universe_stocks}
    tracked_records = list(tracker.pending_signals.values())
    tracked_records += list(lrm_book.state["positions"].values())
    tracked_records += lrm_book.state["pending"]
    tracked_records += list(lrm_experiment.state["positions"].values())
    tracked_records += lrm_experiment.state["pending"]
    for sig in tracked_records:
        code = sig["code"]
        if code not in target_by_code and sig.get("market"):
            target_by_code[code] = {
                "code": code, "name": sig["name"], "market": sig["market"],
            }
    all_scan_targets = list(target_by_code.values())
    missing_tracked = set(target_by_code) - universe_codes
    if missing_tracked:
        print(f"-> 유니버스 외 추적 중인 종목 {len(missing_tracked)}개 추가 수집 목록 포함")

    print(f"-> 총 {len(all_scan_targets)}개 대상 다중 분봉(5m/10m/30m/60m) 수집 및 패턴 채점 진행 중...")

    rows = []
    errors = []
    collected_candles_by_code = {}
    lrm_feeds = {}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = pool.map(lambda s: (s, score_stock(s, now_ts)), all_scan_targets)
        for rec, (row, bars_res, err) in results:
            if bars_res is not None:
                if isinstance(bars_res, dict):
                    df_60m = bars_res.get("60m", pd.DataFrame())
                    df_30m = bars_res.get("30m", pd.DataFrame())
                    df_10m = bars_res.get("10m", pd.DataFrame())
                    df_5m = bars_res.get("5m", pd.DataFrame())

                    # 다중 분봉 이력 저장 (60m은 기존 경로 유지, 30m/10m/5m은 접미사 분리 보관)
                    if not df_60m.empty:
                        lrm_feeds[rec["code"]] = df_60m
                        save_bar_history(rec["code"], df_60m, "60m")
                    if not df_30m.empty:
                        save_bar_history(rec["code"], df_30m, "30m")
                    if not df_10m.empty:
                        save_bar_history(rec["code"], df_10m, "10m")
                    if not df_5m.empty:
                        save_bar_history(rec["code"], df_5m, "5m")

                    # 트래커 및 청산 엔진용 완료봉 목록 (정밀한 5분봉 완료봉 전달, 5거래일 커버)
                    candle_list = []
                    source_df = df_5m if not df_5m.empty else df_60m
                    for idx, b in source_df.tail(400).iterrows():
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
                    lrm_feeds[rec["code"]] = bars_res
                    save_bar_history(rec["code"], bars_res, "60m")
                    candle_list = []
                    for idx, b in bars_res.tail(40).iterrows():
                        candle_list.append({
                            "time_kst": idx.strftime("%Y-%m-%d %H:%M"),
                            "open": float(b.Open),
                            "high": float(b.High),
                            "low": float(b.Low),
                            "close": float(b.Close),
                            "volume": int(b.Volume),
                        })
                    collected_candles_by_code[rec["code"]] = candle_list
            if row is not None:
                rows.append(row)
            else:
                errors.append({"code": rec["code"], "reason": err})

    print(f"-> 채점 완료: 성공 {len(rows)} / 제외·오류 {len(errors)}")

    # LRM-60은 위와 동일한 완료 60분봉을 사용한다. 이전 실행의 가상 계좌만 전진시킨다.
    lrm_advanced = lrm_book.advance(lrm_feeds, target_by_code)
    lrm_state = lrm_book.state
    lrm_experiment.advance(lrm_feeds, target_by_code, index_feeds)
    lrm_experiment_state = lrm_experiment.state
    print(f"-> LRM-60: 기준봉 {lrm_state['last_bar_ts'] or '없음'} | "
          f"4대 게이트 통과 {lrm_state['coverage'].get('passed', 0)}건 | "
          f"3슬롯 가상 보유 {len(lrm_state['positions'])}건 | 신규 봉 {lrm_advanced}")

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
    pd.DataFrame(lrm_state["last_candidates"]).to_csv(
        day_dir / f"{stamp_time}_lrm60.csv", index=False, encoding="utf-8-sig"
    )

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
        eval_res["signal_id"] = sig_id
        eval_res["strategy_version"] = pos.get("strategy_version")
        exit_evaluations.append(eval_res)

        if eval_res["action_type"] in ("TAKE_PROFIT", "CUT_LOSS"):
            print(f"-> [매도 권고] {eval_res['name']}({c_code}): {eval_res['decision']} | {eval_res['reason']}")

    # 8. README.md 모바일 대시보드 갱신
    # [T-041] 병렬 실험 전부 폐기(docs/tasks/T-041.md) — 운영 전략(STRATEGY_VERSION) 단일 구성.
    tracker_stats = tracker.get_summary_stats(strategy_version=STRATEGY_VERSION, ref_key="h5_t535_s5")
    pending_list = list(tracker.pending_signals.values())

    md_dashboard = render_markdown_dashboard(
        top_clean,
        watch_clean,
        exit_evaluations,
        tracker_stats,
        pending_list,
        now_str,
        len(universe_stocks),
        lrm_state=lrm_state,
        lrm_experiment_state=lrm_experiment_state,
    )

    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(md_dashboard)

    # 워크스페이스 루트 README.md의 QUANT_DASHBOARD 마커 구간에도 동일 현황 반영
    md_embed = render_markdown_dashboard(
        top_clean,
        watch_clean,
        exit_evaluations,
        tracker_stats,
        pending_list,
        now_str,
        len(universe_stocks),
        lrm_state=lrm_state,
        lrm_experiment_state=lrm_experiment_state,
        embed=True,
    )
    update_root_readme(md_embed)

    print(f"=== [Quant Collector] 전진 라벨링 및 스캔 전체 완료 ===")
    print(f"-> Top 5: {len(top_clean)}건 | 관찰: {len(watch_clean)}건 | 추적 중: {len(pending_list)}건")
    print(f"-> 저장소 README.md + 루트 README.md 모바일 대시보드 갱신 완료\n")


if __name__ == "__main__":
    run_collector()
