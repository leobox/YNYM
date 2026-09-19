"""T-042: 실증 분석으로 도출한 급등 전조(Precursor) 기반 신규 전략 백테스트.

전략 구성:
1. Strategy A (TrendPrecursor, 추세 시동 돌파형):
   - 주가 > 20일선
   - 직전 3일간 +3% ~ +12% 완만한 우상향
   - 20일 최고가 대비 -12% 이내 (고점 턱밑 압축)
   - 전일 거래량 >= 20일 평균 거래량
2. Strategy B (PanicReversal, 공포 투매 후 바닥 반등형):
   - 주가 < 20일선
   - 최근 3일간 -5% 이하 급락
   - 20일 최고가 대비 -22% 이하 낙폭과대
   - 전일 거래량 >= 20일 평균 거래량 (바닥 손바뀜 유입)
3. Baseline:
   - 기존 hourly_pattern 베이스라인

실행 조건:
- 349개 종목 60거래일 (live_60d_20260918)
- 수수료: 0.175%, 슬리피지: 0.05%
- 익절 +10%, 손절 -5%, 최대 5영업일 보유
- 단일 포지션(1개) 및 다중 포지션(3개) 비교
"""
import glob
import os
import sys
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research.account_simulator import simulate_account
from scanner.pattern import hourly_pattern

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

def generate_precursor_signals(data, sessions, strategy_type='trend'):
    """
    일봉 기반 전조 피처를 계산하여,
    각 날짜의 14:00(장마감 완료 시점)에 신호를 발생시킴.
    진입은 다음 거래일 09:00 시가 체결.
    """
    rows = []
    
    for code, df in data.items():
        # 일봉 리샘플링
        daily_rows = []
        for date, group in df.groupby(df.index.date):
            if len(group) == 0:
                continue
            daily_rows.append({
                'Date': date,
                'Open': group['Open'].iloc[0],
                'High': group['High'].max(),
                'Low': group['Low'].min(),
                'Close': group['Close'].iloc[-1],
                'Volume': group['Volume'].sum(),
                'Last_Bar_Time': group.index[-1]
            })
        ddf = pd.DataFrame(daily_rows).set_index('Date').sort_index()
        if len(ddf) < 25:
            continue
            
        c = ddf['Close']
        h = ddf['High']
        l = ddf['Low']
        v = ddf['Volume']
        
        ma20 = c.rolling(20).mean()
        v_ma20 = v.rolling(20).mean()
        high_20 = h.rolling(20).max()
        ret_3d = c / c.shift(3) - 1.0
        dist_to_high20 = c / high_20
        vol_ratio_20 = v / v_ma20.replace(0, np.nan)
        
        for t in range(20, len(ddf)):
            d = ddf.index[t]
            cur_c = c.iloc[t]
            cur_ma20 = ma20.iloc[t]
            cur_ret3 = ret_3d.iloc[t]
            cur_dist = dist_to_high20.iloc[t]
            cur_vr = vol_ratio_20.iloc[t]
            
            signal = False
            score = 0.0
            
            if strategy_type == 'trend':
                # Strategy A: Trend Precursor
                # 20일선 위, 3일간 +2%~+15% 점진적 상승, 20일 최고가 -12% 이내 근접, 거래량 1배 이상
                if (cur_c > cur_ma20 and 
                    0.02 <= cur_ret3 <= 0.15 and 
                    cur_dist >= 0.88 and 
                    cur_vr >= 1.0):
                    signal = True
                    # 점수: 최고가 근접도 + 거래량 배수
                    score = float(cur_dist * 50 + min(cur_vr, 3.0) * 20)
                    
            elif strategy_type == 'reversal':
                # Strategy B: Panic Reversal
                # 20일선 아래, 3일간 -5% 이하 급락, 20일 최고가 대비 -22% 이하 낙폭과대, 거래량 1배 이상 손바뀜
                if (cur_c < cur_ma20 and 
                    cur_ret3 <= -0.05 and 
                    cur_dist <= 0.78 and 
                    cur_vr >= 1.0):
                    signal = True
                    # 점수: 낙폭이 클수록 + 거래량이 많을수록 높은 점수
                    score = float((1.0 - cur_dist) * 100 + min(cur_vr, 3.0) * 20)
                    
            elif strategy_type == 'combined':
                # Strategy A or B
                if (cur_c > cur_ma20 and 0.02 <= cur_ret3 <= 0.15 and cur_dist >= 0.88 and cur_vr >= 1.0):
                    signal = True
                    score = float(cur_dist * 50 + min(cur_vr, 3.0) * 20)
                elif (cur_c < cur_ma20 and cur_ret3 <= -0.05 and cur_dist <= 0.78 and cur_vr >= 1.0):
                    signal = True
                    score = float((1.0 - cur_dist) * 100 + min(cur_vr, 3.0) * 20)

            if signal:
                # 다음 거래일 09:00 시점에 진입하기 위해 scan 시각을 다음 세션 09:00으로 설정
                # d의 다음 세션 찾기
                s_idx = sessions.index(d) if d in sessions else -1
                if 0 <= s_idx < len(sessions) - 1:
                    next_day = sessions[s_idx + 1]
                    scan_ts = pd.Timestamp(f"{next_day} 09:00", tz='Asia/Seoul')
                    rows.append({
                        'scan': str(scan_ts),
                        'code': code,
                        'name': code,
                        'score': score,
                        'signal_date': str(d),
                        'ret_3d': cur_ret3,
                        'dist_to_high': cur_dist,
                        'vol_ratio': cur_vr
                    })
                    
    df_entries = pd.DataFrame(rows)
    if df_entries.empty:
        return pd.DataFrame(columns=['scan', 'code', 'name', 'score'])
    # 각 scan(익일 09:00) 시점별 score 상위 5개 종목만 후보로 선정 (Deterministic Top 5)
    selected_rows = []
    for scan_time, group in df_entries.groupby('scan'):
        top = group.sort_values(['score', 'code'], ascending=[False, True]).head(5)
        selected_rows.append(top)
        
    return pd.concat(selected_rows, ignore_index=True)

