"""T-043: 10일치 학습 -> 50일치 판정 ML 과적합 vs 일반화 백테스트 실험.

349개 종목 60거래일 데이터:
- Train: 앞의 10거래일 (단 10일의 역사만 학습)
- Test: 뒤의 50거래일 (OOS 실전 판정)

모델 구성:
1. Extreme Overfit Tree (max_depth=None, min_samples_split=2) -> Train 승률 100% 목표
2. Overfit Random Forest (n_estimators=50, max_depth=None)
3. Regularized HistGradientBoosting (max_depth=3, min_samples_leaf=15, L2=2.0)
"""
import os
import sys
import glob
import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research.account_simulator import simulate_account

DATA_DIR = os.path.join(ROOT, 'data', 'live_60d_20260918')

def load_data():
    files = sorted(glob.glob(os.path.join(DATA_DIR, 'bars_*.csv')))
    data = {}
    names = {}
    for f in files:
        code = os.path.basename(f)[5:-4]
        try:
            df = pd.read_csv(f, index_col=0)
            if df.empty or len(df) < 120:
                continue
            df.index = pd.to_datetime(df.index, utc=True).tz_convert('Asia/Seoul')
            data[code] = df.sort_index()
            names[code] = code
        except Exception:
            continue
    sessions = sorted({t.date() for df in data.values() for t in df.index})
    return data, names, sessions

def compute_daily_features_and_labels(data, sessions):
    """
    각 종목의 60분봉으로부터:
    1. 일봉 계산
    2. 일봉 기준 16개 피처 계산 (당일 14:00 완료 시점)
    3. 라벨 계산: 익일 09:00 시가 진입 후 5거래일 내 +10% 익절 먼저 도달시 1, 아니면 0
    """
    all_rows = []
    
    for code, df in data.items():
        daily_bars = []
        for date, group in df.groupby(df.index.date):
            if len(group) == 0:
                continue
            daily_bars.append({
                'Date': date,
                'Open': group['Open'].iloc[0],
                'High': group['High'].max(),
                'Low': group['Low'].min(),
                'Close': group['Close'].iloc[-1],
                'Volume': group['Volume'].sum(),
                'Last_Bar_Time': group.index[-1]
            })
        ddf = pd.DataFrame(daily_bars).set_index('Date').sort_index()
        if len(ddf) < 25:
            continue
            
        c, h, l, o, v = ddf['Close'], ddf['High'], ddf['Low'], ddf['Open'], ddf['Volume']
        
        # 피처 엔지니어링 (Lookahead 방지: t 시점 데이터만 사용)
        ret_1d = c.pct_change(1)
        ret_3d = c.pct_change(3)
        ret_5d = c.pct_change(5)
        v_ma5 = v.rolling(5).mean()
        v_ma20 = v.rolling(20).mean()
        vol_ratio_5 = v / v_ma5.replace(0, np.nan)
        vol_ratio_20 = v / v_ma20.replace(0, np.nan)
        vol_change = v / v.shift(1).replace(0, np.nan)
        range_ratio = (h - l) / c.replace(0, np.nan)
        
        # 볼린저 밴드
        bb_mid = c.rolling(20).mean()
        bb_std = c.rolling(20).std()
        bb_upper = bb_mid + bb_std * 2
        bb_lower = bb_mid - bb_std * 2
        bb_pos = (c - bb_lower) / (bb_upper - bb_lower).replace(0, np.nan)
        bb_width = (bb_upper - bb_lower) / bb_mid.replace(0, np.nan)
        
        # 이평선
        ma5 = c.rolling(5).mean()
        ma20 = c.rolling(20).mean()
        ma60 = c.rolling(min(len(ddf), 60), min_periods=5).mean()
        ma_max = pd.concat([ma5, ma20, ma60], axis=1).max(axis=1)
        ma_min = pd.concat([ma5, ma20, ma60], axis=1).min(axis=1)
        ma_spread = (ma_max - ma_min) / ma20.replace(0, np.nan)
        above_ma20 = (c > ma20).astype(float)
        above_ma60 = (c > ma60).astype(float)
        
        # 신고가/신저가
        high_20 = h.rolling(20).max()
        low_20 = l.rolling(20).min()
        dist_to_high20 = c / high_20.replace(0, np.nan)
        dist_to_low20 = c / low_20.replace(0, np.nan)
        
        # RSI 14
        delta = c.diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi_14 = 100 - (100 / (1 + rs))
        
        # 라벨 생성: 익일 09:00 시가 진입 후 5거래일 내 +10% 도달 여부
        for t in range(20, len(ddf)):
            d = ddf.index[t]
            s_idx = sessions.index(d) if d in sessions else -1
            if s_idx < 0 or s_idx >= len(sessions) - 1:
                continue
                
            next_day = sessions[s_idx + 1]
            entry_ts = pd.Timestamp(f"{next_day} 09:00", tz='Asia/Seoul')
            
            if entry_ts not in df.index:
                continue
            entry_price = float(df.loc[entry_ts, 'Open'])
            if entry_price <= 0:
                continue
                
            # 향후 5개 세션(또는 데이터 끝) 동안의 봉 탐색
            target_price = entry_price * 1.10
            stop_price = entry_price * 0.95
            
            future_bars = df[(df.index >= entry_ts) & (df.index.date <= sessions[min(s_idx + 5, len(sessions) - 1)])]
            
            outcome = 0 # 기본 실패 (0)
            for bar_ts, bar in future_bars.iterrows():
                # 손절 먼저 체크 (보수적)
                if bar.Low <= stop_price:
                    outcome = 0
                    break
                if bar.High >= target_price:
                    outcome = 1 # 성공
                    break
                    
            all_rows.append({
                'code': code,
                'date': d,
                'session_idx': s_idx,
                'scan_ts': str(entry_ts),
                'ret_1d': ret_1d.iloc[t],
                'ret_3d': ret_3d.iloc[t],
                'ret_5d': ret_5d.iloc[t],
                'vol_ratio_5': vol_ratio_5.iloc[t],
                'vol_ratio_20': vol_ratio_20.iloc[t],
                'vol_change': vol_change.iloc[t],
                'range_ratio': range_ratio.iloc[t],
                'bb_pos': bb_pos.iloc[t],
                'bb_width': bb_width.iloc[t],
                'ma_spread': ma_spread.iloc[t],
                'above_ma20': above_ma20.iloc[t],
                'above_ma60': above_ma60.iloc[t],
                'dist_to_high20': dist_to_high20.iloc[t],
                'dist_to_low20': dist_to_low20.iloc[t],
                'rsi_14': rsi_14.iloc[t],
                'label': outcome
            })
            
    df_samples = pd.DataFrame(all_rows).dropna()
    return df_samples

