"""Standalone Colab parity and completed-bar checks; no network needed."""
import unittest
import pandas as pd
from pattern_colab import hourly_pattern, completed_bars, top_five, same_hour_volume, confirmed_breakout, diagnostics
from backtest_clear_trigger import features
from strategy import hourly_pattern as original
from test_hourly_pattern import bars


class ColabTests(unittest.TestCase):
    def test_hold_parity_and_past_only(self):
        b=bars()
        got=confirmed_breakout(b)
        pd.testing.assert_series_equal(got.hold,features(b).hold)
        n=len(b)//2
        pd.testing.assert_frame_equal(got.iloc[:n],confirmed_breakout(b.iloc[:n]))
        self.assertTrue((~got.hold|got.previous_trigger).all())
        self.assertTrue((~got.previous_trigger|got.previous_liquid).all())
        self.assertTrue((~got.previous_liquid|got.previous_breakout).all())

    def test_diagnostics_and_hidden_columns(self):
        row={'코드':'123456','점수':80.,'_base':True,'_breakout':True,'_liquid':True,
             '_trigger':False,'_match':False,'_waiting':True,'_bar_time':'2026-09-15T09:00:00+09:00'}
        lines=diagnostics([row])
        self.assertIn('동시간 2배 0',lines[0])
        self.assertIn('09-15 09:00 1종목',lines[2])
        self.assertTrue(top_five([row]).empty)
        row['_match']=True
        self.assertFalse(any(c.startswith('_') for c in top_five([row]).columns))

    def test_same_scores_as_project(self):
        b=bars()
        pd.testing.assert_frame_equal(hourly_pattern(b),original(b))

    def test_partial_bar_and_quote_snapshot_excluded(self):
        times=pd.to_datetime(['2026-09-15 09:00','2026-09-15 10:00','2026-09-15 10:02']).tz_localize('Asia/Seoul')
        payload={'timestamp':[int(t.timestamp()) for t in times],
                 'indicators':{'quote':[{'open':[100]*3,'high':[101]*3,'low':[99]*3,'close':[100]*3,'volume':[10]*3}]}}
        got=completed_bars(payload,pd.Timestamp('2026-09-15 10:30',tz='Asia/Seoul'))
        self.assertEqual(list(got.index),[times[0]])

    def test_only_matches_shown(self):
        rows=[{'코드':str(i),'점수':90-i,'구분':'일치' if i==5 else '관찰','_match':i==5} for i in range(6)]
        got=top_five(rows)
        self.assertEqual(len(got),1)
        self.assertEqual(got.iloc[0]['구분'],'일치')
        self.assertEqual(got.iloc[0]['코드'],'5')
        self.assertEqual((got['구분']=='관찰').sum(),0)
        self.assertTrue(top_five(rows[:5]).empty)

    def test_zero_volume_closing_placeholder_excluded(self):
        times=pd.to_datetime(['2026-09-15 14:00','2026-09-15 15:00']).tz_localize('Asia/Seoul')
        payload={'timestamp':[int(t.timestamp()) for t in times],
                 'indicators':{'quote':[{'open':[100,101],'high':[102,101],'low':[99,101],'close':[101,101],'volume':[10,0]}]}}
        got=completed_bars(payload,pd.Timestamp('2026-09-16 08:00',tz='Asia/Seoul'))
        self.assertEqual(list(got.index),[times[0]])

    def test_same_hour_excludes_other_hours_and_current_bar(self):
        times=[];values=[]
        for i,day in enumerate(pd.bdate_range('2026-09-01',periods=6)):
            times.append(day.tz_localize('Asia/Seoul')+pd.Timedelta(hours=9))
            values.append(90 if i==5 else (i+1)*10)
            if i<5:
                times.append(day.tz_localize('Asia/Seoul')+pd.Timedelta(hours=14))
                values.append(10000)
        df=pd.DataFrame({'Volume':values},index=pd.DatetimeIndex(times))
        self.assertAlmostEqual(same_hour_volume(df),3.)
        self.assertTrue(pd.isna(same_hour_volume(df.iloc[:3])))


if __name__=='__main__':
    unittest.main(verbosity=2)
