"""T-049: 감사 지적 보완 후 사전 고정 기준(docs/tasks/T-049.md)에 따른 재검증. 연구 전용, 주문/계좌 연동 없음.

  python quant-research/scripts/t049_oot_validation.py --precursors
  python quant-research/scripts/t049_oot_validation.py --breadth
  python quant-research/scripts/t049_oot_validation.py --ml
"""
import argparse
import datetime
import json
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research.account_simulator import simulate_account  # noqa: E402
from research.regime import compute_market_breadth  # noqa: E402
from scripts.backtest_surge_precursors import generate_precursor_signals  # noqa: E402
from scripts.ml_high_winrate_search import load_ytd_data, extract_10_features_and_labels  # noqa: E402

OUT_DIR = os.path.join(ROOT, 'data', 'research', 'T-049')
DISCOVERY_START = datetime.date(2026, 6, 25)   # live_60d 시작 = T-042 임계값 발견 구간 시작
WARMUP = 20
COST = dict(fee=0.00175, entry_slippage=0.0005, exit_slippage=0.0005)
N_RANDOM = 1000


class FastSessions(list):
    """list와 의미가 같고 `in`/`index`만 O(1). simulate_account가 세션 리스트를 반복 조회해 느려지는 것을 피한다."""

    def __init__(self, it):
        super().__init__(it)
        self._set = set(self)
        self._idx = {d: i for i, d in enumerate(self)}

    def __contains__(self, x):
        return x in self._set

    def index(self, x, *a):
        return self._idx[x]


def profit_factor(trades):
    if trades.empty:
        return float('nan')
    gp = trades.loc[trades.pnl > 0, 'pnl'].sum()
    gl = abs(trades.loc[trades.pnl < 0, 'pnl'].sum())
    return float(gp / gl) if gl > 0 else float('nan')


def restrict(data, sessions):
    keep = set(sessions)
    return {c: df[[t.date() in keep for t in df.index]] for c, df in data.items()}


def run_account(entries, data, sessions, mp, **kw):
    if len(entries) == 0:
        return {'trades': 0, 'wins': 0, 'ret': 0.0, 'mdd': 0.0, 'pf': float('nan'), 'no_fill': 0, 'skipped': 0}, pd.DataFrame()
    res, tr, _ = simulate_account(entries, data, sessions, mode='fixed', max_positions=mp, **COST, **kw)
    tr = pd.DataFrame(tr)
    return {'trades': int(res['closed_trades']), 'wins': int(res['wins']), 'ret': round(float(res['return_pct']), 2),
            'mdd': round(float(res['max_drawdown_hourly_pct']), 2), 'pf': round(profit_factor(tr), 2),
            'no_fill': int(res['no_fill']), 'skipped': int(res['skipped_signals'])}, tr


def thirds_pnl(trades, sessions):
    if trades.empty:
        return [0.0, 0.0, 0.0]
    cut = np.array_split(np.array(sessions, dtype=object), 3)
    bounds = [(c[0], c[-1]) for c in cut]
    days = pd.to_datetime(trades['start']).dt.date
    return [round(float(trades.loc[[(b0 <= d <= b1) for d in days], 'pnl'].sum())) for b0, b1 in bounds]


def random_entries(sessions, codes, seed):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(WARMUP, len(sessions) - 1):
        scan = str(pd.Timestamp(f'{sessions[i + 1]} 09:00', tz='Asia/Seoul'))
        for c in rng.choice(codes, 5, replace=False):
            rows.append({'scan': scan, 'code': c, 'name': c, 'score': float(rng.random())})
    return pd.DataFrame(rows)


