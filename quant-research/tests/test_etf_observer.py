"""Offline invariants for ETF source integrity, completed bars and panel isolation."""
from datetime import datetime
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest
import requests

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from research.etf_observer import (KST, START, END, parse_daily, rank_etfs,
                                   render_panel, select_universe, update_readme)

spec = importlib.util.spec_from_file_location('etf_runner', PROJECT/'scripts/run_etf_observation.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def listing(*items):
    return {'resultCode':'success', 'result':{'etfItemList':list(items)}}


def item(code='069500', name='KODEX 200', volume=100000):
    return dict(itemcode=code, itemname=name, nowVal=10000, quant=volume)


def xml(rows, code='069500'):
    return ('<protocol><chartdata symbol="'+code+'">'+''.join(
        '<item data="'+row+'"/>' for row in rows)+'</chartdata></protocol>').encode('euc-kr')


def history(stage='breakout', end='2026-10-16'):
    index=pd.bdate_range(end=end, periods=140)
    close=np.full(140, 10000.0)
    high=np.full(140, 10300.0)
    low=np.full(140, 9700.0)
    # Wide former range, contracted prior 20 sessions, then volume ignition.
    high[-55]=12000; low[-50]=8000
    volume=np.full(140, 100000.0); volume[-1]=200000
    close[-1]=10400 if stage=='breakout' else 10200
    high[-1]=close[-1]+100; low[-1]=close[-1]-100
    return pd.DataFrame(dict(Open=close,High=high,Low=low,Close=close,Volume=volume),index=index)


def test_universe_excludes_leverage_but_accepts_etf_codes_and_sorts_ties():
    payload=listing(item('360751','해외 ETF'),item(),item('0001A0','새 ETF'),
                    item('122630','KODEX 레버리지',999999),item('114800','KODEX 인버스',999999))
    rows,stats=select_universe(payload)
    assert [r['code'] for r in rows]==['0001A0','069500','360751']
    assert stats['excluded_name_leverage_inverse']==2
    rows,_=select_universe(payload,include_leveraged=True)
    assert len(rows)==5 and rows[0]['leveraged_or_inverse']


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1])
def test_invalid_quote_is_not_zero_or_ranked(value):
    bad=item('999999'); bad['nowVal']=value
    rows,stats=select_universe(listing(item(),bad))
    assert len(rows)==1 and stats['invalid_records']==1


def test_overflow_duplicate_and_source_failure():
    bad=item('999999');bad.update(nowVal=1e300,quant=1e300)
    assert select_universe(listing(item(),bad))[1]['invalid_records']==1
    with pytest.raises(ValueError,match='DUPLICATE'):select_universe(listing(item(),item()))
    with pytest.raises(ValueError,match='UNAVAILABLE'):select_universe({'resultCode':'error'})
    with pytest.raises(ValueError,match='EMPTY'):select_universe(listing())


@pytest.mark.parametrize('hour,expected',[(15,1),(16,2)])
def test_completed_daily_cutoff(hour,expected):
    data=xml(['20261015|100|110|90|105|100','20261016|105|115|95|110|200'])
    frame,q=parse_daily(data,'069500',datetime(2026,10,16,hour,30,tzinfo=KST))
    assert len(frame)==expected
    assert q['incomplete_or_future']==2-expected


def test_invalid_ohlc_and_volume_are_not_filled_zero():
    data=xml(['20261012|100|110|90|105|0','20261013|100|99|90|105|10',
              '20261014|100|110|90|105|-1','20261015|100|110|90|nan|10',
              '20261016|105|115|95|110|200'])
    frame,q=parse_daily(data,'069500',datetime(2026,10,16,17,tzinfo=KST))
    assert len(frame)==2 and q['invalid_rows']==3
    assert frame.Volume.iloc[0]==0 and q['zero_volume_rows']==1


def test_wrong_symbol_duplicate_date_and_naive_clock_rejected():
    data=xml(['20261016|100|110|90|105|100']*2)
    now=datetime(2026,10,16,17,tzinfo=KST)
    with pytest.raises(ValueError,match='SYMBOL'):parse_daily(data,'360750',now)
    with pytest.raises(ValueError,match='DUPLICATE'):parse_daily(data,'069500',now)
    with pytest.raises(ValueError,match='TIMEZONE'):parse_daily(data,'069500',now.replace(tzinfo=None))


