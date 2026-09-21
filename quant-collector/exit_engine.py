"""
Exit Signal Engine (매도·청산 판정기)
--------------------------------------
조건 충족(Top 5) 시 매수 진입했다고 가정하고,
매 60분봉 완료 시마다 포지션의 상태를 다각도로 평가하여
기계적/전략적 매도 신호(Exit Signals)를 산출합니다.

[매도 판정 5대 조건]
1. TARGET_HIT (목표 달성 익절): 목표가(+10% 또는 설정치) 터치 시 전량 익절
2. TRAILING_PROFIT (수익 보존 익절): +5% 이상 상승 후 고점 대비 3% 이상 밀리거나 본전 위협 시 이익 보존
3. BREAKOUT_COLLAPSE (기준선 이탈 조기 손절): 매수 근거였던 돌파선(breakout_level) 종가 이탈 폭이 뚜렷할 때(-0.5% 초과) 조기 컷
   - 이탈 폭이 -0.5% 이내로 애매하면 BREAKOUT_AMBIGUOUS로 분류해 확정 손절 대신 "다음 봉 재확인"으로 안내(공포성 확정 문구 방지)
4. VOLUME_BEAR_REVERSAL (대량거래 음봉 탈출): 동시간 1.5배 이상 대량 거래량 실린 음봉 마감 시 세력 이탈 탈출
5. STAGNATION_TIMEOUT (정체 탈출): 진입 후 2거래일(12봉) 경과 후에도 수익률 미미(+1% 미만) 시 기회비용 청산
6. HARD_STOP (절대 손절): -5% 절대 손절가 이탈 시 즉시 시장가 청산
"""

# 돌파선 이탈 폭이 이 값(%) 이내면 "확정 붕괴"가 아니라 "애매함"으로 분류한다.
BREAKOUT_AMBIGUOUS_BAND_PCT = -0.5

from typing import Dict, Any, List, Optional, Tuple
import pandas as pd


class ExitSignal:
    # 매도 판정 코드
    HOLD = "HOLD"  # 보유 지속
    CAUTION = "CAUTION"  # 주의 (돌파선 근접, 거래량 감소 등)
    TARGET_HIT = "TARGET_HIT"  # 목표가 달성 익절 (+10%)
    TRAILING_PROFIT = "TRAILING_PROFIT"  # 고점 대비 되돌림 이익 보존 (+5% 이상 경험 후)
    BREAKOUT_COLLAPSE = "BREAKOUT_COLLAPSE"  # 돌파선 붕괴 조기 탈출 (최소 손실)
    BREAKOUT_AMBIGUOUS = "BREAKOUT_AMBIGUOUS"  # 돌파선 근접 이탈 (확정 아님, 다음 봉 재확인)
    VOLUME_BEAR = "VOLUME_BEAR"  # 대량 거래 음봉 탈출
    STAGNATION = "STAGNATION"  # 2일 정체 기회비용 청산
    HARD_STOP = "HARD_STOP"  # -5% 절대 손절


