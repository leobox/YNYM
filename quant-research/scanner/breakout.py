import pandas as pd
import numpy as np
from scanner.indicators import _atr

def confirmed_breakout(df: pd.DataFrame) -> pd.DataFrame:
    """Exact hold filter from backtest_clear_trigger, using completed bars only."""
    c=df.Close
    fast=c.rolling(12).mean();medium=c.rolling(26).mean()
    trend=(fast>fast.shift(3))&(medium>medium.shift(3))&(c>fast)&(c>medium)
    level=df.High.shift(1).rolling(6).max()
    location=(c-df.Low)/(df.High-df.Low).replace(0,np.nan)
    extension=(c-medium)/_atr(df)
    amount=c*df.Volume
    typical=amount.groupby(df.index.hour).transform(lambda x:x.shift(1).rolling(20,min_periods=5).median())
    ratio=amount/typical.replace(0,np.nan)
    breakout=trend&(c>level)&(c>df.Open)&(location>=.7)&(extension<=4)
    liquid=breakout&(amount>=1e9)
    trigger=liquid&(amount>=typical*2)
    hold_price=trend&(df.Low>=level.shift(1))&(c>=c.shift(1))&(extension<=4)
    held=trigger.shift(1,fill_value=False)&hold_price
    return pd.DataFrame({'hold':held,'previous_breakout':breakout.shift(1,fill_value=False),
                         'previous_liquid':liquid.shift(1,fill_value=False),
                         'previous_trigger':trigger.shift(1,fill_value=False),'waiting':trigger,
                         'hold_price':hold_price,'current_amount':amount/1e8,
                         'current_ratio':ratio,'current_level':level,'trigger_amount':amount.shift(1)/1e8,
                         'trigger_ratio':ratio.shift(1),'breakout_level':level.shift(1)},index=df.index)
