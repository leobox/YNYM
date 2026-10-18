"""Fetch public ETF daily data, preserve evidence, render a separate reference panel."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pandas as pd
import numpy as np
import requests

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'quant-research'))
from research.etf_observer import KST, VERSION, parse_daily, rank_etfs, render_panel, select_universe, update_readme

LIST_URL='https://finance.naver.com/api/sise/etfItemList.nhn'
CHART_URL='https://fchart.stock.naver.com/sise.nhn'


def get_bytes(url: str, params: dict | None = None) -> bytes:
    last=None
    for attempt in range(2):
        try:
            response=requests.get(url,params=params,headers={'User-Agent':'Mozilla/5.0'},timeout=(5,15))
            response.raise_for_status()
            if len(response.content)>8_000_000: raise ValueError('RESPONSE_TOO_LARGE')
            return response.content
        except (requests.RequestException,ValueError) as exc:
            last=type(exc).__name__
            if attempt==0:time.sleep(1)
    raise RuntimeError(f'PUBLIC_DATA_FETCH_FAILED:{last}')


def dump(path: Path, value: dict) -> None:
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    temp.replace(path)


def classify_coverage(frames: dict[str,pd.DataFrame], selected_count: int, now: datetime) -> tuple[str,str | None,list[str]]:
    if selected_count <= 0: return 'ERROR',None,[]
    dates={code:str(df.index[-1].date()) for code,df in frames.items() if len(df)}
    if not dates:return 'ERROR',None,[]
    counts=pd.Series(list(dates.values())).value_counts()
    # Modal complete date, with latest date breaking ties. A lone newer outlier
    # cannot silently become the market date for the rest of the panel.
    as_of=max(counts.index,key=lambda d:(int(counts[d]),d))
    fresh=sorted(code for code,date in dates.items() if date==as_of)
    if (now.astimezone(KST).date()-pd.Timestamp(as_of).date()).days>7:return 'STALE',as_of,fresh
    if len(fresh)/selected_count<.90:return 'INSUFFICIENT_COVERAGE',as_of,fresh
    return ('OK' if len(fresh)==selected_count else 'PARTIAL'),as_of,fresh


def collect(output: Path, limit: int = 100, include_leveraged: bool = False) -> dict:
    started=datetime.now(KST)
    run_id=started.strftime('%Y%m%dT%H%M%S%f')
    raw_dir=output/'raw'/run_id
    raw_dir.mkdir(parents=True,exist_ok=False)
    (output/'runs').mkdir(parents=True,exist_ok=True)
    inputs=[];errors=[];frames={};quality={};selected=[]
    record=dict(version=VERSION,run_id=run_id,fetched_at=started.isoformat(timespec='seconds'),
                include_leveraged=include_leveraged,selected_count=0,fresh_count=0,as_of=None,status='ERROR')

    def save_raw(name,payload):
        compressed=gzip.compress(payload,mtime=0)
        (raw_dir/name).write_bytes(compressed)
        return dict(path=f'raw/{run_id}/{name}',raw_bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),
                    compressed_sha256=hashlib.sha256(compressed).hexdigest())

    try:
        payload=get_bytes(LIST_URL)
        inputs.append(dict(save_raw('etf_list.json.gz',payload),url=LIST_URL,received_at=datetime.now(KST).isoformat()))
        selected,stats=select_universe(json.loads(payload.decode('euc-kr')),limit,include_leveraged)
        record.update(selected_count=len(selected),universe_statistics=stats,universe=selected)

        def fetch_one(item):
            code=item['code'];params=dict(symbol=code,timeframe='day',count=260,requestType=0)
            data=get_bytes(CHART_URL,params)
            meta=dict(save_raw(f'{code}.xml.gz',data),url=CHART_URL,params=params,
                      received_at=datetime.now(KST).isoformat())
            try:
                frame,q=parse_daily(data,code,started)
                return code,frame,q,meta,None
            except Exception as exc:
                return code,None,None,meta,type(exc).__name__+':'+str(exc)[:120]

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures={pool.submit(fetch_one,item):item['code'] for item in selected}
            for future in as_completed(futures):
                code=futures[future]
                try:
                    code,frame,q,meta,error=future.result()
                    inputs.append(meta)
                    if error:
                        errors.append(dict(code=code,error=error))
                    else:
                        frames[code]=frame;quality[code]=q
                except Exception as exc:
                    errors.append(dict(code=code,error=type(exc).__name__+':'+str(exc)[:120]))
        status,as_of,fresh=classify_coverage(frames,len(selected),started)
        record.update(status=status,as_of=as_of,fresh_count=len(fresh))
        if status in ('OK','PARTIAL'):
            # Keep missing shared-session rows as NaN, rather than letting a hole
            # silently shorten a 20/60-day window for one ETF.
            calendar=sorted({date for code in fresh for date in frames[code].index})
            evaluated={code:frames[code].reindex(calendar) for code in fresh}
            result=rank_etfs(evaluated,as_of)
            names={x['code']:x for x in selected}
            for group in result['stages'].values():
                for pick in group:
                    pick.update(name=names[pick['code']]['name'],leveraged_or_inverse=names[pick['code']]['leveraged_or_inverse'])
            record['result']=result
    except Exception as exc:
        errors.append(dict(code=None,error=type(exc).__name__+':'+str(exc)[:120]))
        record['status']='ERROR'
    record.update(fetched_at=datetime.now(KST).isoformat(timespec='seconds'),
                  completion_cutoff_clock=started.isoformat(),errors=sorted(errors,key=lambda e:e.get('code') or ''),
                  data_quality=quality,inputs=sorted(inputs,key=lambda i:i['path']),
                  provenance=dict(list_source=LIST_URL,daily_source=CHART_URL,
                                  price_unit='KRW',volume_unit='shares',adjustment_and_distributions='unverified',
                                  nav_and_total_return='not used',universe='current snapshot, not historical constituents',
                                  universe_selection='list price times volume proxy, descending, top configured limit',
                                  minimum_bars=125,source_requests_max=2*(1+len(selected))))
    record['settings']=dict(universe_limit=limit,include_leveraged=include_leveraged,
                            top_per_stage=5,completion_hour_kst=16,minimum_same_date_coverage=.9,
                            stale_calendar_days=7,rule_version=VERSION)
    record['environment']=dict(python=sys.version,pandas=pd.__version__,numpy=np.__version__,requests=requests.__version__)
    paths=[Path(__file__),ROOT/'quant-research/research/etf_observer.py']
    record['code_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    record['code_snapshots']=[save_raw(p.name+'.gz',p.read_bytes()) for p in paths]
    try:
        record['git_commit']=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,check=True,timeout=10).stdout.strip()
        record['git_dirty']=bool(subprocess.run(['git','status','--porcelain','--untracked-files=normal'],cwd=ROOT,capture_output=True,text=True,check=True,timeout=10).stdout.strip())
    except (subprocess.SubprocessError,OSError):record['git_commit']=None;record['git_dirty']=None
    dump(output/'runs'/f'{run_id}.json',record)
    dump(output/'latest.json',record)
    (output/'latest.md').write_text(render_panel(record),encoding='utf-8')
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'quant-research/data/etf_observation')
    parser.add_argument('--readme',type=Path)
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--include-leveraged',action='store_true')
    parser.add_argument('--render-only',type=Path,help='Render a previously captured JSON without network access')
    args=parser.parse_args()
    if not 1<=args.limit<=200:parser.error('limit must be 1..200')
    if args.render_only:
        record=json.loads(args.render_only.read_text(encoding='utf-8'))
    else:record=collect(args.output,args.limit,args.include_leveraged)
    if args.readme:update_readme(args.readme,render_panel(record))
    print(json.dumps({k:record.get(k) for k in ('run_id','status','as_of','selected_count','fresh_count')},ensure_ascii=True))
    if record['status'] not in ('OK','PARTIAL'):raise SystemExit(1)


if __name__=='__main__':main()
