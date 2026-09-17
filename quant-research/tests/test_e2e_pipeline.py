import numpy as np
import pandas as pd
import pytest

from scanner.pattern import hourly_pattern
from scanner.breakout import confirmed_breakout
from scanner.selector import select_top, build_watch
from research.virtual_execution import simulate_trade
from research.account_simulator import simulate_account

class TestPatternEngineIntegration:
    def test_pattern_on_uptrend(self, sample_ohlcv):
        out = hourly_pattern(sample_ohlcv)
        assert 'score' in out.columns
        assert 'eligible' in out.columns
        assert out['score'].notna().any()

    def test_pattern_on_flat_data(self, kst_index):
        # Create flat price data, verify no eligible signals (falling chart check)
        df = pd.DataFrame({
            "Open": 5000.0,
            "High": 5050.0,
            "Low": 4950.0,
            "Close": 5000.0,
            "Volume": 100000.0,
        }, index=kst_index)
        out = hourly_pattern(df)
        assert not out['eligible'].any()

    def test_pattern_minimum_bars(self, kst_index):
        # Verify pattern works with exactly 120 bars
        df = pd.DataFrame({
            "Open": 5000.0 + np.arange(120),
            "High": 5050.0 + np.arange(120),
            "Low": 4950.0 + np.arange(120),
            "Close": 5000.0 + np.arange(120),
            "Volume": 100000.0,
        }, index=kst_index[:120])
        out = hourly_pattern(df)
        assert len(out) == 120
        # No crash means success

    def test_pattern_nan_handling(self, sample_ohlcv):
        # Inject NaN into Close at a few points, verify eligible is False for those
        df = sample_ohlcv.copy()
        df.loc[df.index[150], 'Close'] = np.nan
        out = hourly_pattern(df)
        assert out.loc[df.index[150], 'eligible'] == False

class TestBreakoutIntegration:
    def test_breakout_produces_columns(self, breakout_bars):
        out = confirmed_breakout(breakout_bars)
        expected_cols = [
            'hold', 'previous_breakout', 'previous_liquid', 'previous_trigger', 
            'waiting', 'hold_price', 'current_amount', 'current_ratio', 
            'current_level', 'trigger_amount', 'trigger_ratio', 'breakout_level'
        ]
        for col in expected_cols:
            assert col in out.columns

    def test_breakout_no_crash_on_short_data(self, kst_index):
        # 50 bars should not crash, just produce False flags
        df = pd.DataFrame({
            "Open": 5000.0,
            "High": 5050.0,
            "Low": 4950.0,
            "Close": 5000.0,
            "Volume": 100000.0,
        }, index=kst_index[:50])
        out = confirmed_breakout(df)
        assert not out['hold'].any()

    def test_breakout_level_calculation(self, breakout_bars):
        # Verify breakout_level equals High.shift(1).rolling(6).max() shifted by 1
        out = confirmed_breakout(breakout_bars)
        expected = breakout_bars.High.shift(1).rolling(6).max().shift(1)
        pd.testing.assert_series_equal(out['breakout_level'], expected, check_names=False)

class TestEndToEndScanFlow:
    def test_score_function_integration(self, kst_index):
        # Create a synthetic OHLCV dataset designed to pass all pattern conditions
        df = pd.DataFrame({
            "Open": 5000.0 + np.arange(len(kst_index)) * 10,
            "High": 5050.0 + np.arange(len(kst_index)) * 10,
            "Low": 4950.0 + np.arange(len(kst_index)) * 10,
            "Close": 5000.0 + np.arange(len(kst_index)) * 10,
            "Volume": 1000000.0 + np.arange(len(kst_index)) * 10000,
        }, index=kst_index)
        
        # Ensure gap.shift(1).rolling(20).min() < 0 by making a dip early on
        df.loc[df.index[30:50], 'Close'] = 4000.0
        
        p_out = hourly_pattern(df)
        b_out = confirmed_breakout(df)
        
        assert len(p_out) == len(df)
        assert len(b_out) == len(df)
        assert 'score' in p_out.columns
        assert 'hold' in b_out.columns

    def test_full_pipeline_stub(self, sample_ohlcv, kst_index):
        p_out = hourly_pattern(sample_ohlcv)
        b_out = confirmed_breakout(sample_ohlcv)
        last = sample_ohlcv.iloc[-1]
        p_last = p_out.iloc[-1]
        b_last = b_out.iloc[-1]
        
        row_base = {
            '종목': '테스트',
            '코드': '000000',
            '점수': float(p_last['score']) if not pd.isna(p_last['score']) else 0.0,
            '기준봉(KST)': sample_ohlcv.index[-1].strftime('%Y-%m-%d %H:%M'),
            '_match': bool(b_last['hold']),
            '_base': True,
            '_waiting': bool(b_last['waiting']),
            '_current_amount': float(b_last['current_amount']) if not pd.isna(b_last['current_amount']) else 0.0,
            '_current_ratio': float(b_last['current_ratio']) if not pd.isna(b_last['current_ratio']) else 0.0,
            '_current_level': float(b_last['current_level']) if not pd.isna(b_last['current_level']) else 0.0,
            '_current_close': float(last['Close']),
        }
        
        rows = []
        for i in range(10):
            r = row_base.copy()
            r['코드'] = f'{i:06d}'
            if i < 3:
                r['_match'] = True
            rows.append(r)
            
        top5 = select_top(rows, top_n=5)
        assert len(top5) <= 5
        
        watch = build_watch(rows)
        assert isinstance(watch, pd.DataFrame)

class TestBacktestIntegration:
    def test_signal_to_execution_flow(self, sample_ohlcv, sessions_list):
        p_out = hourly_pattern(sample_ohlcv)
        # Find any bar to enter
        entry_ts = sample_ohlcv.index[50]
        final_day = sessions_list[-1]
        res = simulate_trade(sample_ohlcv, entry_ts, final_day, sessions_list, final_hour=14)
        assert 'status' in res
        assert 'hit' in res

    def test_account_simulation_conservation(self, sample_ohlcv, sessions_list):
        # Run account_simulator with known trades, verify cash conservation
        entry_ts = sample_ohlcv.index[50]
        entries = pd.DataFrame({
            'scan': [str(entry_ts)],
            'code': ['000000'],
            'score': [90.0]
        })
        data = {'000000': sample_ohlcv}
        res, trades, equity = simulate_account(entries, data, sessions_list, mode='time')
        
        # Verify cash conservation
        assert 'ending_cash' in res
        # If open_position is False, ending cash should match equity
        if not res['open_position']:
            assert abs(res['ending_cash'] - res['ending_equity']) < 1.0