def run_ml_experiment(df_samples, data, sessions):
    feature_cols = [
        'ret_1d', 'ret_3d', 'ret_5d',
        'vol_ratio_5', 'vol_ratio_20', 'vol_change',
        'range_ratio', 'bb_pos', 'bb_width',
        'ma_spread', 'above_ma20', 'above_ma60',
        'dist_to_high20', 'dist_to_low20', 'rsi_14'
    ]
    
    # 60개 세션 중 앞의 10거래일 = Train, 뒤의 50거래일 = Test
    # 실질적으로 t=20부터 시작하므로:
    # Train 세션: sessions[20:30] (10일간의 학습 데이터)
    # Test 세션: sessions[35:] (embargo 5세션 이후 25거래일의 OOS 실전 데이터)
    # [T-049] 라벨이 진입 후 5거래일을 보므로 Train 마지막 세션의 라벨 창이 Test에 닿는다 -> 5세션 embargo
    train_dates = set(sessions[20:30])
    test_dates = set(sessions[35:])
    
    train_df = df_samples[df_samples['date'].isin(train_dates)].copy()
    test_df = df_samples[df_samples['date'].isin(test_dates)].copy()
    
    print(f"=== 데이터 분할 ===")
    print(f"Train 기간: {min(train_dates)} ~ {max(train_dates)} (10거래일)")
    print(f"  - Train 표본 수: {len(train_df):,}건 (성공 라벨 1: {(train_df['label']==1).sum()}건, {train_df['label'].mean()*100:.1f}%)")
    print(f"Test 기간: {min(test_dates)} ~ {max(test_dates)} ({len(test_dates)}거래일)")
    print(f"  - Test 표본 수: {len(test_df):,}건 (성공 라벨 1: {(test_df['label']==1).sum()}건, {test_df['label'].mean()*100:.1f}%)\n")
    
    X_train, y_train = train_df[feature_cols], train_df['label']
    X_test, y_test = test_df[feature_cols], test_df['label']
    
    models = {
        '1. Extreme Overfit Tree (과적합 끝판왕)': DecisionTreeClassifier(
            max_depth=None, min_samples_split=2, min_samples_leaf=1, random_state=42
        ),
        '2. Overfit Random Forest (복잡도 무제한 RF)': RandomForestClassifier(
            n_estimators=50, max_depth=None, min_samples_split=2, min_samples_leaf=1, random_state=42
        ),
        '3. Regularized Boost (규제 적용 HGB)': HistGradientBoostingClassifier(
            max_depth=3, min_samples_leaf=20, l2_regularization=2.0, random_state=42
        )
    }
    
    results = []
    
    for name, model in models.items():
        print(f"[{name}] 학습 중...")
        model.fit(X_train, y_train)
        
        # 모델 예측 확률 또는 결정 점수
        if hasattr(model, 'predict_proba'):
            train_df['score'] = model.predict_proba(X_train)[:, 1]
            test_df['score'] = model.predict_proba(X_test)[:, 1]
        else:
            train_df['score'] = model.predict(X_train).astype(float)
            test_df['score'] = model.predict(X_test).astype(float)
            
        # 백테스트를 위한 엔트리 생성 (매 scan 시각별 score 상위 5개 선정)
        def build_entries(df_subset, min_score=0.5):
            candidates = df_subset[df_subset['score'] >= min_score]
            entries = []
            for scan_ts, group in candidates.groupby('scan_ts'):
                top = group.sort_values(['score', 'code'], ascending=[False, True]).head(5)
                for _, r in top.iterrows():
                    entries.append({'scan': r['scan_ts'], 'code': r['code'], 'name': r['code'], 'score': r['score']})
            return pd.DataFrame(entries) if entries else pd.DataFrame(columns=['scan', 'code', 'name', 'score'])
            
        train_entries = build_entries(train_df, min_score=0.6)
        test_entries = build_entries(test_df, min_score=0.6)
        
        # Train 백테스트
        train_sess_list = sorted(list(train_dates))
        test_sess_list = sorted(list(test_dates))
        
        def run_sim(entries, sess_list, period_name):
            if len(entries) == 0:
                return {'Period': period_name, 'Trades': 0, 'Win_Rate_%': 0.0, 'Total_Return_%': 0.0, 'MDD_%': 0.0, 'Profit_Factor': 0.0}
            res, trades, eq = simulate_account(
                entries=entries, data=data, sessions=sess_list, mode='fixed', fee=0.00175, entry_slippage=0.0005, max_positions=1, exit_slippage=0.0005
            )
            tdf = pd.DataFrame(trades)
            if len(tdf) == 0:
                return {'Period': period_name, 'Trades': 0, 'Win_Rate_%': 0.0, 'Total_Return_%': 0.0, 'MDD_%': 0.0, 'Profit_Factor': 0.0}
            wins = (tdf['pnl'] > 0).sum()
            wr = wins / len(tdf) * 100
            gp = tdf.loc[tdf['pnl'] > 0, 'pnl'].sum()
            gl = abs(tdf.loc[tdf['pnl'] < 0, 'pnl'].sum())
            pf = (gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0)
            final_eq = eq.iloc[-1]['equity'] if len(eq) > 0 else 1_000_000.0
            ret = (final_eq / 1_000_000.0 - 1) * 100
            mdd = abs(((eq['equity'] - eq['equity'].cummax()) / eq['equity'].cummax()).min()) * 100 if len(eq) > 0 else 0.0
            return {
                'Period': period_name,
                'Trades': len(tdf),
                'Win_Rate_%': round(wr, 1),
                'Total_Return_%': round(ret, 2),
                'Profit_Factor': round(pf, 2),
                'MDD_%': round(mdd, 2)
            }
            
        perf_train = run_sim(train_entries, train_sess_list, 'Train (10일 학습)')
        perf_test = run_sim(test_entries, test_sess_list, 'Test (미래 OOS)')
        
        results.append({
            'Model': name,
            'Train_Trades': perf_train['Trades'],
            'Train_WinRate_%': perf_train['Win_Rate_%'],
            'Train_Return_%': perf_train['Total_Return_%'],
            'Test_Trades': perf_test['Trades'],
            'Test_WinRate_%': perf_test['Win_Rate_%'],
            'Test_Return_%': perf_test['Total_Return_%'],
            'Test_PF': perf_test['Profit_Factor'],
            'Test_MDD_%': perf_test['MDD_%']
        })
        
    df_res = pd.DataFrame(results)
    print("\n=========================================================================================")
    print("[RESULT] ML Overfitting vs Generalization Battle (10일 학습 -> 30~50일 판정)")
    print("=========================================================================================")
    print(df_res.to_string(index=False))
    
    out_md = os.path.join(ROOT, 'data', 'research', 'T-043_ml_overfit_report_T049.md')
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write("# T-043 ML 과적합(Overfitting) vs 일반화(Generalization) 백테스트 보고서\n\n")
        f.write(df_res.to_markdown(index=False))
        f.write("\n")
    print(f"\nReport saved to: {out_md}")
    return df_res

if __name__ == '__main__':
    print("349개 종목 60일 데이터 로딩 중...")
    data, names, sessions = load_data()
    print("피처 및 라벨 생성 중 (수치 안정성 및 누수 엄격 차단)...")
    df_samples = compute_daily_features_and_labels(data, sessions)
    run_ml_experiment(df_samples, data, sessions)
