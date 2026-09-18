import pandas as pd
import numpy as np

def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Smoothing 기반 표준 ATR (증권사 MTS와 동일 방식).

    [T-035] 이전에는 tr.rolling(14).mean()(단순이동평균)으로 근사했다. quant-collector에서
    같은 방식 근사가 실제 지표와 값·타이밍이 달라져 백테스트 결론이 바뀐 사례(T-030/T-034)가
    있어 여기도 표준 방식으로 맞춘다. ewm(alpha=1/period, adjust=False)는 Wilder의 재귀식
    (avg_today = (avg_어제*(period-1) + today)/period)과 수학적으로 동일하다.
    """
    previous = df.Close.shift(1)
    tr = pd.concat([df.High-df.Low,(df.High-previous).abs(),(df.Low-previous).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, min_periods=period, adjust=False).mean().replace(0,np.nan)
