"""Offline re-review probes. Assertions confirm remaining defects, not desired behavior."""
from pathlib import Path
from dataclasses import replace
import json
import runpy
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'quant-research'))
import pandas as pd
from research.sector_v1.models import TradingCalendar, DataProvenanceManifest, RegionalBenchmarkSpec, validate_research_inputs, default_krx_research_cost_schedule
from research.sector_v1.evaluator import run_full_v1_evaluation, seal_pre_run_attempt, seal_and_register_evaluation_run
from research.sector_v1.journal_e1 import E1JournalRecord, append_e1_journal_record, load_e1_journal, summarize_e1_journal

fixture = runpy.run_path(str(ROOT / 'quant-research/tests/test_t145_sector_v1.py'))
OUT = {}


def sparse_calendar():
    dates = pd.bdate_range('2024-01-01', '2026-01-05')
    # Every Tue/Thu really has a completed benchmark bar in this synthetic input.
    sparse = TradingCalendar([d for d in dates if d.weekday() in (0, 2, 4)])
    bars = fixture['_flat_or_curve_frame'](dates, 10000.)
    result = sparse.audit_window_coverage(dates[0], dates[-1], {'BM': bars},
        {'KR': RegionalBenchmarkSpec('KR', 'BM', 'BM', 'KRW', False)})
    assert result['calendar_complete_for_requested_window'] is True
    assert result['meets_24_months'] is True
    OUT['RR1_sparse_calendar_accepted'] = {
        'source_benchmark_sessions': len(dates), 'scheduled_sessions': len(sparse.sessions),
        'omitted_benchmark_sessions': len(dates)-len(sparse.sessions),
        'calendar_complete': result['calendar_complete_for_requested_window'],
        'meets_24_months': result['meets_24_months'],
        'benchmark_missing_ratio': result['max_benchmark_missing_ratio'],
    }


def journal_calendar_loss(tmp):
    cal = TradingCalendar(pd.bdate_range('2026-03-06', '2026-03-20'))
    # Expected next session is Mar 9, but both claimed check and actual fill are Mar 10.
    record = E1JournalRecord(record_id='late', action_type='ENTRY',
        signal_date='2026-03-06', check_session_date='2026-03-10',
        code='ETF', region='KR', sector='S', signal_close=10400.,
        signal_sma20=10000., signal_atr20=200., disposition='FILLED',
        observed_at='2026-03-10T10:00:00+09:00', observed_price=10450.,
        ordered_at='2026-03-10T10:01:00+09:00', order_qty=1,
        filled_at='2026-03-10T10:01:01+09:00', filled_qty=1, filled_price=10450.)
    p = tmp / 'journal.jsonl'
    append_e1_journal_record(p, record, calendar=cal)
    saved = json.loads(p.read_text(encoding='utf-8'))
    assert saved['compliance_status'] == 'PROTOCOL_VIOLATION_PRESERVED'
    summary_with = summarize_e1_journal(load_e1_journal(p, calendar=cal), calendar=cal)
    summary_default = summarize_e1_journal(load_e1_journal(p))
    assert summary_with['compliant_entry_fills'] == 0
    assert summary_default['compliant_entry_fills'] == 1
    OUT['RR2_e1_saved_violation_lost'] = {
        'saved_status': saved['compliance_status'],
        'saved_reasons': saved['protocol_violations'],
        'compliant_with_calendar': summary_with['compliant_entry_fills'],
        'compliant_default_reload': summary_default['compliant_entry_fills'],
        'violation_count_default_reload': summary_default['protocol_violation_record_count'],
    }


def seal_reuse(tmp):
    cal, mapping, bars = fixture['_build_multi_sector_fixture'](160)
    prov = DataProvenanceManifest('audit', '2026-11-07', 'synthetic', False, False, True, ())
    inputs = dict(calendar=cal, mapping=mapping, bars_by_code=bars,
                  cost_schedule=default_krx_research_cost_schedule(), provenance=prov)
    validated = validate_research_inputs(**inputs)
    config = dict(eval_start=mapping.valid_from, eval_end=bars['BM_KR'].index[-1],
                  n_random_policies=1, bootstrap_reps=1)
    out = tmp / 'runs'
    handle = seal_pre_run_attempt(out_root=out, repo_root=ROOT,
                                 validated_inputs=validated, **config)
    report = run_full_v1_evaluation(repo_root=ROOT, **inputs, **config)
    saved1 = seal_and_register_evaluation_run(out, report, pre_sealed_run=handle)
    report_path = Path(saved1['report_path'])
    first_bytes = report_path.read_bytes()
    # A second actual evaluation has a different random seed than the sealed inputs.
    report2 = run_full_v1_evaluation(repo_root=ROOT, **inputs, **config, seed=7)
    saved2 = seal_and_register_evaluation_run(out, report2, pre_sealed_run=handle)
    registry = json.loads((out / 'attempt_registry.json').read_text(encoding='utf-8'))
    assert saved1['run_dir'] == saved2['run_dir']
    assert first_bytes != report_path.read_bytes()
    assert [a['attempt_number'] for a in registry['attempts']] == [1, 1]
    pre_seal = json.loads((Path(saved1['run_dir']) / 'pre_run_input_seal.json').read_text(encoding='utf-8'))
    persisted = json.loads(report_path.read_text(encoding='utf-8'))
    assert pre_seal['evaluation_config']['seed'] != persisted['pre_run_seal']['evaluation_config']['seed']
    OUT['RR3_registered_run_overwritten'] = {
        'same_run_dir': True, 'first_report_overwritten': True,
        'attempt_numbers': [a['attempt_number'] for a in registry['attempts']],
        'disk_pre_seal_seed': pre_seal['evaluation_config']['seed'],
        'saved_report_seed': persisted['pre_run_seal']['evaluation_config']['seed'],
        'registry_report_hashes': [a['report_sha256'] for a in registry['attempts']],
    }


if __name__ == '__main__':
    sparse_calendar()
    with tempfile.TemporaryDirectory(prefix='t149-rereview-') as tmp_name:
        tmp = Path(tmp_name)
        journal_calendar_loss(tmp)
        seal_reuse(tmp)
    print(json.dumps(OUT, ensure_ascii=False, indent=2))
