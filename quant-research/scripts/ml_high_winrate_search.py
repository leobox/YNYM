"""T-045: 213일 장기데이터(ytd_11am) 기반 10가지 조건 ML 고승률(80%+) 탐색 및 백테스트.

1. 데이터: 151개 종목 x 213거래일 (2025-11-03 ~ 2026-09-16)
   - Train: 앞 120거래일 (2025-11 ~ 2026-05)
   - Test (OOS): 뒤 93거래일 (2026-05 ~ 2026-09)

2. 10가지 핵심 피처 (10 M/L Features):
   F1: ret_3d (3일 모멘텀 탄력)
   F2: ret_5d (5일 모멘텀 탄력)
   F3: vol_ratio_20 (당일 거래량 / 20일 평균)
   F4: vol_dryup (5일 거래량 / 20일 거래량 마름 지수)
   F5: dist_to_high20 (20일 신고가 턱밑 압축도)
   F6: bb_width (볼린저 밴드 변동성 수축도)
   F7: range_squeeze (고저폭 / 10일 평균 고저폭)
   F8: rsi_14 (14일 RSI - 과열 방지)
   F9: late_vol_ratio (13:00~14:00 장마감 매수세 비중)
   F10: market_breadth (시장 전체 20일선 상회 비율 레짐)

3. 정밀도(Precision) 최적화:
   - 예측 확률 임계값(Threshold)을 상향하여 승률 80%+ 영역 추출
   - 실전 계좌 시뮬레이터(수수료 0.175%, 슬리피지 0.05%) 백테스트
"""
import os
import sys
import glob
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research.account_simulator import simulate_account

DATA_DIR = os.path.join(ROOT, 'data', 'imported', 'ytd_11am')

def load_ytd_data():
    files = sorted(glob.glob(os.path.join(DATA_DIR, 'bars_*.csv')))
    data = {}
    names = {}
    for f in files:
        code = os.path.basename(f)[5:-4]
        try:
            df = pd.read_csv(f, index_col=0)
            if df.empty or len(df) < 200:
                continue
            df.index = pd.to_datetime(df.index, utc=True).tz_convert('Asia/Seoul')
            data[code] = df.sort_index()
            names[code] = code
        except Exception:
            continue
    sessions = sorted({t.date() for df in data.values() for t in df.index})
    return data, names, sessions

def compute_market_breadth(data, sessions):
    daily_closes = {}
    for code, df in data.items():
        daily_closes[code] = df['Close'].groupby(df.index.date).last()
    df_closes = pd.DataFrame(daily_closes).sort_index()
    ma20 = df_closes.rolling(20).mean()
    breadth = (df_closes > ma20).astype(float).mean(axis=1) * 100.0
    return breadth