def run_precursors():
    data, names, sessions = load_ytd_data()
    oot_all = [d for d in sessions if d < DISCOVERY_START]
    oot = FastSessions(oot_all[WARMUP:])                         # 평가 세션 (진입은 21번째 세션부터)
    oot_set = set(oot_all[WARMUP + 1:])
    data_oot = restrict(data, oot)
    print(f'codes={len(data)} OOT sessions={len(oot)} ({oot[0]}~{oot[-1]}) warmup={WARMUP}', flush=True)

    out = {'window': [str(oot[0]), str(oot[-1])], 'sessions': len(oot), 'strategies': {}}
    for kind, label in (('trend', 'A'), ('reversal', 'B'), ('combined', 'C')):
        sig = generate_precursor_signals(data, sessions, strategy_type=kind)
        sig = sig[[pd.Timestamp(s).date() in oot_set for s in sig['scan']]] if len(sig) else sig
        out['strategies'][label] = {'signals': int(len(sig))}
        for mp in (1, 3):
            r, tr = run_account(sig, data_oot, oot, mp)
            r['thirds_pnl'] = thirds_pnl(tr, oot)
            out['strategies'][label][f'mp{mp}'] = r
            print(label, f'mp={mp}', r, flush=True)

    codes = sorted(data_oot)
    rets = {1: [], 3: []}
    t0 = time.time()
    for seed in range(N_RANDOM):
        e = random_entries(oot, codes, seed)
        for mp in (1, 3):
            r, _ = run_account(e, data_oot, oot, mp)
            rets[mp].append(r['ret'])
        if seed == 0:
            print('one random seed sec:', round(time.time() - t0, 1), flush=True)
    out['random'] = {}
    for mp in (1, 3):
        a = np.array(rets[mp])
        out['random'][f'mp{mp}'] = {'n': len(a), 'mean': round(a.mean(), 2), 'median': round(float(np.median(a)), 2),
                                    'p90': round(float(np.percentile(a, 90)), 2), 'p95': round(float(np.percentile(a, 95)), 2),
                                    'max': round(a.max(), 2), 'share_pos': round(float((a > 0).mean()), 3)}
        for label in 'ABC':
            v = out['strategies'][label][f'mp{mp}']['ret']
            out['strategies'][label][f'mp{mp}']['pctile_vs_random'] = round(float((a < v).mean() * 100), 1)
        print('random', mp, out['random'][f'mp{mp}'], flush=True)

    # 사전 고정 기준 판정 (mp=3)
    for label in 'ABC':
        r = out['strategies'][label]['mp3']
        checks = {'trades>=30': r['trades'] >= 30, 'ret>0': r['ret'] > 0,
                  'pctile>=95': r['pctile_vs_random'] >= 95, 'thirds>=2pos': sum(x > 0 for x in r['thirds_pnl']) >= 2}
        r['criteria'] = checks
        r['monitoring_candidate'] = all(checks.values())
        print(label, 'criteria', checks, '=>', r['monitoring_candidate'])
    save(out, 'precursors_oot.json')


def run_breadth():
    from scipy import stats
    data, names, sessions = load_ytd_data()
    b = compute_market_breadth(data)
    px = pd.DataFrame({c: df['Close'].groupby(df.index.date).last() for c, df in data.items()}).sort_index()
    ew = {}
    for h in (1, 5):
        fwd = px.shift(-h) / px - 1
        ew[h] = fwd.mean(axis=1) * 100
    d = pd.DataFrame({'breadth': b, 'fwd1': ew[1], 'fwd5': ew[5]}).dropna()
    out = {'valid_days': int(b.notna().sum()), 'first_valid': str(b.dropna().index[0]),
           'breadth_mean': round(float(b.mean()), 1), 'breadth_min': round(float(b.min()), 1), 'breadth_max': round(float(b.max()), 1),
           'analysis_days': int(len(d)), 'buckets': {}}
    for name, m in (('<25', d.breadth < 25), ('25-45', (d.breadth >= 25) & (d.breadth < 45)), ('>=45', d.breadth >= 45)):
        s = d[m]
        out['buckets'][name] = {'days': int(len(s)), 'fwd1_mean': round(float(s.fwd1.mean()), 3) if len(s) else None,
                                'fwd5_mean': round(float(s.fwd5.mean()), 3) if len(s) else None,
                                'fwd5_pos_share': round(float((s.fwd5 > 0).mean()), 2) if len(s) else None}
    for h in ('fwd1', 'fwd5'):
        rho, p = stats.spearmanr(d.breadth, d[h])
        out[f'spearman_{h}'] = {'rho': round(float(rho), 3), 'p_naive': round(float(p), 4)}
    out['note'] = 'fwd5 windows overlap: effective sample ~ days/5, p-values are optimistic'
    print(json.dumps(out, ensure_ascii=False, indent=2))
    save(out, 'breadth_monitor.json')


