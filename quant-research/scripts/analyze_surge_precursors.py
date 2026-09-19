"""T-042: 상승/급등 종목의 전일(D-1) 공통 피처 실증 분석 스크립트.

349개 종목의 60일 실데이터(data/live_60d_20260918)를 바탕으로:
1. 급등일(Surge Day, 당일 종가 +7% 이상 or 장중 고가 +8% 이상) 추출
2. 급등일 직전(D-1)의 거래량, 변동성 수축, 이평선 수렴, 캔들 형태, 박스권 위치 피처 계산
3. 일반일(Normal Day), 급락일(Drop Day)과의 전일 피처 분포 차이 통계 비교
4. 가장 강력한 급등 전조(Precursor) 시그널 발굴
"""
import glob
import os
import sys
import numpy as np
import pandas as pd
from scipy import stats

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, 'data', 'live_60d_20260918')

def load_and_resample_daily(data_dir=DATA_DIR):
    """Load hourly bars and resample to daily bars."""
    files = sorted(glob.glob(os.path.join(data_dir, 'bars_*.csv')))
    daily_data = {}
    hourly_data = {}
    
    for f in files:
        code = os.path.basename(f)[5:-4]
        try:
            df = pd.read_csv(f, index_col=0)
            if df.empty or len(df) < 60:
                continue
            df.index = pd.to_datetime(df.index, utc=True).tz_convert('Asia/Seoul')
            df = df.sort_index()
            
            # Keep hourly data
            hourly_data[code] = df
            
            # Resample to daily OHLCV
            # Group by date
            daily_rows = []
            for date, group in df.groupby(df.index.date):
                if len(group) == 0:
                    continue
                daily_rows.append({
                    'Date': pd.Timestamp(date),
                    'Open': group['Open'].iloc[0],
                    'High': group['High'].max(),
                    'Low': group['Low'].min(),
                    'Close': group['Close'].iloc[-1],
                    'Volume': group['Volume'].sum(),
                    'Amount_approx': (group['Close'] * group['Volume']).sum(),
                    'Close_Vol_Ratio': group['Volume'].iloc[-2:].sum() / max(1.0, group['Volume'].sum()), # 장후반(13:00,14:00) 거래량 비중
                    'Close_Return_Last2': group['Close'].iloc[-1] / group['Open'].iloc[-2] - 1 if len(group) >= 2 else 0 # 장후반 수익률
                })
            ddf = pd.DataFrame(daily_rows).set_index('Date').sort_index()
            if len(ddf) >= 20:
                daily_data[code] = ddf
        except Exception as e:
            continue
            
    return hourly_data, daily_data