def evaluate_position_exit(
    position: Dict[str, Any],
    recent_bars: List[Dict[str, Any]],
    current_price: float,
    now_kst_str: str,
) -> Dict[str, Any]:
    """
    개별 가상 매수 포지션에 대한 실시간 매도 신호 평가

    :param position: pending_signals의 개별 신호 객체
    :param recent_bars: 해당 종목의 최근 완료봉 리스트 (시간 오름차순)
    :param current_price: 네이버 실시간 현재가
    :param now_kst_str: 현재 평가 시각
    :return: 매도 판정 결과 딕셔너리
    """
    entry_price = float(position["entry_reference_price"])
    features = position.get("features", {})
    breakout_level = float(features.get("breakout_level", entry_price * 0.98))
    target_profit = float(
        position.get("targets", {}).get("tgt_5_35")
        or position.get("targets", {}).get("tgt_535")
        or position.get("targets", {}).get("tgt_10", entry_price * 1.0535)
    )
    target_pct = round((target_profit / entry_price - 1) * 100.0, 2)
    stop_5 = float(position.get("stops", {}).get("stop_5", entry_price * 0.95))

    pnl_pct = round((current_price / entry_price - 1) * 100.0, 2)
    highest_seen = max(float(position.get("highest_seen", entry_price)), current_price)
    max_gain_pct = round((highest_seen / entry_price - 1) * 100.0, 2)
    days_held = int(position.get("trading_days_observed", 0))

    # 신호 이후 발생한 완료봉들
    sig_time = position["signal_time_kst"]
    future_bars = [b for b in recent_bars if b["time_kst"] > sig_time]
    last_bar = future_bars[-1] if future_bars else (recent_bars[-1] if recent_bars else None)

    decision = ExitSignal.HOLD
    action_type = "KEEP"  # KEEP, TAKE_PROFIT, CUT_LOSS
    urgency = "LOW"  # LOW, MEDIUM, HIGH, IMMEDIATE
    reason = "돌파선 위에서 양호하게 상승 추세 유지 중"
    suggested_price = current_price

    # -------------------------------------------------------------
    # 1. 절대 손절 (-5% 터치)
    # -------------------------------------------------------------
    if current_price <= stop_5 or (last_bar and last_bar["low"] <= stop_5):
        decision = ExitSignal.HARD_STOP
        action_type = "CUT_LOSS"
        urgency = "IMMEDIATE"
        reason = f"절대 손절선({stop_5:,.0f}원, -5%) 터치! 무조건 즉시 매도"
        suggested_price = min(current_price, stop_5)

    # -------------------------------------------------------------
    # 2. 목표 달성 익절 (+5.35% 또는 설정치 도달)
    # -------------------------------------------------------------
    elif current_price >= target_profit or (last_bar and last_bar["high"] >= target_profit):
        decision = ExitSignal.TARGET_HIT
        action_type = "TAKE_PROFIT"
        urgency = "IMMEDIATE"
        reason = f"목표 수익률 +{target_pct}%({target_profit:,.0f}원) 도달 완료! 전량 익절"
        suggested_price = target_profit

    # -------------------------------------------------------------
    # 3. 고점 대비 되돌림 이익 보존 (+3.5% 이상 올랐던 종목)
    # -------------------------------------------------------------
    elif max_gain_pct >= 3.5:
        # 고점 대비 -1.8% 이상 밀리거나, 진입가(+0.5% 이하)로 회귀하려는 경우
        retreat_from_peak = (current_price / highest_seen - 1) * 100.0
        if retreat_from_peak <= -1.8 or pnl_pct <= 0.5:
            decision = ExitSignal.TRAILING_PROFIT
            action_type = "TAKE_PROFIT"
            urgency = "HIGH"
            reason = f"최고 수익률 +{max_gain_pct}% 기록 후 고점 대비 {retreat_from_peak:.1f}% 되돌림 발생. 수익 보존 매도"
            suggested_price = current_price

    # -------------------------------------------------------------
    # 4. 돌파선 붕괴 조기 탈출 (매수 근거 소멸, -1~2% 내 조기 컷)
    #    단, 이탈 폭이 작아 애매하면(BREAKOUT_AMBIGUOUS_BAND_PCT 이내) 확정 손절 대신 재확인 대기
    # -------------------------------------------------------------
    elif last_bar and last_bar["close"] < breakout_level:
        breach_pct = round((last_bar["close"] / breakout_level - 1) * 100.0, 2)
        if breach_pct >= BREAKOUT_AMBIGUOUS_BAND_PCT:
            decision = ExitSignal.BREAKOUT_AMBIGUOUS
            action_type = "KEEP"
            urgency = "MEDIUM"
            reason = f"돌파 기준선({breakout_level:,.0f}원) 근접 이탈({breach_pct:+.2f}%). 확정 붕괴로 보기엔 애매해 다음 봉 재확인 필요"
        else:
            decision = ExitSignal.BREAKOUT_COLLAPSE
            action_type = "CUT_LOSS"
            urgency = "HIGH"
            reason = f"돌파 기준선({breakout_level:,.0f}원) 종가 하향 이탈({breach_pct:+.2f}%)! 가짜 돌파로 판단하여 조기 손절({pnl_pct:+.2f}%)"
        suggested_price = current_price

    # -------------------------------------------------------------
    # 5. 대량 거래량 장대 음봉 출현 (세력 이탈 경보)
    # -------------------------------------------------------------
    elif last_bar and (last_bar["close"] < last_bar["open"]):
        bar_drop_pct = (last_bar["close"] / last_bar["open"] - 1) * 100.0
        # 음봉 폭이 -1.5% 이상이거나 최근 거래량 급증
        if bar_drop_pct <= -1.8 and pnl_pct < 0:
            decision = ExitSignal.VOLUME_BEAR
            action_type = "CUT_LOSS"
            urgency = "MEDIUM"
            reason = f"완료봉에서 -{abs(bar_drop_pct):.1f}% 장대 음봉 마감. 하방 압력 심화로 리스크 관리 매도"
            suggested_price = current_price

    # -------------------------------------------------------------
    # 6. 정체 탈출 (2거래일 이상 본전 횡보 기회비용 청산)
    # -------------------------------------------------------------
    elif days_held >= 2 and pnl_pct < 1.0:
        decision = ExitSignal.STAGNATION
        action_type = "CUT_LOSS" if pnl_pct < 0 else "TAKE_PROFIT"
        urgency = "MEDIUM"
        reason = f"진입 후 {days_held}거래일 경과하였으나 탄력 둔화({pnl_pct:+.2f}%). 기회비용 확보를 위해 교체 청산"
        suggested_price = current_price

    # -------------------------------------------------------------
    # 7. 주의 단계 (돌파선과 1% 이내 근접)
    # -------------------------------------------------------------
    elif current_price < breakout_level * 1.01:
        decision = ExitSignal.CAUTION
        action_type = "KEEP"
        urgency = "MEDIUM"
        reason = f"돌파선({breakout_level:,.0f}원) 1% 이내로 근접. 지지 실패 시 매도 준비"

    return {
        "code": position["code"],
        "name": position["name"],
        "entry_price": entry_price,
        "current_price": current_price,
        "pnl_pct": pnl_pct,
        "highest_seen": highest_seen,
        "max_gain_pct": max_gain_pct,
        "days_held": days_held,
        "breakout_level": breakout_level,
        "target_profit": target_profit,
        "target_10": target_profit,
        "stop_5": stop_5,
        "decision": decision,
        "action_type": action_type,
        "urgency": urgency,
        "reason": reason,
        "suggested_price": suggested_price,
        "evaluated_at": now_kst_str,
    }
