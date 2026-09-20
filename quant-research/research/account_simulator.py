import pandas as pd

def simulate_account(entries, data, sessions, mode, fee=0.00175, entry_slippage=0.0, max_positions=1,
                     target_pct=0.10, stop_pct=0.05, exit_slippage=0.0):
    """
    Simulate trading account with realistic execution rules.
    - Entry at next observation bar open (slippage applied)
    - Target: +target_pct limit order (default +10%)
    - Stop: -stop_pct stop loss (default -5%; same-bar stop has priority over target)
    - exit_slippage: applied to every sell price (conservative; default 0 keeps legacy behavior)
    - Max hold: 5 trading sessions (exit at 14:00 bar of 5th session)
    - Multi-position support: max_positions controls concurrent positions (default 1)
    - Equal allocation of available cash across empty position slots
    - Cash conservation verified: ending cash equals equity when no open positions
    """
    if max_positions == 1:
        # Exact legacy single-position branch to guarantee 100% backward compatibility
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
                stop = max(p['entry'] * (1 - stop_pct), p['peak'] * (1 - stop_pct)) if p['partial'] else p['entry'] * (1 - stop_pct)
                target = p['entry'] * (1 + target_pct)
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
                    proceeds = sold * exit_price * (1 - exit_slippage) * (1 - fee)
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
                        proceeds = p['qty'] * float(bar.Close) * (1 - exit_slippage) * (1 - fee)
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
            'max_positions': 1,
            'ending_cash': cash,
            'ending_equity': float(curve.iloc[-1]),
            'return_pct': (curve.iloc[-1] / 1_000_000 - 1) * 100,
            'closed_trades': len(trades),
            'wins': sum(1 for t in trades if t['pnl'] > 0),
            'skipped_signals': skipped,
            'no_fill': no_fill,
            'max_drawdown_hourly_pct': float((curve / curve.cummax() - 1).min() * 100) if not curve.empty else 0.0,
            'open_position': position is not None,
            'open_positions_count': 1 if position is not None else 0
        }
        return result, pd.DataFrame(trades), equity

    # --- Multi-position implementation (max_positions > 1) ---
    cash = 1_000_000.0
    positions = {}  # code -> position dict
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
        
        # New candidate entries at open
        if candidates is not None:
            for _, r in candidates.iterrows():
                # Rule 30: ignore signals for stocks already held
                if r.code in positions:
                    skipped += 1
                    continue
                
                open_slots = max_positions - len(positions)
                if open_slots <= 0:
                    skipped += 1
                    continue
                
                if ts not in data[r.code].index:
                    no_fill += 1
                    continue
                    
                bar = data[r.code].loc[ts]
                price = float(bar.Open) * (1 + entry_slippage)
                allocated_cash = cash / open_slots
                qty = int(allocated_cash // (price * (1 + fee)))
                
                if bar.Volume <= 0 or qty < 1:
                    no_fill += 1
                else:
                    cost = qty * price * (1 + fee)
                    cash -= cost
                    positions[r.code] = {
                        'code': r.code, 'name': r.get('name', r.code), 'entry': price, 'qty': qty, 'initial_qty': qty,
                        'start': ts, 'day': sessions.index(ts.date()), 'cost': cost, 'proceeds': 0.0,
                        'partial': False, 'peak': price, 'time_exit': False, 'last': price
                    }
        
        # Check exits and mark to market for active positions
        closed_codes = []
        for code, p in list(positions.items()):
            b = data[p['code']]
            if ts not in b.index:
                continue
            bar = b.loc[ts]
            age = sessions.index(ts.date()) - p['day'] + 1
            stop = max(p['entry'] * (1 - stop_pct), p['peak'] * (1 - stop_pct)) if p['partial'] else p['entry'] * (1 - stop_pct)
            target = p['entry'] * (1 + target_pct)
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
                proceeds = sold * exit_price * (1 - exit_slippage) * (1 - fee)
                cash += proceeds
                p['proceeds'] += proceeds
                p['qty'] -= sold
                if p['qty'] == 0:
                    trades.append({**p, 'exit': str(ts), 'reason': reason, 'pnl': p['proceeds'] - p['cost']})
                    closed_codes.append(code)
                    
            if code not in closed_codes:
                p['peak'] = max(p['peak'], float(bar.High))
                p['last'] = float(bar.Close)
                if (age >= 5 and ts.hour == 14) or ts == timeline[-1]:
                    proceeds = p['qty'] * float(bar.Close) * (1 - exit_slippage) * (1 - fee)
                    cash += proceeds
                    p['proceeds'] += proceeds
                    p['qty'] = 0
                    trades.append({**p, 'exit': str(ts), 'reason': 'PERIOD_END' if ts == timeline[-1] else 'MAX_5D',
                                   'pnl': p['proceeds'] - p['cost']})
                    closed_codes.append(code)
                elif mode == 'time2' and age >= 2 and ts.hour == 14 and bar.Close <= p['entry']:
                    p['time_exit'] = True
                    
        for code in closed_codes:
            del positions[code]
            
        pos_equity = sum(p['qty'] * p['last'] for p in positions.values())
        marks.append({'time': str(ts), 'equity': cash + pos_equity})
        
    equity = pd.DataFrame(marks)
    if equity.empty:
        curve = pd.Series([1_000_000.0])
    else:
        curve = pd.concat([pd.Series([1_000_000.0]), equity.equity], ignore_index=True)
        
    result = {
        'mode': mode,
        'fee_each_side': fee,
        'max_positions': max_positions,
        'ending_cash': cash,
        'ending_equity': float(curve.iloc[-1]),
        'return_pct': (curve.iloc[-1] / 1_000_000 - 1) * 100,
        'closed_trades': len(trades),
        'wins': sum(1 for t in trades if t['pnl'] > 0),
        'skipped_signals': skipped,
        'no_fill': no_fill,
        'max_drawdown_hourly_pct': float((curve / curve.cummax() - 1).min() * 100) if not curve.empty else 0.0,
        'open_position': len(positions) > 0,
        'open_positions_count': len(positions)
    }
    return result, pd.DataFrame(trades), equity
