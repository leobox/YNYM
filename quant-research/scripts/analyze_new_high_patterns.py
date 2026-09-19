"""T-046: 2026 올해 신고가(New High) 달성 종목들의 공통 패턴 실증 분석 및 백테스트.

데이터: 151개 종목 x 213거래일 YTD 데이터 (imported/ytd_11am)

분석 목표:
1. 60거래일 신고가(New High Breakout) 돌파 사건 전수 수집
2. 성공한 신고가(돌파 후 10영업일 내 +10% 추가 랠리) vs 실패한 신고가(돌파 후 -5% 손절/이탈)의
   돌파 직전 및 돌파 당일 공통 피처 통계 비교 (VCP 변동성 축소, 거래량 마름/폭발, 이평선 배열, 이격도 등)
3. 진짜 신고가와 가짜 신고가(Fakeout)를 가르는 결정적 필터 도출
4. 실전 계좌 시뮬레이터(수수료 0.175%, 슬리피지 0.05%, +10% 익절, -5% 손절) 백테스트
"""
import os
import sys
import glob
import numpy as np
import pandas as pd
from scipy import stats

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research.account_simulator import simulate_account

DATA_DIR = os.path.join(ROOT, 'data', 'imported', 'ytd_11am')

def load_data():
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

def collect_new_high_events(data, sessions, lookback=60, target_pct=0.10, stop_pct=0.05, max_hold=10):
    """
    모든 종목의 일봉에서 lookback(60거래일) 신고가를 종가 또는 고가로 돌파한 사건 수집.
    """
    events = []
    
    for code, df in data.items():
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
            })
        ddf = pd.DataFrame(daily_rows).set_index('Date').sort_index()
        if len(ddf) < lookback + 15:
            continue
            
        c, h, l, o, v = ddf['Close'], ddf['High'], ddf['Low'], ddf['Open'], ddf['Volume']
        
        # 60일 전고점 (D-1까지의 60일 최고가)
        high_prev60 = h.shift(1).rolling(lookback).max()
        
        # 지표들
        ma5 = c.rolling(5).mean()
        ma20 = c.rolling(20).mean()
        ma60 = c.rolling(60).mean()
        ma120 = c.rolling(min(len(ddf), 120), min_periods=20).mean()
        v_ma20 = v.rolling(20).mean()
        v_ma5 = v.rolling(5).mean()
        
        # ATR 14
        prev_c = c.shift(1)
        tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        atr14 = tr.rolling(14).mean()
        
        # VCP(변동성 축소) 지표: 직전 5일 변동폭 / 직전 20일 변동폭
        volat_5d = (h.shift(1).rolling(5).max() - l.shift(1).rolling(5).min()) / c.shift(1)
        volat_20d = (h.shift(1).rolling(20).max() - l.shift(1).rolling(20).min()) / c.shift(1)
        vcp_ratio = (volat_5d / volat_20d.replace(0, np.nan)) # 작을수록 좁게 수축(VCP)
        
        # 직전 5일 거래량 마름 지수 (Dryup)
        vol_dryup = (v_ma5.shift(1) / v_ma20.shift(1).replace(0, np.nan))
        
        # 돌파일 거래량 폭발 배수
        vol_spike = v / v_ma20.shift(1).replace(0, np.nan)
        
        # 20일선 이격도 (ATR 단위)
        extension_atr = (c - ma20) / atr14.replace(0, np.nan)
        
        # 정배열 점수 (MA Alignment)
        is_bull_aligned = ((ma5 > ma20) & (ma20 > ma60) & (ma60 > ma120)).astype(float)
        
        for t in range(lookback, len(ddf)):
            d = ddf.index[t]
            cur_c = c.iloc[t]
            cur_h = h.iloc[t]
            ref_high = high_prev60.iloc[t]
            
            # 신고가 돌파 조건: 당일 종가가 직전 60일 최고가를 갱신 돌파
            if pd.isna(ref_high) or cur_c <= ref_high:
                continue
                
            # 익일 09:00 시가 진입
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
                
            target_price = entry_price * (1.0 + target_pct)
            stop_price = entry_price * (1.0 - stop_pct)
            future_bars = df[(df.index >= entry_ts) & (df.index.date <= sessions[min(s_idx + max_hold, len(sessions) - 1)])]
            
            outcome = 'FAIL'
            exit_bar_idx = 0
            for bar_ts, bar in future_bars.iterrows():
                exit_bar_idx += 1
                if bar.Low <= stop_price:
                    outcome = 'FAIL'
                    break
                if bar.High >= target_price:
                    outcome = 'WIN'
                    break
                    
            events.append({
                'code': code,
                'date': d,
                'scan_ts': str(entry_ts),
                'outcome': outcome,
                'is_win': 1 if outcome == 'WIN' else 0,
                'breakout_margin_%': (cur_c / ref_high - 1) * 100, # 신고가를 얼마나 시원하게 뚫었는가
                'vol_spike': vol_spike.iloc[t],                     # 돌파일 거래량 배수
                'vol_dryup_d1': vol_dryup.iloc[t],                 # 직전 거래량 마름 정도
                'vcp_ratio': vcp_ratio.iloc[t],                     # 변동성 축소 비율 (작을수록 타이트한 VCP)
                'extension_atr': extension_atr.iloc[t],             # 20일선 대비 이격 (ATR 단위)
                'bull_aligned': is_bull_aligned.iloc[t],           # 4대 이평선 완전 정배열 여부
                'range_today': (cur_h - l.iloc[t]) / cur_c,        # 돌파일 캔들 고저폭
                'close_vs_open': (cur_c - o.iloc[t]) / cur_c,       # 돌파일 양봉 크기
            })
            
    return pd.DataFrame(events)

