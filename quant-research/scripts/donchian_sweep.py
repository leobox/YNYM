"""T-037: Donchian 돌파 + 거래량 배수 진입 신호 파라미터 스윕 (연구 전용, 주문/계좌 연동 없음).

사전 고정 기준은 docs/tasks/T-037.md. 스윕 단계는 Train/Validate만 실행하고,
Test는 선정된 1개 조합에 대해 `--final` 로 딱 한 번 실행한다.

  python quant-research/scripts/donchian_sweep.py --sweep
  python quant-research/scripts/donchian_sweep.py --final N40_m2.0_t1_e0
"""
import argparse
import glob
import itertools
import json
import os
import re
import sys
import time
from functools import partial

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research.account_simulator import simulate_account  # noqa: E402
from research.signal_generator import generate_signals  # noqa: E402
from research.walk_forward import WalkForwardTest  # noqa: E402
from scanner.indicators import _atr  # noqa: E402

DATA_DIR = os.path.join(ROOT, 'data', 'imported', 'hourly_pattern')
OUT_DIR = os.path.join(ROOT, 'data', 'research', 'T-037')
GRID = {
    'N': (20, 40, 60),
    'mult': (1.5, 2.0, 3.0, 4.0),
    'trend': (0, 1),   # 1: 종가 > SMA60봉
    'ext': (0, 1),     # 1: (종가-SMA26)/ATR14 <= 4
}
MIN_TRADES = 8


def combo_id(N, mult, trend, ext):
    return f'N{N}_m{mult}_t{trend}_e{ext}'


def load_data(data_dir=DATA_DIR):
    data, names = {}, {}
    for f in sorted(glob.glob(os.path.join(data_dir, 'bars_*.csv'))):
        code = os.path.basename(f)[5:-4]
        df = pd.read_csv(f, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert('Asia/Seoul')
        data[code] = df.sort_index()
        names[code] = code
    return data, names


def split_sessions(data, train=30, validate=15):
    days = sorted({t.date() for df in data.values() for t in df.index})
    return days[:train], days[train:train + validate], days[train + validate:]


def donchian_signal(df, N, mult, trend, ext):
    """봉 i의 신호는 봉 i 종료 시점까지의 값만 사용한다(롤링·shift는 모두 과거 방향)."""
    hi_prev = df.High.rolling(N, min_periods=N).max().shift(1)
    hour = df.index.hour
    vol_med = (df.Volume.groupby(hour).transform(lambda s: s.shift(1).rolling(20, min_periods=5).median()))
    rng = df.High - df.Low
    close_loc = (df.Close - df.Low) / rng.where(rng > 0)
    ratio = df.Volume / vol_med.where(vol_med > 0)

    ok = ((df.Close > hi_prev) & (df.Close > df.Open) & (close_loc >= 0.7)
          & (df.Close >= 2000) & (df.Close * df.Volume >= 1e9) & (ratio >= mult))
    if trend:
        ok &= df.Close > df.Close.rolling(60, min_periods=60).mean()
    if ext:
        sma26 = df.Close.rolling(26, min_periods=26).mean()
        ok &= ((df.Close - sma26) / _atr(df, 14)) <= 4
    ok = ok.fillna(False)
    return pd.DataFrame({
        'eligible': ok,
        'score': ratio.where(ok, 0.0).fillna(0.0),
        'above60': False,
        'pattern': 'donchian_vol',
        'volume_ratio': ratio.fillna(0.0),
    }, index=df.index)


def make_runner(params, sessions_by_period, data, names, max_positions=3, with_test=False):
    train, validate, test = sessions_by_period
    wf = WalkForwardTest(train, validate, test if with_test else [])
    sig = partial(generate_signals, days=len(train) + len(validate) + len(test) + 5,
                  signal_func=partial(donchian_signal, **params))
    acc = partial(simulate_account, mode='fixed', max_positions=max_positions)
    return wf.run(sig, acc, data, names)


def summarize(period):
    r = period.get('result', {})
    return {
        'signals': period.get('signal_count', 0),
        'trades': r.get('closed_trades', 0),
        'wins': r.get('wins', 0),
        'ret': round(float(r.get('return_pct', 0.0)), 2),
        'mdd': round(float(r.get('max_drawdown_hourly_pct', 0.0)), 2),
        'skipped': r.get('skipped_signals', 0),
        'no_fill': r.get('no_fill', 0),
    }


def run_sweep(args):
    data, names = load_data()
    sessions = split_sessions(data)
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = []
    t0 = time.time()
    combos = list(itertools.product(*GRID.values()))
    for k, (N, mult, trend, ext) in enumerate(combos, 1):
        params = dict(N=N, mult=mult, trend=trend, ext=ext)
        res = make_runner(params, sessions, data, names, max_positions=3, with_test=False)
        row = {'id': combo_id(**params), **params}
        for name in ('train', 'validate'):
            for key, val in summarize(res[name]).items():
                row[f'{name}_{key}'] = val
        rows.append(row)
        print(f'[{k}/{len(combos)}] {row["id"]} tr={row["train_trades"]}/{row["train_ret"]}% '
              f'va={row["validate_trades"]}/{row["validate_ret"]}% ({time.time() - t0:.0f}s)', flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT_DIR, 'sweep_train_validate.csv'), index=False)
    elig = df[(df.train_trades >= MIN_TRADES) & (df.validate_trades >= MIN_TRADES)].copy()
    elig['score'] = elig[['train_ret', 'validate_ret']].min(axis=1)
    elig = elig.sort_values(['score', 'id'], ascending=[False, True])
    print(f'\n적격 {len(elig)}/{len(df)}  Validate 양수 {int((df.validate_ret > 0).sum())}/{len(df)}  '
          f'Train 양수 {int((df.train_ret > 0).sum())}/{len(df)}')
    print(elig.head(10).to_string(index=False))
    if len(elig):
        print('\n선정(1개):', elig.iloc[0]['id'])


def run_final(args):
    data, names = load_data()
    sessions = split_sessions(data)
    m = re.fullmatch(r'N(\d+)_m([\d.]+)_t([01])_e([01])', args.final)
    if not m:
        raise SystemExit(f'조합 id 형식 오류: {args.final}')
    params = dict(N=int(m[1]), mult=float(m[2]), trend=int(m[3]), ext=int(m[4]))
    assert combo_id(**params) == args.final, (combo_id(**params), args.final)
    out = {}
    for mp in (3, 1):
        res = make_runner(params, sessions, data, names, max_positions=mp, with_test=True)
        out[f'mp{mp}'] = {p: summarize(res[p]) for p in ('train', 'validate', 'test')}
        out[f'mp{mp}']['warnings'] = {p: res[p].get('warnings') for p in ('train', 'validate', 'test')}
        res['test']['trades'].to_csv(os.path.join(OUT_DIR, f'final_{args.final}_mp{mp}_test_trades.csv'), index=False)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    with open(os.path.join(OUT_DIR, f'final_{args.final}.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--sweep', action='store_true')
    ap.add_argument('--final', help='선정된 조합 id (예: N40_m2.0_t1_e0), Test 1회 실행')
    a = ap.parse_args()
    if a.sweep:
        run_sweep(a)
    elif a.final:
        run_final(a)
    else:
        ap.error('--sweep 또는 --final 필요')
