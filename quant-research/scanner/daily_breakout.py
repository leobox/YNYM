"""T-052: 일봉 완료봉 기준 '추세 돌파' 신호 판정 (VCP 스캐너 재설계).

2026-09-20 감사 결과를 반영한 운영 판정기다.
- 기존 `scanner/vcp.py`는 60분봉에 일봉용 창(rolling 20 등)을 적용해 검증(T-046, 일봉)과 달랐다.
- VCP 수축 조건은 재검증에서 성과에 기여하지 않았다(제거 시 오히려 개선). 게이트에서 뺀다.

신호 = 아래 4가지를 모두 통과한 일봉(완료봉)이다. 수식은 `detect_vcp(lookback_pivot=60, max_vcp_ratio=99)`와
동일하며 `tests/test_daily_breakout.py`가 패리티를 검증한다.
  1. 추세: MA5 > MA20 > MA60 (MA120이 있으면 MA60 > MA120), 종가 > MA20
  2. 돌파: 종가 > 직전 60일 최고가(당일 제외), 양봉
  3. 거래량: 당일 거래량 >= 직전 20일 평균의 1.5배(당일 제외)
  4. 과열 아님: (종가 - MA20) / ATR14 <= 3.8
  + 종가 2,000원 이상, 거래량 > 0

이 신호는 '검증된 매수 신호'가 아니다. 임계값은 관찰 구간(2025-11~2026-09)에서 정해졌고
표본 밖 검증은 전진 기록(원장)으로 쌓는 중이다. 주문/체결 API는 없다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from scanner.indicators import _atr

LOOKBACK = 60
VOL_LOOKBACK = 20
MIN_VOL_SPIKE = 1.5
MAX_EXTENSION_ATR = 3.8
MIN_PRICE = 2000
WATCH_BAND = 0.04  # 돌파선까지 4% 이내면 관찰

# 결과 확정(연구 가정, 백테스트와 동일): +10% 목표 / -5% 손절 / 5거래일 / 편도 수수료 0.175% / 슬리피지 0.05%
TARGET_PCT = 0.10
STOP_PCT = 0.05
MAX_SESSIONS = 5
FEE = 0.00175
SLIPPAGE = 0.0005

MARKET_DONE_HOUR = 16  # 이 시각(KST) 전에는 오늘 일봉을 미완료로 본다


def clean_daily_bars(df: pd.DataFrame) -> pd.DataFrame:
    """일봉 정합성 검증: NaN·중복 일자·비양수 가격·OHLC 모순·음수 거래량 행을 제거하고 일자순 정렬한다."""
    df = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    ok = (
        (df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)
        & (df["High"] >= df[["Open", "Close", "Low"]].max(axis=1))
        & (df["Low"] <= df[["Open", "Close", "High"]].min(axis=1))
        & (df["Volume"] >= 0)
    )
    return df[ok]


def drop_incomplete_today(df: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """오늘(KST) 일봉은 장 마감 처리 시각(16:00) 전이면 제거한다. index는 tz-naive 일자."""
    if df.empty:
        return df
    now_kst = now.tz_convert("Asia/Seoul") if now.tzinfo is not None else now
    today = pd.Timestamp(now_kst.date())
    if now_kst.hour < MARKET_DONE_HOUR:
        return df[df.index < today]
    return df[df.index <= today]


def daily_checks(df: pd.DataFrame,
                 lookback: int = LOOKBACK,
                 min_vol_spike: float = MIN_VOL_SPIKE,
                 max_extension_atr: float = MAX_EXTENSION_ATR,
                 min_price: float = MIN_PRICE) -> pd.DataFrame:
    """일봉 DataFrame(Open/High/Low/Close/Volume)의 각 봉에 대해 4가지 체크와 신호를 계산한다.

    당일 값은 그 봉 마감 시점에 알 수 있는 정보만 쓴다(피봇·평균 거래량은 shift(1)).
    """
    c, h, l, o, v = df["Close"], df["High"], df["Low"], df["Open"], df["Volume"]

    ma5 = c.rolling(5).mean()
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60, min_periods=20).mean()
    ma120 = c.rolling(120, min_periods=30).mean()
    base_trend = (ma5 > ma20) & (ma20 > ma60) & (c > ma20)
    trend_ok = base_trend & (~ma120.notna() | (ma60 > ma120))

    pivot_level = h.shift(1).rolling(lookback).max()
    breakout_ok = (c > pivot_level) & (c > o)

    v_ma20 = v.shift(1).rolling(VOL_LOOKBACK).mean().replace(0, np.nan)
    vol_spike = v / v_ma20
    volume_ok = vol_spike >= min_vol_spike

    atr = _atr(df)
    extension_atr = (c - ma20) / atr.replace(0, np.nan)
    calm_ok = extension_atr <= max_extension_atr

    base_ok = (c >= min_price) & (v > 0)
    signal = trend_ok & breakout_ok & volume_ok & calm_ok & base_ok

    return pd.DataFrame({
        "signal": signal.fillna(False),
        "trend_ok": trend_ok.fillna(False),
        "breakout_ok": breakout_ok.fillna(False),
        "volume_ok": volume_ok.fillna(False),
        "calm_ok": calm_ok.fillna(False),
        "pivot_level": pivot_level,
        "vol_spike": vol_spike,
        "extension_atr": extension_atr,
    }, index=df.index)


def next_session_requirements(df: pd.DataFrame, last: pd.Series) -> dict | None:
    """마지막 완료 일봉 기준, '다음 거래일에 🟢가 되려면 필요한 조건'을 계산한다.

    관찰(🟡) 자격: 마지막 완료봉이 신호가 아니고, 추세가 유지되며 과열이 아니고,
    종가가 60일 최고가(돌파선)의 4% 이내 아래에 있을 것. 조건이 부족하면 None.
    돌파선·필요 거래량은 다음 거래일의 shift(1) 창과 같은 창(마지막 60일 고가, 마지막 20일 거래량)이다.
    """
    if len(df) < LOOKBACK:
        return None
    close = float(df["Close"].iloc[-1])
    volume = float(df["Volume"].iloc[-1])
    if close < MIN_PRICE or volume <= 0:
        return None
    if bool(last["signal"]) or not bool(last["trend_ok"]) or not bool(last["calm_ok"]):
        return None
    pivot_next = float(df["High"].iloc[-LOOKBACK:].max())
    if not np.isfinite(pivot_next) or pivot_next <= 0:
        return None
    gap = pivot_next / close - 1.0
    if not (0.0 <= gap <= WATCH_BAND):
        return None
    vol_ma = float(df["Volume"].iloc[-VOL_LOOKBACK:].mean())
    if not np.isfinite(vol_ma) or vol_ma <= 0:
        return None
    return {
        "pivot_next": pivot_next,
        "gap_pct": gap * 100.0,
        "req_volume": vol_ma * MIN_VOL_SPIKE,
    }


def resolve_outcome(daily: pd.DataFrame, signal_date: pd.Timestamp) -> dict:
    """신호일 다음 거래일 시가 진입 가정의 사후 결과를 일봉으로 확정한다(전진 기록용).

    status: PENDING(다음 거래일 시가 전) / NO_FILL(진입일 거래량 0) / OPEN(5거래일 미경과) /
            TARGET / STOP / TIME.
    일봉은 봉 안의 선후를 모르므로 같은 봉에서 손절·익절이 모두 닿으면 손절 우선(보수적).
    시가가 목표 이상이면 목표가 체결, 손절 아래 갭이면 시가 청산(백테스트 시뮬레이터와 동일 규칙).
    """
    later = daily.loc[daily.index > signal_date]
    if later.empty:
        return {"status": "PENDING"}
    first = later.iloc[0]
    if not (first["Open"] > 0) or not (first["Volume"] > 0):
        return {"status": "NO_FILL", "entry_date": later.index[0]}
    entry = float(first["Open"]) * (1 + SLIPPAGE)
    target = entry * (1 + TARGET_PCT)
    stop = entry * (1 - STOP_PCT)
    window = later.iloc[:MAX_SESSIONS]

    def result(status, exit_price, exit_date):
        ret = exit_price * (1 - SLIPPAGE) * (1 - FEE) / (entry * (1 + FEE)) - 1.0
        return {"status": status, "entry_date": later.index[0], "entry_price": entry,
                "exit_date": exit_date, "exit_price": float(exit_price), "net_return_pct": ret * 100.0}

    for d, bar in window.iterrows():
        if bar["Open"] <= stop:
            return result("STOP", float(bar["Open"]), d)
        if bar["Open"] >= target:
            return result("TARGET", target, d)
        if bar["Low"] <= stop:
            return result("STOP", stop, d)
        if bar["High"] >= target:
            return result("TARGET", target, d)
    if len(window) >= MAX_SESSIONS:
        return result("TIME", float(window["Close"].iloc[-1]), window.index[-1])
    return {"status": "OPEN", "entry_date": later.index[0], "entry_price": entry}
