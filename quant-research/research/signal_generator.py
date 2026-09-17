import pandas as pd
import numpy as np

def generate_signals(data, names, sessions, days=10, lag=0, final_hour=15, require_above60=False,
                     signal_transform=None, signal_func=None):
    """
    Find all pattern signals across time.
    Must use ONLY completed bars and past-available info for signals.
    Entry at next observation bar open.
    No future data leakage: candidate selection must NOT use entry bar's final volume/high/low.
    """
    end_day = sessions[-1]
    rows = []
    signals = {}
    if signal_func is not None:
        signals = {code: signal_func(df) for code, df in data.items()}
    
    for day in sessions[-days:]:
        for hour in range(9, 16):
            scan = pd.Timestamp(f'{day} {hour:02}:00', tz='Asia/Seoul')
            candidates = []
            for code, df in data.items():
                if scan not in df.index:
                    continue
                ends = df.index + pd.to_timedelta(np.where(df.index.hour == 15, 30, 60), unit='m')
                available = np.flatnonzero(ends <= scan - pd.Timedelta(minutes=lag))
                if not len(available):
                    continue
                i = available[-1]
                # Reject stale/missing intraday observations; previous session is valid at 09:00.
                if hour > 9 + lag / 60 and df.index[i].date() != day:
                    continue
                if code not in signals:
                    continue
                s = signals[code].iloc[i]
                if signal_transform is not None:
                    s = signal_transform(code, df, i, scan, s.copy())
                
                # Handling for attributes vs dict keys depending on how signal_func returns
                if hasattr(s, 'above60'):
                    is_above60 = s.above60
                else:
                    is_above60 = s.get('above60', False)
                    
                if require_above60 and not is_above60:
                    continue
                    
                eligible = s.eligible if hasattr(s, 'eligible') else s.get('eligible', False)
                score = s.score if hasattr(s, 'score') else s.get('score', 0)
                
                if eligible and np.isfinite(score):
                    candidates.append((float(score), code, i, s))
            
            for rank, (_, code, i, s) in enumerate(sorted(candidates, key=lambda x: (-x[0], x[1]))[:5], 1):
                df = data[code]
                pattern = s.pattern if hasattr(s, 'pattern') else s.get('pattern', '')
                score = s.score if hasattr(s, 'score') else s.get('score', 0)
                vol_ratio = s.volume_ratio if hasattr(s, 'volume_ratio') else s.get('volume_ratio', 0)
                
                row = {'scan': str(scan), 'hour': hour, 'rank': rank, 'code': code, 'name': names.get(code, code),
                       'pattern': pattern, 'score': float(score), 'signal_bar': str(df.index[i]),
                       'lag_minutes': lag, 'volume_ratio': float(vol_ratio),
                       'above60': bool(is_above60)}
                       
                for key in ('prev_spike_bar','prev_spike_ratio','prev_hold','volume_component'):
                    val = getattr(s, key, s.get(key) if isinstance(s, dict) else None)
                    if val is not None:
                        row[key] = val
                rows.append(row)
    return pd.DataFrame(rows)
