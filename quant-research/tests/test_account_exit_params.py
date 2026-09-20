import datetime

import pandas as pd
import pytest

from research.account_simulator import simulate_account

FEE = 0.00175


def _stock():
    """2 sessions x 6 hourly bars. Entry at day1 09:00 open=10000.
    day1 10:00 high=10850 (touches +8% only), day1 11:00 high=11100 (touches +10%),
    day1 12:00 low=9650 (touches -3.5% only) but only after the target bar in default runs."""
    days = [datetime.date(2026, 9, 1), datetime.date(2026, 9, 2)]
    rows = []
    for d in days:
        for h in range(9, 15):
            ts = pd.Timestamp(f'{d} {h:02}:00', tz='Asia/Seoul')
            rows.append((ts, 10000.0, 10050.0, 9990.0, 10000.0, 1000.0))
    df = pd.DataFrame(rows, columns=['ts', 'Open', 'High', 'Low', 'Close', 'Volume']).set_index('ts')
    return df, days


def _run(df, days, mp, **kw):
    entries = pd.DataFrame([{'scan': str(df.index[0]), 'code': 'A', 'name': 'A', 'score': 1.0}])
    return simulate_account(entries, {'A': df}, days, mode='fixed', max_positions=mp, **kw)


@pytest.fixture(params=[1, 3])
def mp(request):
    return request.param


def test_defaults_are_legacy_ten_five(mp):
    df, days = _stock()
    df.iloc[2, df.columns.get_loc('High')] = 11100.0  # day1 11:00 touches +10%
    res_default, tr_default, _ = _run(df, days, mp)
    res_explicit, tr_explicit, _ = _run(df, days, mp, target_pct=0.10, stop_pct=0.05, exit_slippage=0.0)
    assert res_default == res_explicit
    assert tr_default.iloc[0]['reason'] == 'TARGET'
    assert tr_default.iloc[0]['exit'] == str(df.index[2])
    qty = tr_default.iloc[0]['initial_qty']
    assert tr_default.iloc[0]['proceeds'] == pytest.approx(qty * 11000.0 * (1 - FEE))


def test_custom_target_exits_earlier_at_lower_price(mp):
    df, days = _stock()
    df.iloc[1, df.columns.get_loc('High')] = 10850.0
    df.iloc[2, df.columns.get_loc('High')] = 11100.0
    _, tr, _ = _run(df, days, mp, target_pct=0.08)
    row = tr.iloc[0]
    assert row['reason'] == 'TARGET' and row['exit'] == str(df.index[1])
    assert row['proceeds'] == pytest.approx(row['initial_qty'] * 10800.0 * (1 - FEE))


def test_custom_stop_pct(mp):
    df, days = _stock()
    df.iloc[1, df.columns.get_loc('Low')] = 9650.0
    _, tr_default, _ = _run(df, days, mp)             # -5% stop not touched
    assert tr_default.iloc[0]['reason'] != 'STOP'
    _, tr, _ = _run(df, days, mp, stop_pct=0.03)      # -3% stop touched -> filled at 9700
    row = tr.iloc[0]
    assert row['reason'] == 'STOP'
    assert row['proceeds'] == pytest.approx(row['initial_qty'] * 9700.0 * (1 - FEE))


def test_exit_slippage_reduces_every_sell_and_keeps_cash_conservation(mp):
    df, days = _stock()
    df.iloc[2, df.columns.get_loc('High')] = 11100.0
    res0, tr0, _ = _run(df, days, mp)
    res1, tr1, _ = _run(df, days, mp, exit_slippage=0.0005)
    q = tr1.iloc[0]['initial_qty']
    assert tr1.iloc[0]['proceeds'] == pytest.approx(q * 11000.0 * (1 - 0.0005) * (1 - FEE))
    assert tr1.iloc[0]['pnl'] < tr0.iloc[0]['pnl']
    assert not res1['open_position']
    assert res1['ending_cash'] == pytest.approx(1_000_000.0 + tr1['pnl'].sum())


def test_exit_slippage_applies_to_period_end_close(mp):
    df, days = _stock()  # flat prices: no target/stop, exits at period end close
    _, tr0, _ = _run(df, days, mp)
    _, tr1, _ = _run(df, days, mp, exit_slippage=0.001)
    assert tr0.iloc[0]['reason'] == 'PERIOD_END'
    assert tr1.iloc[0]['proceeds'] == pytest.approx(tr0.iloc[0]['proceeds'] * (1 - 0.001))
