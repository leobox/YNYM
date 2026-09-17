import unittest
import pandas as pd
from account_hourly_hold import simulate


class AccountTests(unittest.TestCase):
    def run_case(self,bars,mode):
        idx=pd.date_range('2026-08-03 09:00',periods=len(bars),freq='h',tz='Asia/Seoul')
        data={'123456':pd.DataFrame(bars,columns=['Open','High','Low','Close','Volume'],index=idx)}
        entries=pd.DataFrame([{'scan':str(t),'code':'123456','name':'test','score':80} for t in idx])
        result,trades,_=simulate(entries,data,[idx[0].date()],mode,0)
        self.assertAlmostEqual(result['ending_cash'],1e6+trades.pnl.sum())
        self.assertFalse(result['open_position'])
        return result,trades

    def test_same_bar_stop_precedes_target(self):
        r,t=self.run_case([[100,112,94,105,100]],'fixed')
        self.assertAlmostEqual(r['ending_cash'],950000)
        self.assertEqual(t.iloc[0].reason,'STOP')

    def test_partial_previous_high_trailing_and_one_slot(self):
        r,t=self.run_case([[100,112,99,110,100],[110,111,100,101,100]],'partial')
        self.assertAlmostEqual(r['ending_cash'],1082000)
        self.assertEqual(r['closed_trades'],1)
        self.assertEqual(r['skipped_signals'],1)

    def test_gap_stop_uses_open(self):
        r,t=self.run_case([[100,104,99,102,100],[90,92,88,91,100]],'fixed')
        self.assertAlmostEqual(r['ending_cash'],900000)
        self.assertEqual(t.iloc[0].reason,'STOP_GAP')


if __name__=='__main__':unittest.main()
