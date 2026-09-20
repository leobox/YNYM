import pandas as pd


def compute_market_breadth(data, window=20, min_stocks=30):
    """Market breadth: % of stocks whose daily close is above their `window`-day SMA.

    A stock counts on a date only if its SMA is defined (>= `window` closes so far).
    Dates with fewer than `min_stocks` valid stocks are NaN, never 0: the first
    `window - 1` sessions have no SMA yet, and a 0% there would read as a market crash.
    Uses only that date's close and earlier closes (available at the close of the date).
    """
    closes = {code: df['Close'].groupby(df.index.date).last() for code, df in data.items()}
    px = pd.DataFrame(closes).sort_index()
    ma = px.rolling(window, min_periods=window).mean()
    valid = ma.notna() & px.notna()
    above = (px > ma) & valid
    n_valid = valid.sum(axis=1)
    return above.sum(axis=1) / n_valid.where(n_valid >= min_stocks) * 100.0
