"""[T-052 주의] 운영 스캐너는 이 모듈을 더 이상 쓰지 않는다(`scanner/daily_breakout.py`로 대체).
이 함수를 60분봉에 적용하면 rolling 창이 일봉이 아닌 시간 단위가 되어 T-046 검증(일봉)과 다른 전략이 되고,
2026-09-20 재검증에서 랜덤 대비 12백분위·계좌 -61%(mp=1)였다. 연구·패리티 테스트 용도로만 남긴다.

T-047: VCP (Volatility Contraction Pattern, 변동성 축소 패턴) 운영 판정 모듈.

마크 미너비니(Mark Minervini)의 VCP 이론 및 2026년 실증 검증(T-046) 기준:
1. 추세 정배열: MA5 > MA20 > MA60 > MA120 (장기 상승 추세 기반)
2. 변동성 축소 (Contraction Waves):
   - 직전 5일 변동폭 < 직전 10일 변동폭 < 직전 20일 변동폭 (VCP 수축 파동)
   - vcp_ratio = 5일 변동폭 / 20일 변동폭 <= 0.65 (변동성 극대 수축)
3. 수급 마름 & 돌파 (Volume Dry-up & Pocket Spike):
   - 피봇 직전 거래량 마름 (5일 평균 거래량 <= 20일 평균)
   - 돌파일 거래량 폭발 (당일 거래량 >= 20일 평균의 150%)
4. 이격 과열 방지: (종가 - MA20) / ATR <= 3.8 (과도한 뇌동 추격 배제)
5. 피봇(Pivot) 돌파: 종가 > 직전 N봉 최고가 (수축 박스권 상단 돌파)
"""
import pandas as pd
import numpy as np
from scanner.indicators import _atr

def detect_vcp(df: pd.DataFrame, 
               lookback_pivot: int = 20, 
               min_vol_spike: float = 1.5,
               max_vcp_ratio: float = 0.65,
               max_extension_atr: float = 3.8) -> pd.DataFrame:
    """
    완료봉(Completed Bars)만을 사용하여 시점 불변성을 완벽 보장하는 VCP 판정 함수.
    
    Returns DataFrame with columns:
      - eligible: bool (VCP 슈퍼 신고가 돌파 충족 여부)
      - score: float (0~100점 랭킹 점수)
      - vcp_ratio: float (수축 비율, 낮을수록 우수)
      - vcp_stage: str ('3T_완전수축', '2T_부분수축', '미달')
      - vol_spike: float (거래량 배수)
      - pivot_level: float (피봇 돌파 기준 가격)
      - extension_atr: float (20일선 이격도)
      - bull_aligned: bool (정배열 여부)
    """
    c, h, l, o, v = df['Close'], df['High'], df['Low'], df['Open'], df['Volume']
    
    # 1. 이동평균선
    ma5 = c.rolling(5).mean()
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60, min_periods=20).mean()
    ma120 = c.rolling(120, min_periods=30).mean()
    
    # 이평선 정배열 (최소 5 > 20 > 60)
    bull_aligned = (ma5 > ma20) & (ma20 > ma60) & (c > ma20)
    # 120선이 충분할 경우 120선 정배열까지 반영
    valid_120 = ma120.notna()
    bull_aligned_full = bull_aligned & (~valid_120 | (ma60 > ma120))
    
    # 2. 피봇(Pivot) 돌파 기준선 (직전 lookback 봉의 최고가, 당일 제외: shift(1))
    pivot_level = h.shift(1).rolling(lookback_pivot).max()
    is_breakout = (c > pivot_level) & (c > o)
    
    # 3. VCP 변동성 축소 파동 (Volatility Contraction Waves)
    # 20봉, 10봉, 5봉 고저폭 (당일 제외: shift(1))
    range_20 = (h.shift(1).rolling(20).max() - l.shift(1).rolling(20).min()) / c.shift(1).replace(0, np.nan)
    range_10 = (h.shift(1).rolling(10).max() - l.shift(1).rolling(10).min()) / c.shift(1).replace(0, np.nan)
    range_5 = (h.shift(1).rolling(5).max() - l.shift(1).rolling(5).min()) / c.shift(1).replace(0, np.nan)
    
    vcp_ratio = range_5 / range_20.replace(0, np.nan)
    
    # 수축 파동 단계 판정
    is_3t = (range_5 < range_10) & (range_10 < range_20)
    is_2t = (range_5 < range_20)
    vcp_stage = np.where(is_3t, '3T_완전수축', np.where(is_2t, '2T_부분수축', '수축미달'))
    
    # 4. 거래량 수급 (Dryup & Spike)
    v_ma20 = v.shift(1).rolling(20).mean().replace(0, np.nan)
    v_ma5 = v.shift(1).rolling(5).mean().replace(0, np.nan)
    
    vol_dryup = v_ma5 / v_ma20 # 직전 5일간 거래량이 줄어들었는가
    vol_spike = v / v_ma20     # 돌파일 거래량 배수
    
    # 5. 이격 과열도 (ATR 단위)
    atr = _atr(df)
    extension_atr = (c - ma20) / atr.replace(0, np.nan)
    
    # 자격 요건 (Eligible) 종합
    eligible = (
        is_breakout & 
        bull_aligned_full & 
        (vcp_ratio <= max_vcp_ratio) & 
        (vol_spike >= min_vol_spike) & 
        (extension_atr <= max_extension_atr) & 
        (c >= 2000) & 
        (v > 0)
    )
    
    # 종합 점수 (Score: 0 ~ 100)
    # - VCP 수축도 점수 (수축이 타이트할수록 높은 점수, 최대 40점)
    score_vcp = ((1.0 - vcp_ratio.clip(0, 1.0)) * 40.0).fillna(0)
    # - 거래량 폭발 점수 (최대 35점)
    score_vol = (vol_spike.clip(1.0, 5.0) / 5.0 * 35.0).fillna(0)
    # - 이평선 이격 적정성 점수 (20일선에 가까울수록 높은 점수, 최대 25점)
    score_ext = ((1.0 - (extension_atr.clip(0, 4.0) / 4.0)) * 25.0).fillna(0)
    
    score = score_vcp + score_vol + score_ext
    score = score.where(np.isfinite(score), 0.0)
    eligible = eligible & (score > 0) & np.isfinite(score)
    
    return pd.DataFrame({
        'eligible': eligible.fillna(False),
        'score': score.round(1),
        'vcp_ratio': vcp_ratio.round(3),
        'vcp_stage': vcp_stage,
        'vol_spike': vol_spike.round(2),
        'vol_dryup': vol_dryup.round(2),
        'pivot_level': pivot_level.round(1),
        'extension_atr': extension_atr.round(2),
        'bull_aligned': bull_aligned_full.fillna(False)
    }, index=df.index)
