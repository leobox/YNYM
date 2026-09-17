import pandas as pd
import numpy as np

def _atr(df: pd.DataFrame) -> pd.Series:
    previous = df.Close.shift(1)
    tr = pd.concat([df.High-df.Low,(df.High-previous).abs(),(df.Low-previous).abs()],axis=1).max(axis=1)
    return tr.rolling(14).mean().replace(0,np.nan)