def extract_10_features_and_labels(data, sessions, breadth, target_pct=0.08, stop_pct=0.04, max_hold=5):
    """
    10개 핵심 피처 계산 및 백테스트 실전 승리 라벨 생성:
    - 익일 09:00 시가 진입
    - target_pct(+8%) 도달시 1 (승리)
    - stop_pct(-4%) 도달시 0 (패배)
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
                'Late_Vol': group['Volume'].iloc[-2:].sum() if len(group) >= 2 else 0
            })
        ddf = pd.DataFrame(daily_bars).set_index('Date').sort_index()
        if len(ddf) < 30:
            continue
            
        c, h, l, o, v = ddf['Close'], ddf['High'], ddf['Low'], ddf['Open'], ddf['Volume']
        
        # 10개 핵심 피처 계산
        # F1: ret_3d
        f1 = c.pct_change(3)
        # F2: ret_5d
        f2 = c.pct_change(5)
        # F3: vol_ratio_20
        v_ma20 = v.rolling(20).mean()
        f3 = v / v_ma20.replace(0, np.nan)
        # F4: vol_dryup (5일 거래량 / 20일 거래량)
        v_ma5 = v.rolling(5).mean()
        f4 = v_ma5 / v_ma20.replace(0, np.nan)
        # F5: dist_to_high20
        h20 = h.rolling(20).max()
        f5 = c / h20.replace(0, np.nan)
        # F6: bb_width
        bb_mid = c.rolling(20).mean()
        bb_std = c.rolling(20).std()
        f6 = (bb_std * 4) / bb_mid.replace(0, np.nan)
        # F7: range_squeeze (고저폭 / 10일 평균)
        rng = (h - l) / c.replace(0, np.nan)
        rng_ma10 = rng.rolling(10).mean()
        f7 = rng / rng_ma10.replace(0, np.nan)
        # F8: rsi_14
        delta = c.diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        f8 = 100 - (100 / (1 + rs))
        # F9: late_vol_ratio (장후반 거래량 비중)
        f9 = ddf['Late_Vol'] / v.replace(0, np.nan)
        
        for t in range(25, len(ddf)):
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
                
            # F10: market_breadth
            f10 = breadth.get(d, 50.0)
            
            # 미래 봉 탐색 (라벨 판정)
            target_price = entry_price * (1.0 + target_pct)
            stop_price = entry_price * (1.0 - stop_pct)
            future_bars = df[(df.index >= entry_ts) & (df.index.date <= sessions[min(s_idx + max_hold, len(sessions) - 1)])]
            
            label = 0
            for bar_ts, bar in future_bars.iterrows():
                if bar.Low <= stop_price:
                    label = 0
                    break
                if bar.High >= target_price:
                    label = 1
                    break
                    
            all_rows.append({
                'code': code,
                'date': d,
                'session_idx': s_idx,
                'scan_ts': str(entry_ts),
                'F1_ret_3d': f1.iloc[t],
                'F2_ret_5d': f2.iloc[t],
                'F3_vol_ratio_20': f3.iloc[t],
                'F4_vol_dryup': f4.iloc[t],
                'F5_dist_to_high20': f5.iloc[t],
                'F6_bb_width': f6.iloc[t],
                'F7_range_squeeze': f7.iloc[t],
                'F8_rsi_14': f8.iloc[t],
                'F9_late_vol_ratio': f9.iloc[t],
                'F10_market_breadth': f10,
                'label': label
            })
            
    df_samples = pd.DataFrame(all_rows).dropna()
    return df_samples

def train_and_evaluate_high_winrate(df_samples, data, sessions):
    feature_cols = [f'F{i}_{name}' for i, name in enumerate([
        'ret_3d', 'ret_5d', 'vol_ratio_20', 'vol_dryup', 'dist_to_high20',
        'bb_width', 'range_squeeze', 'rsi_14', 'late_vol_ratio', 'market_breadth'
    ], 1)]
    
    # Train: Day 25 ~ Day 145 (약 120거래일)
    # Test (OOS): Day 145 ~ 212 (약 68거래일)
    train_dates = set(sessions[25:145])
    test_dates = set(sessions[145:])
    
    train_df = df_samples[df_samples['date'].isin(train_dates)].copy()
    test_df = df_samples[df_samples['date'].isin(test_dates)].copy()
    
    print(f"=== 213거래일 데이터 분할 ===")
    print(f"Train 기간: {min(train_dates)} ~ {max(train_dates)} (약 120거래일)")
    print(f"  - 표본 수: {len(train_df):,}건 (성공 라벨 1: {(train_df['label']==1).sum()}건, {train_df['label'].mean()*100:.1f}%)")
    print(f"Test(OOS) 기간: {min(test_dates)} ~ {max(test_dates)} (약 68거래일)")
    print(f"  - 표본 수: {len(test_df):,}건 (성공 라벨 1: {(test_df['label']==1).sum()}건, {test_df['label'].mean()*100:.1f}%)\n")
    
    X_train, y_train = train_df[feature_cols], train_df['label']
    X_test, y_test = test_df[feature_cols], test_df['label']
    
    # 머신러닝 모델: Random Forest로 10개 조건의 비선형 상호작용 학습
    rf = RandomForestClassifier(n_estimators=100, max_depth=6, min_samples_leaf=10, random_state=42)
    rf.fit(X_train, y_train)
    
    # Feature Importance 출력
    importances = pd.Series(rf.feature_importances_, index=feature_cols).sort_values(ascending=False)
    print("=== 10대 피처 기여도 (Feature Importances) ===")
    for feat, imp in importances.items():
        print(f"  - {feat:20s}: {imp*100:.2f}%")
    print()
    
    # 확률 예측
    train_df['prob'] = rf.predict_proba(X_train)[:, 1]
    test_df['prob'] = rf.predict_proba(X_test)[:, 1]
    
    # 80%+ 고승률을 찾기 위해 예측 확률 Threshold 스윕
    thresholds = [0.40, 0.50, 0.55, 0.60, 0.65]
    
    results = []
    train_sess_list = sorted(list(train_dates))
    test_sess_list = sorted(list(test_dates))
    
    for th in thresholds:
        def get_entries(df_sub):
            cands = df_sub[df_sub['prob'] >= th]
            entries = []
            for scan_ts, group in cands.groupby('scan_ts'):
                top = group.sort_values(['prob', 'code'], ascending=[False, True]).head(5)
                for _, r in top.iterrows():
                    entries.append({'scan': r['scan_ts'], 'code': r['code'], 'name': r['code'], 'score': r['prob']})
            return pd.DataFrame(entries) if entries else pd.DataFrame(columns=['scan', 'code', 'name', 'score'])
            
        tr_entries = get_entries(train_df)
        te_entries = get_entries(test_df)
        
        def run_sim(entries, sess_list, mode='fixed'):
            if len(entries) == 0:
                return 0, 0.0, 0.0, 0.0
            res, trades, eq = simulate_account(
                entries=entries, data=data, sessions=sess_list, mode=mode, fee=0.00175, entry_slippage=0.0005, max_positions=1
            )
            tdf = pd.DataFrame(trades)
            if len(tdf) == 0:
                return 0, 0.0, 0.0, 0.0
            wins = (tdf['pnl'] > 0).sum()
            wr = wins / len(tdf) * 100
            final_eq = eq.iloc[-1]['equity'] if len(eq) > 0 else 1_000_000.0
            ret = (final_eq / 1_000_000.0 - 1) * 100
            mdd = abs(((eq['equity'] - eq['equity'].cummax()) / eq['equity'].cummax()).min()) * 100 if len(eq) > 0 else 0.0
            return len(tdf), round(wr, 1), round(ret, 2), round(mdd, 2)
            
        tr_trades, tr_wr, tr_ret, tr_mdd = run_sim(tr_entries, train_sess_list)
        te_trades, te_wr, te_ret, te_mdd = run_sim(te_entries, test_sess_list)
        
        results.append({
            'Threshold (ML확신도)': f'>= {th:.2f}',
            'Train_Trades': tr_trades,
            'Train_WinRate_%': tr_wr,
            'Train_Return_%': tr_ret,
            'Test_Trades': te_trades,
            'Test_WinRate_%': te_wr,
            'Test_Return_%': te_ret,
            'Test_MDD_%': te_mdd
        })
        
    df_res = pd.DataFrame(results)
    print("=========================================================================================")
    print("[RESULT] 10 Features ML High-Precision Search (Train vs Test OOS)")
    print("=========================================================================================")
    print(df_res.to_string(index=False))
    
    out_md = os.path.join(ROOT, 'data', 'research', 'T-045_high_winrate_report.md')
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write("# T-045 213일 장기데이터 기반 10가지 조건 ML 고승률 탐색 보고서\n\n")
        f.write("### 10대 피처 기여도\n")
        f.write(importances.to_markdown())
        f.write("\n\n### 임계값별 백테스트 결과\n")
        f.write(df_res.to_markdown(index=False))
        f.write("\n")
    print(f"\nReport saved to: {out_md}")

if __name__ == '__main__':
    print("1. 151개 종목 213거래일 YTD 장기 데이터 로딩 중...")
    data, names, sessions = load_ytd_data()
    print(f"  - 로딩 완료: {len(data)}개 종목, 총 {len(sessions)}개 거래일 세션")
    
    print("2. 시장 폭(Market Breadth) 계산 중...")
    breadth = compute_market_breadth(data, sessions)
    
    print("3. 10대 피처 및 라벨 데이터 추출 중 (시간 소요 예상)...")
    df_samples = extract_10_features_and_labels(data, sessions, breadth, target_pct=0.08, stop_pct=0.04)
    print(f"  - 총 유효 표본: {len(df_samples):,}건")
    
    print("4. 머신러닝 학습 및 고승률(80%+) 탐색 백테스트 실행...")
    train_and_evaluate_high_winrate(df_samples, data, sessions)