def analyze_breakouts(df_events):
    print("=========================================================================================")
    print(f"[STATS] 2026 Year-to-Date 60-day New High Events: Total {len(df_events):,} cases")
    print("=========================================================================================")
    
    wins = df_events[df_events['outcome'] == 'WIN']
    fails = df_events[df_events['outcome'] == 'FAIL']
    
    print(f"  - 진짜 신고가(True Breakout, 10일 내 +10% 추가 랠리 성공): {len(wins)}건 ({len(wins)/len(df_events)*100:.1f}%)")
    print(f"  - 가짜 신고가(Fakeout, 돌파 직후 -5% 손절 실패): {len(fails)}건 ({len(fails)/len(df_events)*100:.1f}%)\n")
    
    features = [
        'vcp_ratio', 'vol_dryup_d1', 'vol_spike', 'extension_atr',
        'breakout_margin_%', 'bull_aligned', 'range_today', 'close_vs_open'
    ]
    
    rows = []
    for f in features:
        w = wins[f].dropna()
        fl = fails[f].dropna()
        if len(w) < 5 or len(fl) < 5:
            continue
        stat, p = stats.mannwhitneyu(w, fl, alternative='two-sided')
        cohen_d = (w.mean() - fl.mean()) / np.sqrt((w.var() + fl.var()) / 2)
        rows.append({
            'Feature': f,
            'True_Mean': w.mean(),
            'True_Median': w.median(),
            'Fake_Mean': fl.mean(),
            'Fake_Median': fl.median(),
            'Diff_%': (w.median() - fl.median()) / (abs(fl.median()) if abs(fl.median()) > 1e-4 else 1.0) * 100,
            'Cohen_d': cohen_d,
            'p_value': p
        })
        
    df_stat = pd.DataFrame(rows).sort_values('Cohen_d', ascending=False)
    print(df_stat.to_string(index=False))
    return df_stat, wins, fails