def extract_features_and_labels(daily_data):
    """
    각 종목의 각 일자(t)에 대해:
    - Label(t): 당일(t)의 종가 수익률 및 장중 최대 상승률
    - Features(t): 전일(t-1)까지의 정보만 사용한 피처들 (Lookahead bias 엄격 방지)
    """
    records = []
    
    for code, df in daily_data.items():
        if len(df) < 25:
            continue
            
        c = df['Close']
        h = df['High']
        l = df['Low']
        o = df['Open']
        v = df['Volume']
        
        # 일일 수익률 및 장중 고가 상승률
        prev_c = c.shift(1)
        ret_close = (c - prev_c) / prev_c # 당일 종가 상승률
        ret_high = (h - o) / o           # 당일 시가 대비 장중 고가 상승률
        ret_open = (o - prev_c) / prev_c  # 시가 갭
        
        # 전일(D-1) 기준 피처 계산 (모두 shift(1) 적용하여 D-1 시점의 데이터로 만듦)
        # 1. 거래량 관련 피처
        v_ma5 = v.rolling(5).mean()
        v_ma20 = v.rolling(20).mean()
        vol_ratio_5 = (v / v_ma5.replace(0, np.nan)).shift(1)      # D-1 거래량 / 5일 평균 거래량
        vol_ratio_20 = (v / v_ma20.replace(0, np.nan)).shift(1)    # D-1 거래량 / 20일 평균 거래량
        vol_change_d1 = (v / v.shift(1).replace(0, np.nan)).shift(1) # D-1 거래량 / D-2 거래량
        
        # 2. 변동성 수축(Volatility Squeeze) 관련 피처
        daily_range = (h - l) / c
        range_ma10 = daily_range.rolling(10).mean()
        range_squeeze = (daily_range / range_ma10.replace(0, np.nan)).shift(1) # D-1 변동성 / 10일 평균 변동성
        
        # 볼린저 밴드 폭 (20일, 2표준편차)
        bb_mid = c.rolling(20).mean()
        bb_std = c.rolling(20).std()
        bb_width = ((bb_std * 4) / bb_mid.replace(0, np.nan)).shift(1) # D-1 볼린저 밴드 폭
        bb_width_min20 = (bb_width / bb_width.rolling(20).min()).shift(1) # D-1 밴드폭이 20일 최저 대비 얼마나 좁은가
        
        # 3. 이평선 수렴 및 추세 (MA Alignment)
        ma5 = c.rolling(5).mean()
        ma20 = c.rolling(20).mean()
        ma60 = c.rolling(min(len(df), 60), min_periods=10).mean()
        
        ma_max = pd.concat([ma5, ma20, ma60], axis=1).max(axis=1)
        ma_min = pd.concat([ma5, ma20, ma60], axis=1).min(axis=1)
        ma_spread = ((ma_max - ma_min) / ma20.replace(0, np.nan)).shift(1) # 5-20-60 이평선 밀집도 (작을수록 밀집)
        
        above_ma20 = (c > ma20).astype(float).shift(1)
        above_ma60 = (c > ma60).astype(float).shift(1)
        bull_aligned = ((ma5 > ma20) & (ma20 > ma60)).astype(float).shift(1)
        
        # 4. 박스권 및 신고가 근접도
        high_20 = h.rolling(20).max()
        dist_to_high20 = (c / high_20.replace(0, np.nan)).shift(1) # D-1 종가 / 20일 최고가 (1에 가까울수록 신고가 근접)
        
        # 5. 전일 캔들 형태
        body = (c - o).abs()
        candle_range = (h - l).replace(0, np.nan)
        doji_score = (1.0 - (body / candle_range)).shift(1) # 1에 가까울수록 십자형(도지)
        is_bull_d1 = (c > o).astype(float).shift(1)         # D-1이 양봉인가
        lower_shadow = (pd.concat([o, c], axis=1).min(axis=1) - l) / candle_range
        lower_shadow_d1 = lower_shadow.shift(1)             # D-1 아랫꼬리 비율
        
        # 6. 전일 장후반 특징
        late_vol_ratio = df['Close_Vol_Ratio'].shift(1)
        late_return = df['Close_Return_Last2'].shift(1)
        
        # 7. 최근 3일 누적 수익률 (눌림목 여부)
        ret_3d = (c.shift(1) / c.shift(4) - 1)
        
        # 데이터프레임 결합
        for t in range(20, len(df)):
            date = df.index[t]
            r_c = ret_close.iloc[t]
            r_h = ret_high.iloc[t]
            r_o = ret_open.iloc[t]
            
            # 레이블 분류:
            # 1. 급등 (Surge): 당일 종가 +7% 이상 OR 장중 고가 +10% 이상
            # 2. 일반 (Normal): 종가 -1.5% ~ +1.5%
            # 3. 하락 (Drop): 종가 -4% 이하
            is_surge = (r_c >= 0.07) or (r_h >= 0.09)
            is_normal = (-0.015 <= r_c <= 0.015)
            is_drop = (r_c <= -0.04)
            
            label = 'surge' if is_surge else ('drop' if is_drop else ('normal' if is_normal else 'other'))
            
            records.append({
                'code': code,
                'date': date,
                'label': label,
                'ret_close': r_c,
                'ret_high': r_h,
                'ret_open': r_o,
                'vol_ratio_5': vol_ratio_5.iloc[t],
                'vol_ratio_20': vol_ratio_20.iloc[t],
                'vol_change_d1': vol_change_d1.iloc[t],
                'range_squeeze': range_squeeze.iloc[t],
                'bb_width': bb_width.iloc[t],
                'ma_spread': ma_spread.iloc[t],
                'above_ma20': above_ma20.iloc[t],
                'above_ma60': above_ma60.iloc[t],
                'bull_aligned': bull_aligned.iloc[t],
                'dist_to_high20': dist_to_high20.iloc[t],
                'doji_score': doji_score.iloc[t],
                'is_bull_d1': is_bull_d1.iloc[t],
                'lower_shadow_d1': lower_shadow_d1.iloc[t],
                'late_vol_ratio': late_vol_ratio.iloc[t],
                'late_return': late_return.iloc[t],
                'ret_3d_before': ret_3d.iloc[t],
            })
            
    return pd.DataFrame(records)

