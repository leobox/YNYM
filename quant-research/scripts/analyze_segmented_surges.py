"""T-042: 급등 유형 세분화 분석:
1. 추세 돌파형 (Above MA20) 급등 전일 공통점
2. 바닥 반등형 (Below MA20) 급등 전일 공통점
"""
import os
import sys
import numpy as np
import pandas as pd
from scipy import stats

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.analyze_surge_precursors import load_and_resample_daily, extract_features_and_labels

def segment_analysis():
    _, daily_data = load_and_resample_daily()
    df = extract_features_and_labels(daily_data)
    
    surge = df[df['label'] == 'surge']
    normal = df[df['label'] == 'normal']
    
    surge_above = surge[surge['above_ma20'] == 1]
    surge_below = surge[surge['above_ma20'] == 0]
    normal_above = normal[normal['above_ma20'] == 1]
    normal_below = normal[normal['above_ma20'] == 0]
    
    features = [
        'vol_ratio_5', 'vol_ratio_20', 'vol_change_d1',
        'range_squeeze', 'bb_width', 'ma_spread',
        'dist_to_high20', 'doji_score', 'is_bull_d1',
        'lower_shadow_d1', 'late_vol_ratio', 'late_return',
        'ret_3d_before'
    ]
    
    print("================================================================================")
    print(f"1. [추세 돌파형] 20일선 위 급등 종목 전일 분석 (Surge N={len(surge_above)}, Normal N={len(normal_above)})")
    print("================================================================================")
    rows_above = []
    for f in features:
        s = surge_above[f].dropna()
        n = normal_above[f].dropna()
        if len(s) < 5 or len(n) < 5:
            continue
        stat, p = stats.mannwhitneyu(s, n, alternative='two-sided')
        cohen_d = (s.mean() - n.mean()) / np.sqrt((s.var() + n.var()) / 2)
        rows_above.append({
            'Feature': f, 'Surge_Med': s.median(), 'Normal_Med': n.median(),
            'Surge_Mean': s.mean(), 'Normal_Mean': n.mean(),
            'Cohen_d': cohen_d, 'p_value': p
        })
    df_above = pd.DataFrame(rows_above).sort_values('Cohen_d', ascending=False)
    print(df_above.to_string(index=False))
    
    print("\n================================================================================")
    print(f"2. [바닥 반등형] 20일선 아래 급등 종목 전일 분석 (Surge N={len(surge_below)}, Normal N={len(normal_below)})")
    print("================================================================================")
    rows_below = []
    for f in features:
        s = surge_below[f].dropna()
        n = normal_below[f].dropna()
        if len(s) < 5 or len(n) < 5:
            continue
        stat, p = stats.mannwhitneyu(s, n, alternative='two-sided')
        cohen_d = (s.mean() - n.mean()) / np.sqrt((s.var() + n.var()) / 2)
        rows_below.append({
            'Feature': f, 'Surge_Med': s.median(), 'Normal_Med': n.median(),
            'Surge_Mean': s.mean(), 'Normal_Mean': n.mean(),
            'Cohen_d': cohen_d, 'p_value': p
        })
    df_below = pd.DataFrame(rows_below).sort_values('Cohen_d', ascending=False)
    print(df_below.to_string(index=False))

if __name__ == '__main__':
    segment_analysis()