def generate_hourly_pattern_signals(data, sessions):
    """기존 hourly_pattern 베이스라인 신호 생성 (매 세션 09:00 진입)"""
    rows = []
    for code, df in data.items():
        out = hourly_pattern(df)
        eligible = out[out['eligible'] == True]
        for ts, r in eligible.iterrows():
            # 다음 관측봉 시가 진입
            loc = df.index.get_loc(ts)
            if loc + 1 < len(df):
                next_ts = df.index[loc + 1]
                rows.append({
                    'scan': str(next_ts),
                    'code': code,
                    'name': code,
                    'score': float(r['score']),
                })
    if not rows:
        return pd.DataFrame(columns=['scan', 'code', 'name', 'score'])
    df_entries = pd.DataFrame(rows)
    selected_rows = []
    for scan_time, group in df_entries.groupby('scan'):
        top = group.sort_values(['score', 'code'], ascending=[False, True]).head(5)
        selected_rows.append(top)
    return pd.concat(selected_rows, ignore_index=True)

def evaluate_strategy(name, entries, data, sessions, max_positions=1):
    if len(entries) == 0:
        return {'Strategy': name, 'Positions': max_positions, 'Total_Return_%': 0.0, 'Trades': 0, 'Win_Rate_%': 0.0}
    
    result, trades, equity = simulate_account(
        entries=entries,
        data=data,
        sessions=sessions,
        mode='fixed',
        fee=0.00175,
        entry_slippage=0.0005, # 0.05% 슬리피지
        max_positions=max_positions
    )
    
    trades_df = pd.DataFrame(trades)
    n_trades = len(trades_df)
    if n_trades == 0:
        win_rate = 0.0
        profit_factor = 0.0
        target_rate = 0.0
        stop_rate = 0.0
        time_rate = 0.0
    else:
        wins = (trades_df['pnl'] > 0).sum()
        win_rate = wins / n_trades * 100
        gross_profit = trades_df.loc[trades_df['pnl'] > 0, 'pnl'].sum()
        gross_loss = abs(trades_df.loc[trades_df['pnl'] < 0, 'pnl'].sum())
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        
        target_rate = (trades_df['reason'] == 'TARGET').mean() * 100
        stop_rate = trades_df['reason'].str.startswith('STOP').mean() * 100
        time_rate = trades_df['reason'].str.startswith('TIME').mean() * 100
        
    final_equity = equity.iloc[-1]['equity'] if len(equity) > 0 else 1_000_000.0
    initial_cash = 1_000_000.0
    total_ret = (final_equity / initial_cash - 1) * 100
    
    # MDD calculation
    if len(equity) > 0:
        eq_series = equity['equity']
        peak = eq_series.cummax()
        dd = (eq_series - peak) / peak
        mdd = abs(dd.min()) * 100
    else:
        mdd = 0.0
        
    return {
        'Strategy': name,
        'Positions': max_positions,
        'Total_Return_%': round(total_ret, 2),
        'Trades': n_trades,
        'Win_Rate_%': round(win_rate, 1),
        'Profit_Factor': round(profit_factor, 2),
        'MDD_%': round(mdd, 2),
        'Target_Rate_%': round(target_rate, 1),
        'Stop_Rate_%': round(stop_rate, 1),
        'Time_Exit_%': round(time_rate, 1),
        'Final_Equity': int(final_equity)
    }

