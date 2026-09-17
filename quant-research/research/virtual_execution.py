import pandas as pd

def simulate_trade(df, entry_ts, final_day, sessions, horizon=None, final_hour=15, target_pct=10.0, stop_pct=5.0):
    """
    Entry at observed bar open; target can be hit in that same bar.
    Rules (from design doc §8.2):
      - Same-bar stop+target: stop takes priority
      - Open >= target: fill at target (limit order)
      - Gap below stop: fill at open (loss can exceed -5%)
      - Volume 0 at entry: no fill, don't substitute another stock
      - Default: +10% target, -5% stop, max 5 trading days
      - Entry bar final volume/high/low NOT used for candidate selection
    """
    if df.loc[entry_ts, 'Volume'] <= 0:
        return {'status': 'NO_FILL'}
    
    entry = float(df.loc[entry_ts, 'Open'])
    day = entry_ts.date()
    start = sessions.index(day)
    
    if horizon is not None:
        end_idx = start + horizon - 1
        if end_idx >= len(sessions) or sessions[end_idx] > final_day:
            return {'status': 'CENSORED'}
        end_day_target = sessions[end_idx]
    else:
        end_day_target = final_day
        
    future = df[(df.index >= entry_ts) & (df.index.date <= end_day_target)]
    
    if future.empty or future.index[-1].date() != end_day_target or future.index[-1].hour != final_hour:
        return {'status': 'CENSORED'}
        
    target = entry * (100 + target_pct) / 100
    stop = entry * (100 - stop_pct) / 100 if stop_pct is not None else None
    
    status, exit_ts, exit_price = 'OPEN_MARK_TO_MARKET', None, float(future.Close.iloc[-1])
    
    for bar in future.itertuples():
        if stop is not None and bar.Open <= stop:
            status, exit_ts, exit_price = 'STOP', bar.Index, float(bar.Open)
        elif bar.Open >= target:
            status, exit_ts, exit_price = 'TARGET', bar.Index, target
        elif stop is not None and bar.Low <= stop:
            status, exit_ts, exit_price = 'STOP', bar.Index, stop
        elif bar.High >= target:
            status, exit_ts, exit_price = 'TARGET', bar.Index, target
            
        if exit_ts is not None:
            break
            
    hit = status == 'TARGET'
    hit_ts = exit_ts if hit else None
    
    return {
        'hit': hit, 
        'entry_price': entry, 
        'exit_price': exit_price,
        'target_price': target,
        'hit_bar': str(hit_ts) if hit else None,
        'sessions_to_hit': sessions.index(hit_ts.date()) - start + 1 if hit else None,
        'return_pct': target_pct if hit else (exit_price / entry - 1) * 100,
        'status': status, 
        'exit_bar': str(exit_ts) if exit_ts is not None else None,
        'stop_price': stop
    }