def test_disjoint_stages_top5_and_deterministic_tie_break():
    source={str(i).zfill(6):history() for i in reversed(range(8))}
    source['360750']=history('watch')
    result=rank_etfs(source,'2026-10-16')
    assert [x['code'] for x in result['stages']['breakout']]==[str(i).zfill(6) for i in range(5)]
    assert [x['code'] for x in result['stages']['watch']]==['360750']
    assert result['qualified_before_top5']==dict(breakout=8,watch=1)
    assert result==rank_etfs(dict(reversed(list(source.items()))),'2026-10-16')


def test_prefix_invariance_future_extreme_price_and_volume():
    prefix=history();future=pd.DataFrame(1e20,index=pd.bdate_range('2026-10-19',periods=4),columns=prefix.columns)
    assert rank_etfs({'069500':prefix},'2026-10-16')==rank_etfs(
        {'069500':pd.concat([prefix,future])},'2026-10-16')


def test_finite_inputs_with_overflowing_aggregates_do_not_rank():
    frame=history();frame['Volume']=1e308
    with np.errstate(over='ignore',invalid='ignore'):
        result=rank_etfs({'069500':frame},'2026-10-16')
    assert result['stages']==dict(breakout=[],watch=[])


@pytest.mark.parametrize('problem',['hole','no_volume','no_ignition','illiquid','short','stale'])
def test_missing_or_ineligible_data_does_not_fill_table(problem):
    frame=history()
    if problem=='hole':frame.iloc[-15,0]=np.nan
    if problem=='no_volume':frame.iloc[-1,frame.columns.get_loc('Volume')]=0
    if problem=='no_ignition':frame['Volume']=100000
    if problem=='illiquid':frame['Volume']/=100
    if problem=='short':frame=frame.iloc[-100:]
    if problem=='stale':frame=frame.iloc[:-1]
    assert rank_etfs({'069500':frame},'2026-10-16')['stages']==dict(breakout=[],watch=[])


def test_coverage_distinguishes_failure_old_partial_and_insufficient():
    now=datetime(2026,10,18,tzinfo=KST);frame=history()
    frames={str(i):frame for i in range(9)}
    assert runner.classify_coverage(frames,9,now)[0]=='OK'
    assert runner.classify_coverage(frames,10,now)[0]=='PARTIAL'
    assert runner.classify_coverage(frames,11,now)[0]=='INSUFFICIENT_COVERAGE'
    assert runner.classify_coverage({'a':history(end='2026-10-01')},1,now)[0]=='STALE'
    assert runner.classify_coverage({},0,now)[0]=='ERROR'
    frames['newer']=history(end='2026-10-19')
    # Modal date must not be replaced by a lone date outlier.
    assert runner.classify_coverage(frames,10,now)[1]=='2026-10-16'


def test_render_safe_names_and_explicit_no_data():
    result=rank_etfs({'069500':history()},'2026-10-16')
    result['stages']['breakout'][0]['name']='ETF|<script>[link]'
    record=dict(status='OK',as_of='2026-10-16',fetched_at='2026-10-18',result=result)
    panel=render_panel(record)
    assert 'ETF&#124;&lt;script&gt;&#91;link&#93;' in panel
    assert '조건 충족 없음' in panel
    record['status']='ERROR';panel=render_panel(record)
    assert '자료 확인 후 산출' in panel and '조건 충족 없음' not in panel
    assert 'ETF&#124;' not in panel


def test_cash_rate_tag_does_not_change_the_signal_or_ranking():
    result=rank_etfs({'069500':history()},'2026-10-16')
    result['stages']['breakout'][0]['name']='TIGER CD1년금리액티브(합성)'
    panel=render_panel(dict(status='OK',as_of='2026-10-16',fetched_at='2026-10-18',result=result))
    assert '금리·현금형 1개' in panel and '주식형 ETF의 상승초입과 성격이 다릅니다' in panel
    assert '기준 10,300원' in panel


