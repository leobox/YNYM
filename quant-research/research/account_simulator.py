import pandas as pd

def simulate_account(entries, data, sessions, mode, fee=0.00175, entry_slippage=0.0):
    cash = 1_000_000.0
    position = None
    trades = []
    marks = []
    skipped = 0
    no_fill = 0
    
    # Sort entries for deterministic tie breaking
    if 'score' in entries.columns:
        signals = {t: g.sort_values(['score', 'code'], ascending=[False, True]) for t, g in entries.groupby('scan')}
    else:
        signals = {t: g.sort_values(['code'], ascending=[True]) for t, g in entries.groupby('scan')}
        
    timeline = sorted({t for b in data.values() for t in b.index if t.date() in sessions})
    
    for ts in timeline:
        candidates = signals.get(str(ts))
        occupied = position is not None
        if candidates is not None and occupied:
            skipped += len(candidates)
        if not occupied and candidates is not None:
            r = candidates.iloc[0]
            if ts not in data[r.code].index:
                no_fill += 1
            else:
                bar = data[r.code].loc[ts]
                price = float(bar.Open) * (1 + entry_slippage)
                qty = int(cash // (price * (1 + fee)))
                if bar.Volume <= 0 or qty < 1:
                    no_fill += 1
                else:
                    cost = qty * price * (1 + fee)
                    cash -= cost
                    position = {
                        'code': r.code, 'name': r.get('name', r.code), 'entry': price, 'qty': qty, 'initial_qty': qty,
                        'start': ts, 'day': sessions.index(ts.date()), 'cost': cost, 'proceeds': 0.0,
                        'partial': False, 'peak': price, 'time_exit': False, 'last': price
                    }
                    skipped += len(candidates) - 1
                
        if position is not None:
            p = position
            b = data[p['code']]
            if ts not in b.index:
                marks.append({'time': str(ts), 'equity': cash + p['qty'] * p['last']})
                continue
            bar = b.loc[ts]
            age = sessions.index(ts.date()) - p['day'] + 1
            stop = max(p['entry'] * 0.95, p['peak'] * 0.95) if p['partial'] else p['entry'] * 0.95
            target = p['entry'] * 1.10
            exit_price = None
            reason = None
            
            if p['time_exit']:
                exit_price = float(bar.Open)
                reason = 'TIME_NEXT_OPEN'
            elif bar.Open <= stop:
                exit_price = float(bar.Open)
                reason = 'STOP_GAP'
            elif not p['partial'] and bar.Open >= target:
                exit_price = target
                reason = 'TARGET'
            elif bar.Low <= stop:
                exit_price = stop
                reason = 'STOP'
            elif not p['partial'] and bar.High >= target:
                exit_price = target
                reason = 'TARGET'
                
            if exit_price is not None:
                sold = p['qty']
                if mode == 'partial' and reason == 'TARGET' and sold >= 2:
                    sold = p['initial_qty'] // 2
                    p['partial'] = True
                proceeds = sold * exit_price * (1 - fee)
                cash += proceeds
                p['proceeds'] += proceeds
                p['qty'] -= sold
                if p['qty'] == 0:
                    trades.append({**p, 'exit': str(ts), 'reason': reason, 'pnl': p['proceeds'] - p['cost']})
                    position = None
                    
            if position is not None:
                p['peak'] = max(p['peak'], float(bar.High))
                p['last'] = float(bar.Close)
                if (age >= 5 and ts.hour == 14) or ts == timeline[-1]:
                    proceeds = p['qty'] * float(bar.Close) * (1 - fee)
                    cash += proceeds
                    p['proceeds'] += proceeds
                    p['qty'] = 0
                    trades.append({**p, 'exit': str(ts), 'reason': 'PERIOD_END' if ts == timeline[-1] else 'MAX_5D',
                                   'pnl': p['proceeds'] - p['cost']})
                    position = None
                elif mode == 'time2' and age >= 2 and ts.hour == 14 and bar.Close <= p['entry']:
                    p['time_exit'] = True
                    
        marks.append({'time': str(ts), 'equity': cash + (position['qty'] * position['last'] if position else 0.0)})
        
    equity = pd.DataFrame(marks)
    if equity.empty:
        curve = pd.Series([1_000_000.0])
    else:
        curve = pd.concat([pd.Series([1_000_000.0]), equity.equity], ignore_index=True)
        
    result = {
        'mode': mode,
        'fee_each_side': fee,
        'ending_cash': cash,
        'ending_equity': float(curve.iloc[-1]),
        'return_pct': (curve.iloc[-1] / 1_000_000 - 1) * 100,
        'closed_trades': len(trades),
        'wins': sum(1 for t in trades if t['pnl'] > 0),
        'skipped_signals': skipped,
        'no_fill': no_fill,
        'max_drawdown_hourly_pct': float((curve / curve.cummax() - 1).min() * 100) if not curve.empty else 0.0,
        'open_position': position is not None
    }
    return result, pd.DataFrame(trades), equity
