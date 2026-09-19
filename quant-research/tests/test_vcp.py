"""Unit tests for scanner.vcp (VCP Pattern Detection Engine)."""
import numpy as np
import pandas as pd
import pytest

from scanner.vcp import detect_vcp

def _generate_vcp_series(n_bars=150):
    """
    Synthesize 150 bars of bullish VCP contraction + breakout.
    Stage 1: Base uptrend (0-100 bars)
    Stage 2: Contraction waves (100-145 bars)
    Stage 3: Pivot Breakout with 3x Volume (146-150 bars)
    """
    dates = pd.date_range("2026-05-01 09:00", periods=n_bars, freq="h", tz="Asia/Seoul")
    
    # 1. Base uptrend
    price = 10000 + np.linspace(0, 5000, n_bars)
    
    # Add narrowing oscillation at bars 100-145 (VCP)
    for i in range(100, 145):
        wave_scale = max(10, 500 * (1.0 - (i - 100) / 45.0)) # narrowing wave
        price[i] += np.sin(i * 0.5) * wave_scale
        
    # Bar 148: Breakout above prior peak
    pivot = price[100:145].max()
    price[148] = pivot + 300
    price[149] = pivot + 500
    
    volume = np.full(n_bars, 100000.0)
    # Dry-up right before breakout (bars 135-147)
    volume[135:148] = 30000.0
    # Breakout volume spike (bar 148)
    volume[148] = 350000.0
    volume[149] = 250000.0
    
    df = pd.DataFrame({
        'Open': price - 50,
        'High': price + 100,
        'Low': price - 100,
        'Close': price,
        'Volume': volume
    }, index=dates)
    
    df.loc[df.index[148], 'Open'] = pivot - 100
    df.loc[df.index[148], 'Close'] = pivot + 300
    df.loc[df.index[148], 'High'] = pivot + 350
    df.loc[df.index[148], 'Low'] = pivot - 120
    
    return df

class TestVCPDetection:
    def test_output_columns_and_length(self, sample_ohlcv):
        out = detect_vcp(sample_ohlcv)
        assert len(out) == len(sample_ohlcv)
        expected_cols = [
            'eligible', 'score', 'vcp_ratio', 'vcp_stage',
            'vol_spike', 'vol_dryup', 'pivot_level', 'extension_atr', 'bull_aligned'
        ]
        for c in expected_cols:
            assert c in out.columns
            
    def test_ideal_vcp_breakout(self):
        df = _generate_vcp_series()
        out = detect_vcp(df)
        
        # Breakout bar 148 should trigger eligible
        breakout_row = out.iloc[148]
        assert breakout_row['bull_aligned'] == True
        assert breakout_row['vol_spike'] >= 2.0
        assert breakout_row['score'] > 50.0
        assert breakout_row['eligible'] == True

    def test_rejects_downtrend(self, sample_ohlcv):
        # Invert prices to create downtrend
        df = sample_ohlcv.copy()
        df['Close'] = 20000 - np.arange(len(df)) * 50
        df['Open'] = df['Close'] + 20
        df['High'] = df['Close'] + 50
        df['Low'] = df['Close'] - 50
        
        out = detect_vcp(df)
        assert not out['eligible'].any()

    def test_rejects_without_volume_spike(self):
        df = _generate_vcp_series()
        # Remove volume spike
        df.loc[df.index[148], 'Volume'] = 10000.0
        out = detect_vcp(df)
        assert out.iloc[148]['eligible'] == False

    def test_rejects_overextension(self):
        df = _generate_vcp_series()
        # Explode price to 10 ATRs above MA20
        df.loc[df.index[148], 'Close'] = 50000.0
        df.loc[df.index[148], 'High'] = 55000.0
        out = detect_vcp(df, max_extension_atr=3.0)
        assert out.iloc[148]['eligible'] == False

    def test_handles_nans_gracefully(self, sample_ohlcv):
        df = sample_ohlcv.copy()
        df.loc[df.index[50:60], 'Close'] = np.nan
        df.loc[df.index[70], 'Volume'] = np.nan
        out = detect_vcp(df)
        assert len(out) == len(df)
        # NaN points should never be eligible
        assert not out.iloc[50:60]['eligible'].any()
        assert out.iloc[70]['eligible'] == False