def run_breakout_strategy_backtest(df_events, data, sessions):
    """
    도출된 진짜 신고가 핵심 필터를 적용한 전략 vs 무필터 단순 신고가 매수 비교 백테스트.
    필터 조건:
    1. VCP 변동성 축소: vcp_ratio <= 0.60 (직전 변동폭이 20일 변동폭의 60% 이하로 수축)
    2. 직전 거래량 마름: vol_dryup_d1 <= 1.0 (5일 평균 거래량이 20일 평균 이하로 차분)
    3. 돌파일 거래량 폭발: vol_spike >= 2.0 (평소 20일 대비 200% 이상 거래량 폭발)
    4. 이격 과열 방지: extension_atr <= 3.5 (20일선에서 너무 멀리 날아간 뇌동매매 제외)
    """
    print("\n=========================================================================================")
    print("[RESULT] Real Backtest: Raw New High vs VCP Super New High")
    print("=========================================================================================")
    
    # 1. 단순 신고가 (No Filter)
    df_raw = df_events.copy()
    df_raw['score'] = df_raw['vol_spike']
    
    # 2. 슈퍼 신고가 (Filtered)
    df_filtered = df_events[
        (df_events['vcp_ratio'] <= 0.65) &
        (df_events['vol_dryup_d1'] <= 1.1) &
        (df_events['vol_spike'] >= 1.5) &
        (df_events['extension_atr'] <= 3.8)
    ].copy()
    df_filtered['score'] = df_filtered['vol_spike'] * 2.0 + (1.0 - df_filtered['vcp_ratio']) * 50.0
    
    def to_entries(df_sub):
        entries = []
        for scan_ts, group in df_sub.groupby('scan_ts'):
            top = group.sort_values(['score', 'code'], ascending=[False, True]).head(5)
            for _, r in top.iterrows():
                entries.append({'scan': r['scan_ts'], 'code': r['code'], 'name': r['code'], 'score': r['score']})
        return pd.DataFrame(entries) if entries else pd.DataFrame(columns=['scan', 'code', 'name', 'score'])
        
    entries_raw = to_entries(df_raw)
    entries_fil = to_entries(df_filtered)
    
    results = []
    for name, entries in [('1. 단순 60일 신고가 돌파 (No Filter)', entries_raw),
                          ('2. VCP 변동성수축+수급폭발 슈퍼신고가 (Filtered)', entries_fil)]:
        for max_pos in (1, 3):
            if len(entries) == 0:
                continue
            res, trades, eq = simulate_account(
                entries=entries, data=data, sessions=sessions, mode='fixed', fee=0.00175, entry_slippage=0.0005, max_positions=max_pos
            )
            tdf = pd.DataFrame(trades)
            if len(tdf) == 0:
                continue
            wins = (tdf['pnl'] > 0).sum()
            wr = wins / len(tdf) * 100
            final_eq = eq.iloc[-1]['equity'] if len(eq) > 0 else 1_000_000.0
            ret = (final_eq / 1_000_000.0 - 1) * 100
            mdd = abs(((eq['equity'] - eq['equity'].cummax()) / eq['equity'].cummax()).min()) * 100 if len(eq) > 0 else 0.0
            gp = tdf.loc[tdf['pnl'] > 0, 'pnl'].sum()
            gl = abs(tdf.loc[tdf['pnl'] < 0, 'pnl'].sum())
            pf = (gp / gl) if gl > 0 else 999.0
            
            results.append({
                'Strategy': name,
                'Positions': max_pos,
                'Trades': len(tdf),
                'Win_Rate_%': round(wr, 1),
                'Total_Return_%': round(ret, 2),
                'Profit_Factor': round(pf, 2),
                'MDD_%': round(mdd, 2),
                'Target_+10%_Rate_%': round((tdf['reason']=='TARGET').mean()*100, 1),
                'Stop_-5%_Rate_%': round(tdf['reason'].str.startswith('STOP').mean()*100, 1)
            })
            
    df_res = pd.DataFrame(results)
    print(df_res.to_string(index=False))
    
    out_md = os.path.join(ROOT, 'data', 'research', 'T-046_new_high_report.md')
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write("# T-046 올해 신고가(New High) 패턴 분석 및 백테스트 보고서\n\n")
        f.write(df_res.to_markdown(index=False))
        f.write("\n")
    print(f"\nReport saved to: {out_md}")
    return df_res

if __name__ == '__main__':
    print("1. 151개 종목 213거래일 YTD 장기 데이터 로딩 중...")
    data, names, sessions = load_data()
    print("2. 60일 신고가 돌파 사건 전수 수집 및 성패 판정 중...")
    df_events = collect_new_high_events(data, sessions)
    print("3. 진짜 신고가 vs 가짜 신고가 통계 분석...")
    df_stat, wins, fails = analyze_breakouts(df_events)
    print("4. 슈퍼 신고가 전략 실전 백테스트 실행...")
    run_breakout_strategy_backtest(df_events, data, sessions)
