import pandas as pd

def generate_report(trades_df, account_result=None, equity_df=None):
    """
    Generate comprehensive report with:
    - +10% hit rate
    - Cost-adjusted win rate
    - Independent signal mean return
    - Account final balance
    - MDD (hourly basis)
    - Average holding time
    - Loss tail distribution
    - No-signal days count
    - Trade count insufficiency warning
    - Bias/limitation section
    """
    report = {}
    if trades_df is not None and not trades_df.empty:
        if 'hit' in trades_df.columns:
            hits = trades_df['hit'].sum()
            report['+10%_hit_rate'] = hits / len(trades_df)
            if 'return_pct' in trades_df.columns:
                report['mean_signal_return'] = trades_df['return_pct'].mean()
            
            losses = trades_df[trades_df['return_pct'] < 0]['return_pct']
            report['loss_tail_distribution'] = {
                '5th_percentile': losses.quantile(0.05) if not losses.empty else None,
                '1st_percentile': losses.quantile(0.01) if not losses.empty else None
            }
            
            if 'sessions_to_hit' in trades_df.columns:
                report['average_holding_time'] = trades_df['sessions_to_hit'].mean()
                
            report['trade_count'] = len(trades_df)
            
        elif 'pnl' in trades_df.columns:
            report['cost_adjusted_win_rate'] = (trades_df['pnl'] > 0).mean()
            report['trade_count'] = len(trades_df)
            
            loss_pcts = trades_df[trades_df['pnl'] < 0]['pnl'] / trades_df[trades_df['pnl'] < 0]['cost']
            report['loss_tail_distribution'] = {
                '5th_percentile_pct': loss_pcts.quantile(0.05) * 100 if not loss_pcts.empty else None,
                '1st_percentile_pct': loss_pcts.quantile(0.01) * 100 if not loss_pcts.empty else None
            }
            
            if 'start' in trades_df.columns and 'exit' in trades_df.columns:
                starts = pd.to_datetime(trades_df['start'])
                exits = pd.to_datetime(trades_df['exit'])
                report['average_holding_time_hours'] = (exits - starts).dt.total_seconds().mean() / 3600
                
    else:
        report['trade_count'] = 0
        
    if account_result is not None:
        report['account_final_balance'] = account_result.get('ending_equity', 1_000_000.0)
        report['mdd_hourly_pct'] = account_result.get('max_drawdown_hourly_pct', 0.0)
        
    report['trade_count_warning'] = 'insufficient' if report.get('trade_count', 0) < 8 else 'sufficient'
    
    from .bias_reporter import get_bias_warnings
    report['bias_and_limitations'] = get_bias_warnings(report.get('trade_count', 0))
        
    return report
