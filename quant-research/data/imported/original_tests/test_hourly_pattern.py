"""Timing, censoring, and causal-score regression checks."""
import unittest
import numpy as np
import pandas as pd
from backtest_hourly_pattern import outcome, study
from strategy import hourly_pattern


def bars(days=25):
    idx = pd.DatetimeIndex([pd.Timestamp(d).tz_localize('Asia/Seoul') + pd.Timedelta(hours=h)
                            for d in pd.bdate_range('2026-07-01', periods=days) for h in range(9, 16)])
    c = 3000 + np.sin(np.arange(len(idx)) / 8) * 100
    return pd.DataFrame({'Open': c, 'High': c + 10, 'Low': c - 10,
                         'Close': c, 'Volume': np.full(len(idx), 1000)}, index=idx)


class TimingTests(unittest.TestCase):
    def test_no_stop_and_same_bar_target(self):
        df = bars(2); ts = df.index[0]; df.loc[ts, ['Open', 'High', 'Low']] = [3000, 3200, 1000]
        sessions = sorted(set(df.index.date))
        r = outcome(df, ts, sessions[-1], sessions)
        self.assertTrue(r['hit']); self.assertEqual(r['return_pct'], 5)
        self.assertEqual(r['hit_bar'], str(ts))

    def test_past_high_cannot_hit(self):
        df = bars(2); df.loc[:, ['Open', 'High', 'Low', 'Close']] = [3000, 3010, 2990, 3000]
        df.iloc[0, df.columns.get_loc('High')] = 4000
        sessions = sorted(set(df.index.date))
        self.assertFalse(outcome(df, df.index[1], sessions[-1], sessions)['hit'])

    def test_incomplete_horizon_censored_even_if_early_hit(self):
        df = bars(2); df['High'] = 4000
        sessions = sorted(set(df.index.date))
        self.assertIsNone(outcome(df, df.index[-7], sessions[-1], sessions, 3))

    def test_missing_final_bar_censored(self):
        df = bars(2); sessions = sorted(set(df.index.date))
        self.assertIsNone(outcome(df.iloc[:-1], df.index[0], sessions[-1], sessions))

    def test_future_prices_do_not_change_past_scores(self):
        df = bars(); original = hourly_pattern(df)
        df.iloc[120:, df.columns.get_indexer(['Open', 'High', 'Low', 'Close'])] *= 3
        pd.testing.assert_frame_equal(original.iloc[:120], hourly_pattern(df).iloc[:120])

    def test_signal_bar_precedes_entry_and_rank_limit(self):
        df = bars(); sessions = sorted(set(df.index.date))
        data = {str(i): df for i in range(7)}
        names = {str(i): str(i) for i in range(7)}
        from unittest.mock import patch
        scores = pd.DataFrame({'score': 80., 'volume_ratio': 2., 'pattern': 'rebound',
                               'eligible': True}, index=df.index)
        with patch('backtest_hourly_pattern.hourly_pattern', return_value=scores):
            result = study(data, names, sessions, days=2, lag=60)
        self.assertTrue((result.groupby('scan').size() == 5).all())
        for row in result.itertuples():
            t = pd.Timestamp(row.signal_bar)
            end = t + pd.Timedelta(minutes=30 if t.hour == 15 else 60)
            self.assertLessEqual(end + pd.Timedelta(hours=1), pd.Timestamp(row.scan))

    def test_entry_bar_final_volume_cannot_change_selection(self):
        from unittest.mock import patch
        df = bars(); sessions = sorted(set(df.index.date))
        scores = pd.DataFrame({'score': 80., 'volume_ratio': 2., 'pattern': 'rebound',
                               'above60': True, 'eligible': True}, index=df.index)
        with patch('backtest_hourly_pattern.hourly_pattern', return_value=scores):
            a = study({'A': df}, {'A': 'A'}, sessions, days=1)
            df.loc[df.index[-7:], 'Volume'] = 0
            b = study({'A': df}, {'A': 'A'}, sessions, days=1)
        columns = ['scan','code','rank','score','signal_bar']
        pd.testing.assert_frame_equal(a[columns], b[columns])
        self.assertTrue((b.status == 'NO_FILL').all())

    def test_filter_applied_before_top_five(self):
        from unittest.mock import patch
        df = bars(); sessions = sorted(set(df.index.date))
        bad = pd.DataFrame({'score': 99., 'volume_ratio': 2., 'pattern': 'rebound',
                            'above60': False, 'eligible': True}, index=df.index)
        good = bad.assign(score=70., above60=True)
        data = {str(i): df for i in range(6)}
        with patch('backtest_hourly_pattern.hourly_pattern', side_effect=[bad]*5+[good]):
            x = study(data, {k:k for k in data}, sessions, days=1, require_above60=True)
        self.assertEqual(set(x.code), {'5'})

    def test_holdout_cannot_use_target_after_cutoff(self):
        from analyze_hourly_failures import truncated
        df = bars(7)
        df.loc[:, ['Open','High','Low','Close']] = [3000,3010,2990,3000]
        df.iloc[-7:, df.columns.get_loc('High')] = 3200
        sessions = sorted(set(df.index.date))
        rows = pd.DataFrame([{'code':'A','scan':str(df.index[0]),'status':'TARGET','hit':True,
                              'return_pct':5.,'hit_bar':str(df.index[-7])}])
        x = truncated(rows, {'A':df}, sessions[:-1],15)
        self.assertFalse(x.iloc[0].hit)
        self.assertEqual(x.iloc[0].status,'OPEN_MARK_TO_MARKET')

    def test_percentage_bracket_stop_precedes_target(self):
        df = bars(2); df.loc[:, ['Open','High','Low','Close']] = [3000,3010,2990,3000]
        df.iloc[0, df.columns.get_loc('Low')] = 2800
        df.iloc[1, df.columns.get_loc('High')] = 3400
        sessions = sorted(set(df.index.date))
        r = outcome(df,df.index[0],sessions[-1],sessions,target_pct=10,stop_pct=5)
        self.assertEqual(r['status'],'STOP'); self.assertAlmostEqual(r['return_pct'],-5)

    def test_percentage_bracket_same_bar_stop_first(self):
        df = bars(2); df.loc[:, ['Open','High','Low','Close']] = [3000,3010,2990,3000]
        df.iloc[0, df.columns.get_loc('Low')] = 2800
        df.iloc[0, df.columns.get_loc('High')] = 3400
        sessions = sorted(set(df.index.date))
        r = outcome(df,df.index[0],sessions[-1],sessions,target_pct=10,stop_pct=5)
        self.assertEqual(r['status'],'STOP')

    def test_percentage_bracket_gap_down_fills_open(self):
        df = bars(2); df.loc[:, ['Open','High','Low','Close']] = [3000,3010,2990,3000]
        df.iloc[1, df.columns.get_loc('Open')] = 2700
        sessions = sorted(set(df.index.date))
        r = outcome(df,df.index[0],sessions[-1],sessions,target_pct=10,stop_pct=5)
        self.assertAlmostEqual(r['return_pct'],-10)

    def test_percentage_bracket_gap_up_limit_before_later_low(self):
        df = bars(2); df.loc[:, ['Open','High','Low','Close']] = [3000,3010,2990,3000]
        df.iloc[1, df.columns.get_indexer(['Open','High','Low'])] = [3500,3600,2700]
        sessions = sorted(set(df.index.date))
        r = outcome(df,df.index[0],sessions[-1],sessions,target_pct=10,stop_pct=5)
        self.assertEqual(r['status'],'TARGET'); self.assertEqual(r['return_pct'],10)


if __name__ == '__main__':
    unittest.main(verbosity=2)
