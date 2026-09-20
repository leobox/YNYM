"""T-044: 시장 국면(Market Breadth 레짐) 스위칭 전략 백테스트.

레짐 정의:
- Market Breadth (MB): 349개 종목 중 당일 종가가 20일선 위에 있는 종목의 비율 (%)
- Bull Regime (MB >= 45%): 상승 훈풍 국면 -> Strategy A (추세 시동 돌파)만 진입
- Bear Regime (25% <= MB < 45%): 약세 침체 국면 -> 돌파 올스톱, Strategy B (바닥 반등)만 진입
- Panic Crash (MB < 25%): 시장 전체 투매 국면 -> 모든 매수 중단, 현금 100% 관망
"""
import os
import sys
import glob
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research.account_simulator import simulate_account
from research.regime import compute_market_breadth as _breadth
from scripts.backtest_surge_precursors import load_data, generate_precursor_signals, evaluate_strategy


def compute_market_breadth(data, sessions):
    """[T-049] 20세션 미만 구간은 0%가 아니라 NaN (research.regime)."""
    return _breadth(data)



def generate_regime_switched_signals(data, sessions, breadth):
    """
    레짐에 따라 전략을 스위칭하는 신호 생성:
    - MB >= 45%: Strategy A (TrendPrecursor)
    - 25% <= MB < 45%: Strategy B (PanicReversal)
    - MB < 25%: 현금 100% (신호 없음)
    """
    signals_trend = generate_precursor_signals(data, sessions, strategy_type='trend')
    signals_reversal = generate_precursor_signals(data, sessions, strategy_type='reversal')
    
    # 신호의 scan 일자(진입일)의 전일(신호 발생일)의 레짐 확인
    # signals dataframe has 'scan' (entry timestamp: next day 09:00)
    # and 'signal_date'
    switched_rows = []
    
    for idx, r in signals_trend.iterrows():
        sig_date = pd.Timestamp(r['signal_date']).date()
        mb = breadth.get(sig_date, np.nan)  # [T-049] 결측을 50%로 위장하지 않는다
        if pd.isna(mb):
            continue
        # Bull Regime: MB >= 45%
        if mb >= 45.0:
            switched_rows.append(r)
            
    for idx, r in signals_reversal.iterrows():
        sig_date = pd.Timestamp(r['signal_date']).date()
        mb = breadth.get(sig_date, np.nan)
        if pd.isna(mb):
            continue
        # Bear Regime: 25% <= MB < 45% (Panic crash 아래면 진입 안 함)
        if 25.0 <= mb < 45.0:
            switched_rows.append(r)
            
    if not switched_rows:
        return pd.DataFrame(columns=['scan', 'code', 'name', 'score'])
        
    df_switched = pd.DataFrame(switched_rows)
    # Deterministic Top 5 per scan time
    selected = []
    for scan_ts, group in df_switched.groupby('scan'):
        top = group.sort_values(['score', 'code'], ascending=[False, True]).head(5)
        selected.append(top)
        
    return pd.concat(selected, ignore_index=True)

if __name__ == '__main__':
    print("1. 349개 종목 60일 데이터 로딩 중...")
    data, names, sessions = load_data()
    print(f"  - 종목 수: {len(data)}, 세션 수: {len(sessions)}")
    
    print("2. Market Breadth(시장 폭, 20일선 상회 비율) 레짐 계산 중...")
    breadth = compute_market_breadth(data, sessions)
    print(f"  - 평균 시장 폭(유효 {int(breadth.notna().sum())}일): {breadth.mean():.1f}%, 최저: {breadth.min():.1f}%, 최고: {breadth.max():.1f}%")
    
    print("3. 신호 생성...")
    # 1) 레짐 없는 단순 결합 전략 (Strategy C: Combined)
    sig_no_regime = generate_precursor_signals(data, sessions, strategy_type='combined')
    # 2) 레짐 스위칭 전략 (Regime-Switching Strategy)
    sig_regime = generate_regime_switched_signals(data, sessions, breadth)
    
    print(f"  - 레짐 미적용 신호: {len(sig_no_regime)}건")
    print(f"  - 레짐 스위칭 신호: {len(sig_regime)}건 (비우호적 환경 필터링)")
    
    print("\n4. 계좌 시뮬레이션 백테스트 비교 (수수료 0.175%, 슬리피지 0.05%, +10% 익절, -5% 손절)...")
    results = []
    
    for max_pos in (1, 3):
        res_no = evaluate_strategy('Strategy C (No Regime, 상시 진입)', sig_no_regime, data, sessions, max_pos)
        res_reg = evaluate_strategy('Strategy D (Regime Switching, 국면 스위칭)', sig_regime, data, sessions, max_pos)
        results.extend([res_no, res_reg])
        
    df_res = pd.DataFrame(results)
    print("\n=========================================================================================")
    print("[RESULT] Market Regime Switching Battle Summary")
    print("=========================================================================================")
    print(df_res.to_string(index=False))
    
    out_md = os.path.join(ROOT, 'data', 'research', 'T-044_regime_report.md')
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write("# T-044 시장 국면(Market Breadth 레짐) 스위칭 전략 백테스트 보고서\n\n")
        f.write(df_res.to_markdown(index=False))
        f.write("\n")
    print(f"\nReport saved to: {out_md}")