def analyze_patterns(df_features):
    """급등일 vs 일반일 vs 하락일의 피처 통계 비교 분석."""
    feature_cols = [
        'vol_ratio_5', 'vol_ratio_20', 'vol_change_d1',
        'range_squeeze', 'bb_width', 'ma_spread',
        'above_ma20', 'above_ma60', 'bull_aligned',
        'dist_to_high20', 'doji_score', 'is_bull_d1',
        'lower_shadow_d1', 'late_vol_ratio', 'late_return',
        'ret_3d_before'
    ]
    
    surge = df_features[df_features['label'] == 'surge']
    normal = df_features[df_features['label'] == 'normal']
    drop = df_features[df_features['label'] == 'drop']
    
    print(f"=== 데이터셋 표본 현황 ===")
    print(f"전체 관측 표본(종목-일자): {len(df_features):,}건")
    print(f"  - 급등일 (Surge, 종가+7% or 고가+9%): {len(surge):,}건 ({len(surge)/len(df_features)*100:.2f}%)")
    print(f"  - 일반일 (Normal, -1.5% ~ +1.5%): {len(normal):,}건 ({len(normal)/len(df_features)*100:.2f}%)")
    print(f"  - 급락일 (Drop, 종가 -4% 이하): {len(drop):,}건 ({len(drop)/len(df_features)*100:.2f}%)")
    print()
    
    summary_rows = []
    
    for col in feature_cols:
        s_vals = surge[col].dropna()
        n_vals = normal[col].dropna()
        
        if len(s_vals) < 10 or len(n_vals) < 10:
            continue
            
        s_mean, s_med = s_vals.mean(), s_vals.median()
        n_mean, n_med = n_vals.mean(), n_vals.median()
        
        # Mann-Whitney U test (비모수 차이 검정)
        stat, p_val = stats.mannwhitneyu(s_vals, n_vals, alternative='two-sided')
        
        # Cohen's d (효과 크기)
        pooled_std = np.sqrt(((len(s_vals)-1)*s_vals.var() + (len(n_vals)-1)*n_vals.var()) / (len(s_vals) + len(n_vals) - 2))
        cohen_d = (s_mean - n_mean) / (pooled_std if pooled_std > 0 else 1.0)
        
        summary_rows.append({
            'Feature': col,
            'Surge_Mean': s_mean,
            'Surge_Median': s_med,
            'Normal_Mean': n_mean,
            'Normal_Median': n_med,
            'Diff_%': (s_med - n_med) / (abs(n_med) if abs(n_med) > 1e-4 else 1.0) * 100,
            'Cohen_d': cohen_d,
            'p_value': p_val
        })
        
    df_summary = pd.DataFrame(summary_rows)
    # Cohen's d 절대값 순으로 정렬 (차이가 뚜렷한 상위 피처)
    df_summary['abs_effect'] = df_summary['Cohen_d'].abs()
    df_summary = df_summary.sort_values('abs_effect', ascending=False)
    
    return df_summary, surge, normal, drop

if __name__ == '__main__':
    print("349개 종목 60일 데이터 로딩 및 일봉 리샘플링 중...")
    hourly_data, daily_data = load_and_resample_daily()
    print(f"로딩 완료: {len(daily_data)}개 종목 유효 일봉 데이터 생성")
    
    print("전일(D-1) 피처 및 당일(D-0) 레이블 추출 중 (Lookahead 방지 엄격 적용)...")
    df_features = extract_features_and_labels(daily_data)
    
    df_summary, surge, normal, drop = analyze_patterns(df_features)
    
    print("\n=== 급등 종목 전일(D-1) 공통점 통계 분석 결과 ===")
    print(df_summary.to_string(index=False))
    
    # 상위 유의미 피처 상세 해석 출력
    print("\n=== 핵심 발견 (Top Precursors) ===")
    top_features = df_summary.head(6)
    for _, row in top_features.iterrows():
        feat = row['Feature']
        p_str = "< 0.001" if row['p_value'] < 0.001 else f"{row['p_value']:.4f}"
        print(f"[{feat}] Cohen's d = {row['Cohen_d']:+.3f} (p={p_str})")
        print(f"  - 급등 전일: 평균 {row['Surge_Mean']:.3f}, 중앙값 {row['Surge_Median']:.3f}")
        print(f"  - 일반 전일: 평균 {row['Normal_Mean']:.3f}, 중앙값 {row['Normal_Median']:.3f}")
        print(f"  - 차이율: {row['Diff_%']:+.1f}%\n")