def run_ml():
    from sklearn.ensemble import RandomForestClassifier
    data, names, sessions = load_ytd_data()
    b = compute_market_breadth(data)
    df = extract_10_features_and_labels(data, sessions, b, 0.08, 0.04, 5)
    feats_all = [c for c in df.columns if c.startswith('F')]
    tr_d, va_d, te_d = set(sessions[25:110]), set(sessions[115:150]), set(sessions[155:])
    te_sessions = FastSessions(sessions[155:])
    data_te = restrict(data, te_sessions)
    out = {'split': {'train': [str(sessions[25]), str(sessions[109])], 'validate': [str(sessions[115]), str(sessions[149])],
                     'test': [str(sessions[155]), str(sessions[-1])]}, 'variants': {}}
    for vname, drop in (('with_F10', ()), ('without_F10', ('F10_market_breadth',))):
        cols = [c for c in feats_all if c not in drop]
        tr, va, te = df[df.date.isin(tr_d)], df[df.date.isin(va_d)], df[df.date.isin(te_d)].copy()
        rf = RandomForestClassifier(n_estimators=100, max_depth=6, min_samples_leaf=10, random_state=42).fit(tr[cols], tr.label)
        pv, pt = rf.predict_proba(va[cols])[:, 1], rf.predict_proba(te[cols])[:, 1]
        chosen, table = None, []
        for th in (0.40, 0.45, 0.50, 0.55, 0.60, 0.65):
            m = pv >= th
            n, nd = int(m.sum()), int(va.date[m].nunique())
            prec = float(va.label[m].mean()) if n else float('nan')
            table.append({'th': th, 'cands': n, 'dates': nd, 'precision': round(prec, 3) if n else None})
            if n >= 30 and nd >= 10 and (chosen is None or prec > chosen[1]):
                chosen = (th, prec)
        v = {'validate_table': table, 'validate_base_rate': round(float(va.label.mean()), 3),
             'test_base_rate': round(float(te.label.mean()), 3), 'chosen_threshold': chosen[0] if chosen else None}
        if chosen:
            th = chosen[0]
            te['prob'] = pt
            sel = te[te.prob >= th]
            v['test'] = {'cands': int(len(sel)), 'dates': int(sel.date.nunique()),
                         'precision': round(float(sel.label.mean()), 3) if len(sel) else None}
            rows = []
            for scan, g in sel.groupby('scan_ts'):
                for _, r in g.sort_values(['prob', 'code'], ascending=[False, True]).head(5).iterrows():
                    rows.append({'scan': scan, 'code': r.code, 'name': r.code, 'score': float(r.prob)})
            entries = pd.DataFrame(rows)
            for mp in (1, 3):
                acc, _ = run_account(entries, data_te, te_sessions, mp, target_pct=0.08, stop_pct=0.04)
                v['test'][f'mp{mp}'] = acc
            t = v['test']
            checks = {'cands>=100': t['cands'] >= 100, 'dates>=15': t['dates'] >= 15,
                      'precision>=base+5pp': (t['precision'] or 0) >= v['test_base_rate'] + 0.05,
                      'mp3_ret>0': t['mp3']['ret'] > 0}
            v['criteria'] = checks
            v['adopt'] = all(checks.values())
        else:
            v['adopt'] = False
            v['note'] = 'Validate에서 후보 ≥30건·날짜 ≥10일을 만족하는 임계값 없음 -> Test 실행 안 함'
        out['variants'][vname] = v
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    save(out, 'ml_revalidation.json')


