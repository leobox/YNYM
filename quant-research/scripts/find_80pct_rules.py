"""T-045: 213일 장기데이터에서 승률 80%+ 달성 가능한 ML 조건 및 손익비 탐색."""
import os
import sys
import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.ml_high_winrate_search import load_ytd_data, compute_market_breadth, extract_10_features_and_labels
from research.account_simulator import simulate_account

data, names, sessions = load_ytd_data()
breadth = compute_market_breadth(data, sessions)

# Sweep target and stop
settings = [
    (0.08, 0.04, 'Target +8%, Stop -4% (2:1)'),
    (0.06, 0.03, 'Target +6%, Stop -3% (2:1)'),
    (0.05, 0.04, 'Target +5%, Stop -4% (1.25:1)'),
    (0.04, 0.03, 'Target +4%, Stop -3% (1.33:1)'),
]

train_dates = set(sessions[25:145])
test_dates = set(sessions[145:])

for tgt, stp, desc in settings:
    print(f"\n=======================================================")
    print(f"Testing: {desc}")
    print(f"=======================================================")
    df_samples = extract_10_features_and_labels(data, sessions, breadth, target_pct=tgt, stop_pct=stp, max_hold=5)
    
    train_df = df_samples[df_samples['date'].isin(train_dates)].copy()
    test_df = df_samples[df_samples['date'].isin(test_dates)].copy()
    
    features = [c for c in train_df.columns if c.startswith('F')]
    
    # Gradient Boosting with strict regularization
    hgb = HistGradientBoostingClassifier(max_depth=4, min_samples_leaf=30, l2_regularization=3.0, random_state=42)
    hgb.fit(train_df[features], train_df['label'])
    
    test_df['prob'] = hgb.predict_proba(test_df[features])[:, 1]
    
    for th in [0.45, 0.50, 0.55, 0.60]:
        sub = test_df[test_df['prob'] >= th]
        if len(sub) >= 10:
            raw_winrate = sub['label'].mean() * 100
            print(f"Threshold >= {th:.2f}: Candidates={len(sub)}, Raw Win Rate = {raw_winrate:.1f}%")
