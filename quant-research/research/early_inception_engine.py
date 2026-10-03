"""
Early Inception Factor Model & Trend Weakening Exit Engine.
-----------------------------------------------------------
Addresses the core problem of Mode 2 (which picks stocks that already surged +60~75% over 60 days):
1. Early Inception Entry (상승 초입 진입):
   - Non-overextended filter: Filters out stocks already up > 25% over the past 60 days (mom60_5 <= 0.25).
   - Base consolidation & compression: 20-day high-low range <= 25% of price (volatility contraction).
   - Breakout from base: Close >= 0.98 * 20-day High, Close > SMA20, and early MA alignment (SMA20 >= SMA60).
   - Volume / Liquidity Ignition: Recent 5-day median volume / prior 20-day median volume >= 1.25x
     and positive Chaikin Money Flow (CMF20 > 0).
   - Minimum liquidity: 20-day median turnover >= 500M KRW.
2. Trend Weakening Exit (추세 약화 청산):
   - Rather than rigid 20-day hold or deep -15% stop:
   - SMA Weakening: Close < SMA10 (or SMA20) indicates short-term momentum loss -> exit at next open.
   - Peak Trailing Stop: Price pullbacks >= 6.0% from highest close since entry -> lock in gains.
   - Protective Stop Loss: -5.0% from entry price -> quickly cut losses on false breakouts.
   - Max Horizon: 20 trading days.
3. Realistic Cost Model:
   - Buy fee 0.015%, Sell fee 0.015%, Sell tax 0.18%, Slippage 0.05% on both sides.
   - Point-in-time strictly enforced. Zero lookahead bias.
   - Pure analytical state simulation. Absolutely NO real trading order execution.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Standard fee and cost assumptions
BUY_FEE = 0.00015
SELL_FEE = 0.00015
SELL_TAX = 0.0018
SLIPPAGE = 0.0005


def compute_early_inception_rankings(
    universe: Dict[str, pd.DataFrame],
    eval_date: pd.Timestamp,
    min_med_amt: float = 500_000_000.0,
    max_mom60_5: float = 0.25,
    max_range_ratio: float = 0.25,
    max_contraction_ratio: float = 0.85,
    min_vol_ratio: float = 1.20,
) -> pd.DataFrame:
    """
    Computes cross-sectional Early Inception rankings on eval_date strictly using
    data available up to eval_date close (zero lookahead).

    Identifies stocks at the inception of an uptrend rather than chasing late-stage leaders.
    """
    rows = []
    for code, df in universe.items():
        if eval_date not in df.index:
            continue
        hist = df.loc[:eval_date]
        if len(hist) < 125:
            continue

        c = hist["Close"]
        h = hist["High"]
        l = hist["Low"]
        v = hist["Volume"]
        amt = c * v

        current_c = float(c.iloc[-1])
        if current_c <= 0:
            continue

        # 1. Trend & Moving Averages
        sma10 = float(c.rolling(10).mean().iloc[-1])
        sma20 = float(c.rolling(20).mean().iloc[-1])
        sma60 = float(c.rolling(60).mean().iloc[-1])
        sma120 = float(c.rolling(120).mean().iloc[-1])

        # Baseline trend viability: Price above SMA20 and above SMA60 (or SMA20 >= SMA60)
        if current_c < sma20 or (current_c < sma60 and sma20 < sma60):
            continue

        # 2. Liquidity Filter: 20-day median turnover >= min_med_amt
        med_amt = float(amt.rolling(20).median().iloc[-1])
        if not np.isfinite(med_amt) or med_amt < min_med_amt:
            continue

        # 3. Non-Overextended Filter (상승 과열 배제 - 이미 60일간 크게 오른 종목 탈락)
        c_t5 = float(c.iloc[-5])
        c_t60 = float(c.iloc[-60])
        if c_t60 <= 0:
            continue
        mom60_5 = (c_t5 - c_t60) / c_t60
        ratio_to_sma60 = current_c / sma60 if sma60 > 0 else 999.0

        # If already up > 25% or price > 1.18x SMA60, it's late stage, NOT early inception!
        if mom60_5 > max_mom60_5 or ratio_to_sma60 > 1.18:
            continue

        # 4. Base Consolidation & Volatility Contraction (변동성 수축 및 박스권 형성)
        # Prior 20-day and 60-day high/low (excluding current bar to avoid lookahead/self-reference)
        prior_h20 = float(h.iloc[-21:-1].max()) if len(h) >= 21 else float(h.iloc[:-1].max())
        prior_l20 = float(l.iloc[-21:-1].min()) if len(l) >= 21 else float(l.iloc[:-1].min())
        prior_h60 = float(h.iloc[-61:-1].max()) if len(h) >= 61 else float(h.iloc[:-1].max())
        prior_l60 = float(l.iloc[-61:-1].min()) if len(l) >= 61 else float(l.iloc[:-1].min())
        if prior_h20 <= 0 or prior_l20 <= 0 or prior_h60 <= 0 or prior_l60 <= 0:
            continue

        range_20 = prior_h20 - prior_l20
        range_60 = prior_h60 - prior_l60
        range_width_ratio = range_20 / current_c

        # True Volatility Contraction: 20-day range is noticeably contracted compared to 60-day range
        contraction_ratio = range_20 / range_60 if range_60 > 0 else 1.0
        range_pos_20 = (current_c - prior_l20) / range_20 if range_20 > 0 else 1.0

        # Absolute band <= 25% AND 20-day range is at most max_contraction_ratio of 60-day range (measurable contraction)
        if range_width_ratio > max_range_ratio or contraction_ratio > max_contraction_ratio:
            continue

        # Strict Breakout: current close must meet or exceed prior 20-day high (no 98.5% discount)
        if current_c < prior_h20:
            continue

        # 5. Volume / Liquidity Ignition (수급 점화)
        prior_vol = float(v.iloc[-25:-5].median()) if len(v) >= 25 else float(v.iloc[:-5].median())
        recent_vol = float(v.iloc[-5:].median())
        vol_ratio = recent_vol / prior_vol if prior_vol > 0 else 0.0

        current_vol = float(v.iloc[-1])
        vol_spike_1d = current_vol / prior_vol if prior_vol > 0 else 0.0

        # Recent 5-day volume or current day volume must show ignition
        if vol_ratio < min_vol_ratio and vol_spike_1d < 1.35:
            continue

        # 6. Chaikin Money Flow (CMF 20)
        hl = (h.iloc[-20:] - l.iloc[-20:]).replace(0, np.nan)
        mf_mult = ((c.iloc[-20:] - l.iloc[-20:]) - (h.iloc[-20:] - c.iloc[-20:])) / hl
        mf_vol = mf_mult * v.iloc[-20:]
        sum_vol = float(v.iloc[-20:].sum())
        cmf20 = float(mf_vol.sum() / sum_vol) if sum_vol > 0 else -1.0

        # CMF must not be strongly negative (money flow supporting the move)
        if cmf20 < -0.05:
            continue

        # Short-term momentum: 5-day return
        ret_5 = (current_c - float(c.iloc[-6])) / float(c.iloc[-6]) if len(c) >= 6 else 0.0
        # Compression score: lower range_width is tighter compression (better)
        compression_score = max(0.0, 1.0 - (range_width_ratio / max_range_ratio))

        rows.append({
            "code": code,
            "close": current_c,
            "mom60_5": mom60_5,
            "ret_5": ret_5,
            "ratio_to_sma60": ratio_to_sma60,
            "range_width_ratio": range_width_ratio,
            "contraction_ratio": contraction_ratio,
            "range_pos_20": range_pos_20,
            "compression_score": compression_score,
            "vol_ratio": vol_ratio,
            "vol_spike_1d": vol_spike_1d,
            "cmf20": cmf20,
            "med_amt": med_amt,
            "sma10": sma10,
            "sma20": sma20,
            "sma60": sma60,
        })

    if not rows:
        return pd.DataFrame()

    res = pd.DataFrame(rows)

    # Cross-sectional percentiles for composite score
    res["rank_compression"] = res["compression_score"].rank(pct=True)
    res["rank_vol_ignition"] = (res["vol_ratio"] + res["vol_spike_1d"]).rank(pct=True)
    res["rank_cmf"] = res["cmf20"].rank(pct=True)
    res["rank_breakout"] = res["range_pos_20"].rank(pct=True)

    # Inception Score:
    # 35% Volume & Liquidity Ignition + 25% Compression & Tight Base + 20% Breakout + 20% CMF
    res["inception_score"] = (
        0.35 * res["rank_vol_ignition"] +
        0.25 * res["rank_compression"] +
        0.20 * res["rank_breakout"] +
        0.20 * res["rank_cmf"]
    )

    return res.sort_values(by=["inception_score", "med_amt"], ascending=[False, False]).reset_index(drop=True)


def simulate_trade_with_weakening_exit(
    df: pd.DataFrame,
    entry_date: pd.Timestamp,
    max_horizon: int = 20,
    stop_loss_pct: float = 0.05,
    peak_trailing_pct: float = 0.06,
    use_sma_exit: bool = True,
    sma_exit_window: int = 10,
    slippage: float = SLIPPAGE,
    buy_fee: float = BUY_FEE,
    sell_fee: float = SELL_FEE,
    sell_tax: float = SELL_TAX,
) -> Dict[str, Any]:
    """
    Simulates a trade entered at next session Open with Trend Weakening Exits:
    1. SMA Weakening: Close < SMA10 (or SMA20) after day 1 -> exit next session Open.
    2. Peak Trailing Stop: Pullback >= peak_trailing_pct from peak close since entry.
    3. Protective Stop Loss: Stop loss at entry * (1 - stop_loss_pct) (e.g. -5%).
    4. Max Horizon: Closed on day max_horizon if no prior exit.
    """
    if entry_date not in df.index:
        return {"status": "INVALID_ENTRY_DATE"}

    entry_idx = df.index.get_loc(entry_date)
    # Check if there is a next session to enter
    if entry_idx + 1 >= len(df):
        return {"status": "NO_NEXT_BAR_FOR_ENTRY"}

    actual_entry_date = df.index[entry_idx + 1]
    entry_bar = df.loc[actual_entry_date]
    entry_open = float(entry_bar["Open"])
    if entry_open <= 0 or float(entry_bar["Volume"]) <= 0:
        return {"status": "NO_ENTRY_VOLUME"}

    # Effective buy cost per share
    cost_price = entry_open * (1.0 + slippage) * (1.0 + buy_fee)

    hard_stop_price = entry_open * (1.0 - stop_loss_pct)

    # Future window from actual_entry_date onwards
    future_bars = df.iloc[entry_idx + 1 : entry_idx + 1 + max_horizon]
    if len(future_bars) < 2:
        return {"status": "INSUFFICIENT_FUTURE_BARS"}

    # Pre-calculate SMA for exit checks across full history up to each point
    c_series = df["Close"]
    sma_series = c_series.rolling(sma_exit_window).mean()

    highest_close = entry_open
    exit_date = None
    exit_price = 0.0
    exit_reason = "MAX_HORIZON"
    holding_days = 0

    pending_sma_exit = False
    pending_trailing_exit = False

    for i, (bar_date, bar) in enumerate(future_bars.iterrows()):
        holding_days = i + 1
        day_open = float(bar["Open"])
        day_high = float(bar["High"])
        day_low = float(bar["Low"])
        day_close = float(bar["Close"])

        # Check if we had a pending exit signaled by previous day's close
        if pending_sma_exit:
            exit_date = bar_date
            exit_price = day_open
            exit_reason = f"SMA{sma_exit_window}_WEAKENING"
            break

        if pending_trailing_exit:
            exit_date = bar_date
            exit_price = day_open
            exit_reason = "PEAK_TRAILING_STOP"
            break

        # Check protective stop loss (hard stop intraday)
        if day_open <= hard_stop_price:
            exit_date = bar_date
            exit_price = day_open  # Gap down stop
            exit_reason = "STOP_LOSS"
            break
        elif day_low <= hard_stop_price:
            exit_date = bar_date
            exit_price = hard_stop_price
            exit_reason = "STOP_LOSS"
            break

        # Update peak close since entry
        if day_close > highest_close:
            highest_close = day_close

        # Check peak trailing stop (signals exit for next bar Open)
        trailing_stop_threshold = highest_close * (1.0 - peak_trailing_pct)
        if day_close < trailing_stop_threshold and highest_close > entry_open * 1.02:
            pending_trailing_exit = True
            continue

        # Check SMA weakening on close (signals exit for next bar Open)
        if use_sma_exit and i >= 1:
            current_sma = float(sma_series.loc[bar_date])
            if np.isfinite(current_sma) and day_close < current_sma:
                # Trigger exit for next bar Open
                pending_sma_exit = True
                continue

    # If completed all bars without exit
    if exit_date is None:
        last_bar = future_bars.iloc[-1]
        exit_date = future_bars.index[-1]
        exit_price = float(last_bar["Close"])
        exit_reason = "MAX_HORIZON"

    # Effective exit net proceeds per share
    net_proceeds = exit_price * (1.0 - slippage) * (1.0 - sell_fee - sell_tax)
    net_return_pct = (net_proceeds / cost_price - 1.0) * 100.0
    raw_return_pct = (exit_price / entry_open - 1.0) * 100.0

    # Max favorable excursion (MFE) and Max adverse excursion (MAE) STRICTLY DURING HOLDING
    held_bars = future_bars.iloc[:holding_days]
    mfe_pct = (held_bars["High"].max() / entry_open - 1.0) * 100.0
    mae_pct = (held_bars["Low"].min() / entry_open - 1.0) * 100.0
    hit_5pct = bool(mfe_pct >= 5.0)

    return {
        "status": "COMPLETED",
        "entry_date": str(actual_entry_date.date()),
        "entry_price": entry_open,
        "exit_date": str(exit_date.date()) if hasattr(exit_date, "date") else str(exit_date),
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "holding_days": holding_days,
        "raw_return_pct": raw_return_pct,
        "net_return_pct": net_return_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "hit_5pct": hit_5pct,
        "win": net_return_pct > 0,
    }


def compute_early_inception_3d_rankings(
    universe: Dict[str, pd.DataFrame],
    eval_date: pd.Timestamp,
    max_contraction_ratio: float = 0.40,
    max_upper_wick_ratio: float = 0.30,
    min_med_amt: float = 500_000_000.0,
) -> pd.DataFrame:
    """
    Computes Early Inception 3-Day Focus rankings on eval_date strictly using
    data available up to eval_date close (zero lookahead). Replaces legacy Mode 2.
    1. Volatility contraction: 20d range / 60d range <= 0.40 (max_contraction_ratio)
    2. Strict 20d breakout: Close >= prior 20d High, Close > SMA20, Close > SMA60
    3. Solid body candle: Upper wick <= 30% of day's range (max_upper_wick_ratio)
    4. Non-overextended: mom60_5 <= 25%, price <= 1.18x SMA60
    5. Volume ignition: 5d vol >= 1.2x or 1d spike >= 1.35x
    6. CMF20 >= -0.05
    """
    rows = []
    for code, df in universe.items():
        if eval_date not in df.index:
            continue
        hist = df.loc[:eval_date]
        if len(hist) < 125:
            continue

        c = hist["Close"]
        h = hist["High"]
        l = hist["Low"]
        v = hist["Volume"]
        amt = c * v

        current_c = float(c.iloc[-1])
        current_h = float(h.iloc[-1])
        current_l = float(l.iloc[-1])
        current_o = float(hist["Open"].iloc[-1])
        current_v = float(v.iloc[-1])

        if current_c <= 0 or current_h <= current_l:
            continue

        sma20 = float(c.rolling(20).mean().iloc[-1])
        sma60 = float(c.rolling(60).mean().iloc[-1])
        if current_c < sma20 or current_c < sma60:
            continue

        med_amt = float(amt.rolling(20).median().iloc[-1])
        if not np.isfinite(med_amt) or med_amt < min_med_amt:
            continue

        c_t5 = float(c.iloc[-5])
        c_t60 = float(c.iloc[-60])
        if c_t60 <= 0:
            continue
        mom60_5 = (c_t5 - c_t60) / c_t60
        ratio_to_sma60 = current_c / sma60 if sma60 > 0 else 999.0

        if mom60_5 > 0.25 or ratio_to_sma60 > 1.18:
            continue

        prior_h20 = float(h.iloc[-21:-1].max()) if len(h) >= 21 else float(h.iloc[:-1].max())
        prior_l20 = float(l.iloc[-21:-1].min()) if len(l) >= 21 else float(l.iloc[:-1].min())
        prior_h60 = float(h.iloc[-61:-1].max()) if len(h) >= 61 else float(h.iloc[:-1].max())
        prior_l60 = float(l.iloc[-61:-1].min()) if len(l) >= 61 else float(l.iloc[:-1].min())
        if prior_h20 <= 0 or prior_l20 <= 0 or prior_h60 <= 0 or prior_l60 <= 0:
            continue

        range_20 = prior_h20 - prior_l20
        range_60 = prior_h60 - prior_l60
        contraction_ratio = range_20 / range_60 if range_60 > 0 else 1.0
        range_width_ratio = range_20 / current_c

        if contraction_ratio > max_contraction_ratio:
            continue
        if current_c < prior_h20:
            continue

        candle_range = current_h - current_l
        upper_wick_ratio = (current_h - max(current_c, current_o)) / candle_range if candle_range > 0 else 1.0
        if upper_wick_ratio > max_upper_wick_ratio:
            continue

        prior_vol = float(v.iloc[-25:-5].median()) if len(v) >= 25 else float(v.iloc[:-5].median())
        vol_ratio = float(v.iloc[-5:].median()) / prior_vol if prior_vol > 0 else 0.0
        vol_spike_1d = current_v / prior_vol if prior_vol > 0 else 0.0
        if vol_ratio < 1.20 and vol_spike_1d < 1.35:
            continue

        hl = (h.iloc[-20:] - l.iloc[-20:]).replace(0, np.nan)
        mf_mult = ((c.iloc[-20:] - l.iloc[-20:]) - (h.iloc[-20:] - c.iloc[-20:])) / hl
        mf_vol = mf_mult * v.iloc[-20:]
        sum_vol = float(v.iloc[-20:].sum())
        cmf20 = float(mf_vol.sum() / sum_vol) if sum_vol > 0 else -1.0
        if cmf20 < -0.05:
            continue

        score = (
            (1.0 - contraction_ratio / max_contraction_ratio) * 0.40
            + (1.0 - range_width_ratio / 0.25) * 0.30
            + (1.0 - upper_wick_ratio / max_upper_wick_ratio) * 0.30
        )

        rows.append({
            "code": code,
            "close": current_c,
            "mom60_5": mom60_5,
            "ratio_to_sma60": ratio_to_sma60,
            "range_width_ratio": range_width_ratio,
            "contraction_ratio": contraction_ratio,
            "upper_wick_ratio": upper_wick_ratio,
            "vol_ratio": vol_ratio,
            "vol_spike_1d": vol_spike_1d,
            "cmf20": cmf20,
            "med_amt": med_amt,
            "inception_3d_score": float(score),
        })

    if not rows:
        return pd.DataFrame()

    res = pd.DataFrame(rows)
    return res.sort_values(["inception_3d_score", "code"], ascending=[False, True]).reset_index(drop=True)