def test_readme_update_only_owns_etf_block_and_is_idempotent(tmp_path):
    path=tmp_path/'README.md'
    before='title\n<!-- DUAL_FORWARD:START -->stock sentinel<!-- DUAL_FORWARD:END -->\nother panel\n'
    path.write_text(before,encoding='utf-8')
    update_readme(path,'first')
    update_readme(path,'second')
    text=path.read_text(encoding='utf-8')
    assert text.count(START)==text.count(END)==1 and 'first' not in text
    assert '<!-- DUAL_FORWARD:START -->stock sentinel<!-- DUAL_FORWARD:END -->' in text
    assert text.endswith('\nother panel\n')
    update_readme(path,'second')
    assert path.read_text(encoding='utf-8')==text
    path.write_text(START+'broken',encoding='utf-8')
    with pytest.raises(ValueError,match='MALFORMED'):update_readme(path,'third')


def test_fetch_has_bounded_timeout_and_retries(monkeypatch):
    calls=[]
    def fail(*args,**kwargs):
        calls.append(kwargs);raise requests.Timeout('fixture')
    monkeypatch.setattr(runner.requests,'get',fail)
    monkeypatch.setattr(runner.time,'sleep',lambda _:None)
    with pytest.raises(RuntimeError,match='FETCH_FAILED'):runner.get_bytes('https://example.invalid')
    assert len(calls)==2 and all(c['timeout']==(5,15) for c in calls)


def test_collect_preserves_bad_raw_hash_and_failure_state(tmp_path,monkeypatch):
    payload=json.dumps(listing(item()),ensure_ascii=False).encode('euc-kr')
    monkeypatch.setattr(runner,'get_bytes',lambda url,params=None:payload if params is None else b'broken xml')
    record=runner.collect(tmp_path)
    assert record['status']=='ERROR' and record['fresh_count']==0
    assert len(record['inputs'])==2 and record['errors'][0]['code']=='069500'
    for entry in record['inputs']+record['code_snapshots']:
        raw=gzip.decompress((tmp_path/entry['path']).read_bytes())
        assert hashlib.sha256(raw).hexdigest()==entry['sha256']
    assert json.loads((tmp_path/'latest.json').read_text(encoding='utf-8'))==record
    assert json.loads((tmp_path/'runs'/f"{record['run_id']}.json").read_text(encoding='utf-8'))==record
    assert '조회 실패' in (tmp_path/'latest.md').read_text(encoding='utf-8')


def test_universe_excludes_cash_bond_and_pension_ineligible_etfs_by_default():
    cd_item = dict(item('459580', 'KODEX CD금리액티브(합성)', 999999), etfTabCode=6)
    mmf_item = dict(item('488770', 'KODEX 머니마켓액티브', 999999), etfTabCode=7)
    bond_item = dict(item('273130', 'KODEX 종합채권(AA-이상)액티브', 999999), etfTabCode=6)
    gold_fut = dict(item('132030', 'KODEX 골드선물(H)', 999999), etfTabCode=5)
    wti_fut = dict(item('261220', 'KODEX WTI원유선물(H)', 999999), etfTabCode=5)
    synth_eq = dict(item('245710', 'ACE 베트남VN30(합성)', 999999), etfTabCode=4)
    gold_spot = dict(item('411060', 'ACE KRX금현물', 150000), etfTabCode=5)
    eq_item = dict(item('069500', 'KODEX 200', 100000), etfTabCode=1)
    rows, stats = select_universe(
        listing(cd_item, mmf_item, bond_item, gold_fut, wti_fut, synth_eq, gold_spot, eq_item)
    )
    assert [r['code'] for r in rows] == ['411060', '069500']
    assert all(r['pension_eligible'] for r in rows)
    assert stats['excluded_cash_or_bond'] == 3
    assert stats['excluded_pension_ineligible'] == 3

    result = rank_etfs({'069500': history()}, '2026-10-16')
    result['stages']['breakout'][0]['name'] = 'KODEX 200'
    panel = render_panel(dict(status='OK', as_of='2026-10-16', fetched_at='2026-10-18', result=result))
    assert '## 📊 ETF 상승 초입 관찰 (연금계좌 매수 가능)' in panel
    assert '연금저축·DC/IRP' in panel
    assert 'ETF 게이트별 생존 (`1`개 평가):' in panel
    assert '### ETF 상승 초입 · 20일 돌파 확정' in panel
    assert '### ETF 상승 초입 · 20일 수축 돌파임박' in panel
    assert '-5% 9,880원 · SMA10' in panel