if __name__ == '__main__':
    print("349개 종목 60일 60분봉 데이터 로딩 중...")
    data, names, sessions = load_data()
    print(f"로딩 완료: {len(data)}개 종목, 총 {len(sessions)}개 거래일 세션\n")
    
    print("1. 신호 생성 중...")
    signals_trend = generate_precursor_signals(data, sessions, strategy_type='trend')
    print(f"  - Strategy A (TrendPrecursor): {len(signals_trend)}건 신호")
    
    signals_reversal = generate_precursor_signals(data, sessions, strategy_type='reversal')
    print(f"  - Strategy B (PanicReversal): {len(signals_reversal)}건 신호")
    
    signals_combined = generate_precursor_signals(data, sessions, strategy_type='combined')
    print(f"  - Strategy C (Combined A+B): {len(signals_combined)}건 신호")
    
    signals_baseline = generate_hourly_pattern_signals(data, sessions)
    print(f"  - Baseline (hourly_pattern): {len(signals_baseline)}건 신호\n")
    
    print("2. 계좌 시뮬레이션 백테스트 실행 중 (수수료 0.175%, 슬리피지 0.05%, +10% 익절 / -5% 손절)...")
    results = []
    
    for max_pos in (1, 3):
        print(f"\n--- 포지션 수: {max_pos}개 분할 실행 ---")
        res_trend = evaluate_strategy('Strategy A (TrendPrecursor)', signals_trend, data, sessions, max_pos)
        res_reversal = evaluate_strategy('Strategy B (PanicReversal)', signals_reversal, data, sessions, max_pos)
        res_comb = evaluate_strategy('Strategy C (Combined A+B)', signals_combined, data, sessions, max_pos)
        res_base = evaluate_strategy('Baseline (hourly_pattern)', signals_baseline, data, sessions, max_pos)
        
        results.extend([res_trend, res_reversal, res_comb, res_base])
        
    df_res = pd.DataFrame(results)
    out_md = os.path.join(ROOT, 'data', 'research', 'T-042_backtest_report.md')
    os.makedirs(os.path.dirname(out_md), exist_ok=True)
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write("# T-042 백테스팅 결과 요약\n\n")
        f.write(df_res.to_markdown(index=False))
        f.write("\n")
    print("\n=========================================================================================")
    print("[RESULT] Strategy Backtest Battle Summary")
    print("=========================================================================================")
    print(df_res.to_string(index=False))
    print(f"\nReport saved to: {out_md}")