def run_ml_checks(n_random=300):
    """`with_F10` 변형이 사전 기준을 통과한 뒤의 사후 점검(채택 판정 전 필수): breadth 단독 기준선과 랜덤 진입 기준선."""
    from sklearn.ensemble import RandomForestClassifier
    data, names, sessions = load_ytd_data()
    b = compute_market_breadth(data)
    df = extract_10_features_and_labels(data, sessions, b, 0.08, 0.04, 5)
    cols = [c for c in df.columns if c.startswith('F')]
    tr = df[df.date.isin(set(sessions[25:110]))]
    te = df[df.date.isin(set(sessions[155:]))].copy()
    rf = RandomForestClassifier(n_estimators=100, max_depth=6, min_samples_leaf=10, random_state=42).fit(tr[cols], tr.label)
    te['prob'] = rf.predict_proba(te[cols])[:, 1]
    sel = te[te.prob >= 0.40]
    out = {'test_base_rate': round(float(te.label.mean()), 3), 'ml_cands': int(len(sel)),
           'ml_precision': round(float(sel.label.mean()), 3)}
    # (1) breadth 단독: 같은 후보 수만큼 F10이 큰 순으로 선택
    top = te.sort_values('F10_market_breadth', ascending=False).head(len(sel))
    out['breadth_only_precision_same_n'] = round(float(top.label.mean()), 3)
    out['breadth_ge45_precision'] = round(float(te[te.F10_market_breadth >= 45].label.mean()), 3)
    out['ml_share_breadth_ge45'] = round(float((sel.F10_market_breadth >= 45).mean()), 3)
    # 같은 날짜 구성에서 ML이 종목 선별로 더하는 것: 날짜별 기저율 대비 ML 정밀도
    day_base = te.groupby('date').label.mean()
    out['ml_precision_minus_same_day_base'] = round(float(sel.label.mean() - sel.date.map(day_base).mean()), 3)
    # (2) 랜덤 진입 기준선 (같은 Test 구간, 같은 8%/4% 청산, mp별)
    te_sessions = FastSessions(sessions[155:])
    data_te = restrict(data, te_sessions)
    codes = sorted(data_te)
    rets = {1: [], 3: []}
    for seed in range(n_random):
        e = random_entries(te_sessions, codes, seed)
        for mp in (1, 3):
            r, _ = run_account(e, data_te, te_sessions, mp, target_pct=0.08, stop_pct=0.04)
            rets[mp].append(r['ret'])
    ml_ret = {1: 29.41, 3: 1.54}   # run_ml() 결과(with_F10, th=0.40)
    out['random_benchmark'] = {}
    for mp in (1, 3):
        a = np.array(rets[mp])
        out['random_benchmark'][f'mp{mp}'] = {'n': len(a), 'median': round(float(np.median(a)), 2), 'p90': round(float(np.percentile(a, 90)), 2),
                                              'p95': round(float(np.percentile(a, 95)), 2), 'ml_ret': ml_ret[mp],
                                              'ml_pctile': round(float((a < ml_ret[mp]).mean() * 100), 1)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    save(out, 'ml_checks.json')


def save(obj, name):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name), 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--precursors', action='store_true')
    ap.add_argument('--breadth', action='store_true')
    ap.add_argument('--ml', action='store_true')
    ap.add_argument('--ml-checks', action='store_true')
    a = ap.parse_args()
    if a.precursors:
        run_precursors()
    if a.breadth:
        run_breadth()
    if a.ml:
        run_ml()
    if a.ml_checks:
        run_ml_checks()
    if not (a.precursors or a.breadth or a.ml or a.ml_checks):
        ap.error('--precursors/--breadth/--ml 중 하나 필요')
