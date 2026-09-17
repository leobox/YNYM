import unittest
import pandas as pd
from tune_11am_ytd import select
from account_hourly_hold import simulate


class Tune11Tests(unittest.TestCase):
    def test_price_cap_after_top_five_and_stress(self):
        rows=[{'scan':'2026-01-02 11:00:00+09:00','code':str(i),'score':100-i,
               'lag':0,'mode':'hold','trend':True,'entry_open':104 if i<5 else 100,
               'trigger_close':100} for i in range(6)]
        pop=pd.DataFrame(rows)
        self.assertEqual(len(select(pop,0,'hold',None,False,0)),5)
        self.assertTrue(select(pop,0,'hold',3,False,0).empty)
        self.assertEqual(len(select(pop,0,'hold',5,False,0)),5)
        self.assertTrue(select(pop,0,'hold',5,False,.02).empty)

    def test_entry_stress_changes_quantity_target_and_cash(self):
        ts=pd.Timestamp('2026-01-02 11:00',tz='Asia/Seoul')
        b=pd.DataFrame([[100,115,99,110,100]],columns=['Open','High','Low','Close','Volume'],index=[ts])
        entries=pd.DataFrame([{'scan':str(ts),'code':'x','name':'x','score':80}])
        r,t,e=simulate(entries,{'x':b},[ts.date()],'fixed',fee=0,entry_slippage=.003)
        qty=int(1e6//100.3)
        self.assertEqual(t.iloc[0].initial_qty,qty)
        self.assertAlmostEqual(r['ending_cash'],1e6+qty*100.3*.1)


if __name__=='__main__':unittest.main()
