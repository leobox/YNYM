import pandas as pd
import numpy as np

def clean_bars(raw_item: dict, now: pd.Timestamp) -> pd.DataFrame:
    df = pd.DataFrame(raw_item['indicators']['quote'][0],
                      index=pd.to_datetime(raw_item['timestamp'],unit='s',utc=True).tz_convert('Asia/Seoul'))
    df = df.rename(columns=str.title)[['Open','High','Low','Close','Volume']].dropna()
    df = df[~df.index.duplicated()].sort_index()
    df = df[(df.index.minute==0)&(df.index.second==0)&(df.index.hour>=9)&(df.index.hour<=15)]
    # Yahoo may append a zero-volume 15:00 quote placeholder after the session.
    df = df[~((df.index.hour==15)&(df.Volume==0)&(df.High==df.Low))]
    ends = df.index+pd.to_timedelta(np.where(df.index.hour==15,30,60),unit='m')
    return df[ends<=now]

def valid_ohlcv(df: pd.DataFrame) -> bool:
    if df.empty or df.index.has_duplicates or not df.index.is_monotonic_increasing:
        return False
    if not np.isfinite(df[['Open','High','Low','Close','Volume']].to_numpy(dtype=float)).all():
        return False
    bad = ((df[['Open','High','Low','Close']]<=0).any(axis=1) | (df.Volume<0) |
           (df.High<df[['Open','Close']].max(axis=1)) | (df.Low>df[['Open','Close']].min(axis=1)))
    return not bool(bad.any())
