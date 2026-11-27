"""Separate, descriptive ETF reference panel based on completed daily bars."""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

KST = ZoneInfo('Asia/Seoul')
VERSION = 'etf_reference_v2'
START, END = '<!-- ETF_OBSERVATION:START -->', '<!-- ETF_OBSERVATION:END -->'
EXCLUDED = re.compile(r'레버리지|인버스|레버|(?<![0-9])[23](?:\.0)?\s*[xX배]|[-−]\s*1\s*[xX]|롱.*숏|숏.*롱')
CASH_RATE = re.compile(r'CD(?:\d+년)?금리|KOFR|SOFR|머니마켓|MMF', re.IGNORECASE)
CASH_BOND = re.compile(
    r'CD(?:\d+년)?금리|KOFR|SOFR|머니마켓|MMF|단기채|종합채권|금융채|국고채|회사채|통안채|전단채|채권혼합|미국채혼합|TDF',
    re.IGNORECASE,
)
PENSION_INELIGIBLE = re.compile(
    r'선물|합성|원유|WTI|VIX|달러|엔선물|구리선물|농산물|콩선물|금선물|은선물|골드선물|은액티브|금액티브|국제금',
    re.IGNORECASE,
)
PENSION_ALLOWED_COMMODITY = re.compile(r'KRX금현물|금현물', re.IGNORECASE)


def select_universe(
    payload: dict,
    limit: int = 100,
    include_leveraged: bool = False,
    include_cash_bond: bool = False,
    pension_only: bool = True,
) -> tuple[list[dict], dict]:
    if not 1 <= limit <= 200: raise ValueError('INVALID_UNIVERSE_LIMIT')
    if payload.get('resultCode') != 'success':
        raise ValueError('ETF_LIST_UNAVAILABLE')
    raw = payload.get('result',{}).get('etfItemList')
    if not isinstance(raw,list) or not raw:
        raise ValueError('ETF_LIST_EMPTY')
    rows, excluded, excluded_cash_bond, excluded_pension, invalid, seen = [], 0, 0, 0, 0, set()
    for item in raw:
        code, name = str(item.get('itemcode','')), str(item.get('itemname','')).strip()
        try:
            price, volume = float(item['nowVal']), float(item['quant'])
        except (KeyError,TypeError,ValueError):
            invalid += 1
            continue
        if not re.fullmatch(r'[0-9A-Z]{6}',code) or not name or not np.isfinite([price,volume,price*volume]).all() or price<=0 or volume<0:
            invalid += 1
            continue
        if code in seen: raise ValueError('DUPLICATE_ETF_CODE')
        seen.add(code)
        tab_code = item.get('etfTabCode')
        leveraged = bool(EXCLUDED.search(name)) or (tab_code == 3)
        if leveraged and not include_leveraged:
            excluded += 1
            continue
        cash_or_bond = bool(CASH_BOND.search(name)) or (tab_code in (6, 7))
        if cash_or_bond and not include_cash_bond:
            excluded_cash_bond += 1
            continue
        pension_ineligible = bool(PENSION_INELIGIBLE.search(name)) or (
            tab_code == 5 and not PENSION_ALLOWED_COMMODITY.search(name)
        )
        if pension_ineligible and pension_only and not include_leveraged:
            excluded_pension += 1
            continue
        rows.append(dict(code=code,name=name,leveraged_or_inverse=leveraged,
                         cash_or_bond=cash_or_bond,
                         pension_eligible=not (leveraged or pension_ineligible),
                         selection_amount_proxy=price*volume,
                         source_tab_code=tab_code))
    rows.sort(key=lambda x:(-x['selection_amount_proxy'],x['code']))
    if not rows: raise ValueError('NO_ELIGIBLE_ETFS')
    return rows[:limit], dict(source_count=len(raw),excluded_name_leverage_inverse=excluded,
                             excluded_cash_or_bond=excluded_cash_bond,
                             excluded_pension_ineligible=excluded_pension,
                             invalid_records=invalid,eligible_before_limit=len(rows),selected=min(limit,len(rows)))


def parse_daily(payload: bytes, code: str, fetched_at: datetime) -> tuple[pd.DataFrame,dict]:
    if fetched_at.tzinfo is None: raise ValueError('FETCH_TIME_REQUIRES_TIMEZONE')
    root = ET.fromstring(payload.decode('euc-kr'))
    chart = root.find('chartdata')
    if chart is None or chart.get('symbol') != code: raise ValueError('CHART_SYMBOL_MISMATCH')
    rows=[]
    for item in chart.findall('item'):
        parts=item.get('data','').split('|')
        if len(parts)!=6: raise ValueError('INVALID_CHART_SCHEMA')
        date=pd.to_datetime(parts[0],format='%Y%m%d',errors='raise')
        rows.append([date]+[float(x) for x in parts[1:]])
    if not rows: raise ValueError('EMPTY_DAILY_BARS')
    frame=pd.DataFrame(rows,columns=['Date','Open','High','Low','Close','Volume']).set_index('Date').sort_index()
    if frame.index.has_duplicates: raise ValueError('DUPLICATE_DAILY_DATE')
    now=fetched_at.astimezone(KST)
    cutoff=now.date() if now.hour>=16 else (now-timedelta(days=1)).date()
    complete=frame.index.date<=cutoff
    finite=np.isfinite(frame).all(axis=1)
    good=(finite & frame[['Open','High','Low','Close']].gt(0).all(axis=1) & frame.Volume.ge(0) &
          frame.High.ge(frame[['Open','Low','Close']].max(axis=1)) & frame.Low.le(frame[['Open','High','Close']].min(axis=1)))
    quality=dict(raw_rows=len(frame),incomplete_or_future=int((~complete).sum()),invalid_rows=int((~good&complete).sum()),
                 zero_volume_rows=int((frame.Volume.eq(0)&complete).sum()),cutoff_date=str(cutoff))
    cleaned=frame.loc[complete&good].copy()
    quality['completed_rows']=len(cleaned)
    quality['last_date']=str(cleaned.index[-1].date()) if len(cleaned) else None
    return cleaned,quality


def rank_etfs(universe: dict[str,pd.DataFrame], as_of: str) -> dict:
    """Daily inception-like reference gates; no stock-market regime veto for global ETFs.

    Each stage is ranked independently. Prebreakout retains the same liquidity,
    trend, contraction and ignition requirements; only the breakout comparison
    differs.
    """
    day=pd.Timestamp(as_of)
    rows=[]
    breadth_above=breadth_valid=0
    funnel=dict(evaluated=0,trend_liquidity=0,nonextended=0,contraction=0,ignition=0)
    for code,source in sorted(universe.items()):
        df=source.loc[:day]
        if len(df)<125 or df.index[-1]!=day: continue
        c,h,l,v=(df[k] for k in ('Close','High','Low','Volume'))
        if not np.isfinite(df[['Open','High','Low','Close','Volume']].iloc[-125:]).all().all(): continue
        close=float(c.iloc[-1]); sma10=float(c.iloc[-10:].mean()); sma20=float(c.iloc[-20:].mean()); sma60=float(c.iloc[-60:].mean())
        if not np.isfinite([close,sma10,sma20,sma60]).all() or min(close,sma10,sma20,sma60)<=0: continue
        breadth_valid+=1; breadth_above+=int(close>sma60); funnel['evaluated']+=1
        amount=float((c*v).iloc[-20:].median())
        if not np.isfinite(amount): continue
        if v.iloc[-1]<=0 or close<sma10 or sma10<sma20 or close<sma60 or sma20<sma60*0.99 or amount<500_000_000: continue
        funnel['trend_liquidity']+=1
        mom=float(c.iloc[-5]/c.iloc[-60]-1)
        ret1=float(c.iloc[-1]/c.iloc[-2]-1) if len(c)>=2 and c.iloc[-2]>0 else 0.
        if mom<0.0 or mom>.25 or close/sma60>1.18 or ret1>0.06: continue
        funnel['nonextended']+=1
        h20=float(h.iloc[-21:-1].max()); l20=float(l.iloc[-21:-1].min())
        r20=h20-l20; r60=float(h.iloc[-61:-1].max()-l.iloc[-61:-1].min())
        contraction=r20/r60 if r60>0 else 1.
        width=r20/close
        if width>.22 or contraction>.75: continue
        funnel['contraction']+=1
        prior=float(v.iloc[-25:-5].median())
        vr=float(v.iloc[-5:].median()/prior) if prior>0 else 0.
        spike=float(v.iloc[-1]/prior) if prior>0 else 0.
        multiplier=(2*c.iloc[-20:]-h.iloc[-20:]-l.iloc[-20:])/(h.iloc[-20:]-l.iloc[-20:]).replace(0,np.nan)
        cmf=float((multiplier*v.iloc[-20:]).sum()/v.iloc[-20:].sum()) if v.iloc[-20:].sum()>0 else -1.
        if not np.isfinite([mom,h20,l20,r20,r60,contraction,width,vr,spike,cmf]).all(): continue
        if (vr<1.2 and spike<1.35) or spike>5.0 or cmf<0.0: continue
        funnel['ignition']+=1
        open_p=float(df['Open'].iloc[-1])
        bar_r=float(h.iloc[-1]-l.iloc[-1])
        wick=(float(h.iloc[-1])-max(close,open_p))/bar_r if bar_r>0 else 0.
        if close < h20 * 0.97 or close > h20 * 1.035 or close < open_p or wick > 0.50: continue
        pullback_buy_1 = round(min((close + h20) / 2.0, close * 0.992), 2)
        pullback_buy_2 = round(min(h20, close * 0.985), 2)
        rows.append(dict(code=code,close=close,prior_high20=h20,
                         pullback_buy_1=pullback_buy_1,pullback_buy_2=pullback_buy_2,
                         stop_price_5pct=round(close*0.95,2),sma10=sma10,
                         distance_to_high_pct=(close/h20-1)*100,contraction_ratio=contraction,
                         range_width_ratio=width,volume_ratio5=vr,volume_spike=spike,cmf20=cmf,
                         median_amount=amount,mom60_5=mom,
                         compression=max(0.,1-width/.25),volume_score=vr+spike,
                         range_pos=(close-l20)/r20 if r20>0 else 1.,
                         stage='breakout' if close>=h20 else 'watch'))
    stages={}
    for stage in ('breakout','watch'):
        frame=pd.DataFrame([r for r in rows if r['stage']==stage])
        if frame.empty: stages[stage]=[]; continue
        brk_rank = (-frame.distance_to_high_pct.sub(0.8).abs()).rank(pct=True) if stage=='breakout' else frame.range_pos.rank(pct=True)
        frame['score']=(.35*frame.volume_score.rank(pct=True)+.25*frame.compression.rank(pct=True)+
                        .2*brk_rank+.2*frame.cmf20.rank(pct=True))
        stages[stage]=frame.sort_values(['score','median_amount','code'],ascending=[False,False,True]).head(5).to_dict('records')
    return dict(stages=stages,funnel=funnel,qualified_before_top5=dict(
                    breakout=sum(r['stage']=='breakout' for r in rows),watch=sum(r['stage']=='watch' for r in rows)),
                breadth_pct=100*breadth_above/breadth_valid if breadth_valid else None,breadth_denominator=breadth_valid)


def safe_text(value: str) -> str:
    return escape(str(value)).replace('|','&#124;').replace('\n',' ').replace('[','&#91;').replace(']','&#93;')


def _etf_bar_state_up_to(
    frame: pd.DataFrame | None,
    as_of_ts: pd.Timestamp,
    med_ret20: float | None,
) -> dict | None:
    if frame is None or frame.empty or as_of_ts not in frame.index:
        return None
    hist = frame.loc[:as_of_ts].dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])
    if hist.empty or hist.index[-1] != as_of_ts:
        return None
    c = hist['Close'].astype(float)
    h = hist['High'].astype(float)
    l = hist['Low'].astype(float)
    close_now = float(c.iloc[-1])
    if not np.isfinite(close_now) or close_now <= 0:
        return None
    ret20 = float(c.iloc[-1] / c.iloc[-21] - 1.0) if len(c) >= 21 and float(c.iloc[-21]) > 0 else None
    if ret20 is not None and not np.isfinite(ret20):
        ret20 = None
    rel20 = (ret20 - med_ret20) if (ret20 is not None and med_ret20 is not None and np.isfinite(med_ret20)) else None
    if rel20 is None or ret20 is None:
        rs_label = '상대강도 미산출'
    elif rel20 > 0 and ret20 >= 0:
        rs_label = f'상대강도 우위(20일 {ret20:+.1%} · 중앙대비 {rel20*100:+.1f}%p)'
    elif rel20 > 0 and ret20 < 0:
        rs_label = f'상대강도 우위·절대하락(20일 {ret20:+.1%} · 중앙대비 {rel20*100:+.1f}%p)'
    else:
        rs_label = f'상대강도 열위(20일 {ret20:+.1%} · 중앙대비 {rel20*100:+.1f}%p)'

    con_label = '수축 미산출'
    if len(hist) >= 61:
        r20 = float(h.iloc[-21:-1].max() - l.iloc[-21:-1].min())
        r60 = float(h.iloc[-61:-1].max() - l.iloc[-61:-1].min())
        if r60 > 0 and r20 >= 0:
            cr = r20 / r60
            con_label = f'수축 충족(수축비 {cr:.2f})' if (r20 / close_now <= 0.22 and cr <= 0.75) else f'수축 미충족(수축비 {cr:.2f})'
    sma20 = float(c.iloc[-20:].mean()) if len(c) >= 20 else float('nan')
    return dict(close=close_now, rs_label=rs_label, con_label=con_label, sma20=sma20, hist=hist)


def build_etf_observation_rows(
    record: dict,
    history_records: list[dict] | None = None,
    evaluated_frames: dict[str, pd.DataFrame] | None = None,
    max_horizon: int = 20,
) -> list[dict]:
    """Build read-only ETF tracking & pre-breakout watch rows up to record['as_of'] (no buy/stop fields)."""
    status = record.get('status')
    as_of = record.get('as_of')
    if status not in ('OK', 'PARTIAL') or not as_of:
        return []
    as_of_str = str(as_of)
    as_of_ts = pd.Timestamp(as_of_str)
    frames = evaluated_frames or {}
    result = record.get('result') or {}
    stages = result.get('stages') or {}
    today_breakout_codes = {str(p.get('code', '')) for p in (stages.get('breakout') or [])}

    ret20_vals = []
    for df in frames.values():
        if df is None or df.empty or as_of_ts not in df.index:
            continue
        s = df.loc[:as_of_ts].dropna(subset=['Close'])
        if len(s) >= 21 and s.index[-1] == as_of_ts and float(s['Close'].iloc[-21]) > 0:
            v = float(s['Close'].iloc[-1] / s['Close'].iloc[-21] - 1.0)
            if np.isfinite(v):
                ret20_vals.append(v)
    med_ret20 = float(np.median(ret20_vals)) if ret20_vals else None

    prior_list = sorted(
        [h for h in (history_records or []) if isinstance(h, dict) and str(h.get('as_of') or '') < as_of_str],
        key=lambda x: str(x['as_of']),
    )
    cal_days = {as_of_str} | {str(h['as_of']) for h in prior_list}
    for df in frames.values():
        if df is not None and not df.empty:
            for ts in df.loc[:as_of_ts].dropna(subset=['Close']).index[-35:]:
                cal_days.add(pd.Timestamp(ts).date().isoformat())
    ordered = sorted(d for d in cal_days if d <= as_of_str)
    pos = {d: i for i, d in enumerate(ordered)}
    as_pos = pos[as_of_str]

    tracked: dict[str, dict] = {}
    for h_rec in prior_list:
        h_day = str(h_rec['as_of'])
        if h_day in pos and (as_pos - pos[h_day]) > max_horizon:
            continue
        h_stages = h_rec.get('stages') or (h_rec.get('result') or {}).get('stages') or {}
        for st_key, st_lbl in (('breakout', '돌파확정'), ('watch', '수축관찰')):
            for p in h_stages.get(st_key) or []:
                c_code = str(p.get('code', '')).strip()
                if not c_code or c_code in today_breakout_codes:
                    continue
                if c_code not in tracked:
                    tracked[c_code] = dict(
                        code=c_code,
                        name=str(p.get('name', c_code)),
                        first_date=h_day,
                        first_close=float(p.get('close', 0.0) or 0.0),
                        origin_label=st_lbl,
                    )

    out_rows: list[dict] = []
    for c_code, info in tracked.items():
        st = _etf_bar_state_up_to(frames.get(c_code), as_of_ts, med_ret20)
        f_date, f_close = info['first_date'], info['first_close']
        if st is not None:
            elapsed = int((st['hist'].index > pd.Timestamp(f_date)).sum())
            if elapsed > max_horizon:
                continue
            cur_c = st['close']
            chg = (cur_c / f_close - 1.0) * 100.0 if f_close > 0 else 0.0
            path_str = f"{cur_c:,.0f}원 ({chg:+.1f}% · {elapsed}거래일 경과)"
            trend_note = 'SMA20 상회 유지' if (np.isfinite(st['sma20']) and cur_c >= st['sma20']) else 'SMA20 하회'
            status_str = f"{st['rs_label']} · {st['con_label']} · `{trend_note}`"
        else:
            path_str = '시세 미수신 (일봉 조회 시 갱신)'
            status_str = f"원 기록: `{info['origin_label']}` · 시세 확인 대기"
        out_rows.append(dict(
            category=f"이전 포착 추적 (`{info['origin_label']}`)",
            code=c_code,
            name=info['name'],
            first_capture_str=f"`{f_date}` ({f_close:,.0f}원)" if f_close > 0 else f"`{f_date}`",
            current_path_str=path_str,
            status_str=status_str,
        ))

    seen = set(today_breakout_codes) | set(tracked.keys())
    for w_pick in stages.get('watch') or []:
        w_code = str(w_pick.get('code', '')).strip()
        if not w_code or w_code in seen:
            continue
        seen.add(w_code)
        w_close = float(w_pick.get('close', 0.0) or 0.0)
        w_name = str(w_pick.get('name', w_code))
        dist = float(w_pick.get('distance_to_high_pct', 0.0) or 0.0)
        c_rat = float(w_pick.get('contraction_ratio', 0.0) or 0.0)
        st = _etf_bar_state_up_to(frames.get(w_code), as_of_ts, med_ret20)
        rs_lbl = st['rs_label'] if st is not None else '상대강도 미산출'
        out_rows.append(dict(
            category='수축 관찰 (미돌파)',
            code=w_code,
            name=w_name,
            first_capture_str=f"`{as_of_str}` ({w_close:,.0f}원)" if w_close > 0 else f"`{as_of_str}`",
            current_path_str=f"{w_close:,.0f}원 (20일 고점대비 {dist:+.2f}%)",
            status_str=f"{rs_lbl} · 수축 충족(수축비 {c_rat:.2f}) · **`20일 고점 미돌파로 제외`**",
        ))
    return out_rows


def render_panel(record: dict) -> str:
    labels={'OK':'정상 확인','PARTIAL':'일부 조회 실패 · 조회된 ETF만 비교','STALE':'자료가 오래되어 후보 산출 보류',
            'ERROR':'조회 실패 · 후보 산출 보류','INSUFFICIENT_COVERAGE':'동일 날짜 자료 부족 · 후보 산출 보류'}
    status=record['status']; result=record.get('result',{})
    lines=['### ETF 상승 초입 · 20일 돌파 확정','',
           f"> 갱신: `{record['fetched_at']}` · 기준 완료 일봉: `{record.get('as_of') or '미확인'}` · 실행 상태: {labels[status]} (연금저축·DC/IRP 적격 `{record.get('selected_count',0)}`개 중 `{record.get('fresh_count',0)}`개 확인)",'']
    if result.get('breadth_pct') is not None:
        f=result.get('funnel',{})
        q=result.get('qualified_before_top5',{'breakout':0,'watch':0})
        lines += [
            f"> 관찰군 60일선(SMA60) 상회 비율 **{result['breadth_pct']:.1f}%** (`{result['breadth_denominator']}`개 기준) · "
            f"**ETF 게이트별 생존 (`{f.get('evaluated',0)}`개 평가):** "
            f"추세·대금 `{f.get('trend_liquidity',0)}` → "
            f"비과열 `{f.get('nonextended',0)}` → "
            f"20일 수축 `{f.get('contraction',0)}` → "
            f"수급 점화 `{f.get('ignition',0)}` → "
            f"**최종 돌파 확정 `{q.get('breakout',0)}`건**",
            '',
        ]
    picks=result.get('stages',{}).get('breakout',[]) if status in ('OK','PARTIAL') else []
    cash_picks=sum(bool(CASH_RATE.search(p.get('name',''))) for p in picks)
    if cash_picks and status in ('OK','PARTIAL'):
        lines += [f'> 표시 종목 중 **금리·현금형 {cash_picks}개**가 포함되어 있습니다(상품명 기준). 이들 상품의 고점 갱신은 주식형 ETF의 상승초입과 성격이 다릅니다.','']
    if record.get('errors'):lines += [f"> 조회 실패 {len(record['errors'])}개 · 과거 값을 최신 값으로 대신하지 않았습니다.",'']
    lines += ['| 순위 | ETF (연금계좌) | 완료일 종가 (추격금지) | 익일 눌림 매수가 (1차 / 2차) | 20일 기준고점 (돌파이격) | 참조 손절 / 추세선 | 수치 근거 |',
              '|---:|:---|---:|:---|:---|:---|:---|']
    for i,pick in enumerate(picks,1):
        name=safe_text(pick.get('name',pick['code'])); code=pick['code']
        raw_name=pick.get('name','')
        if pick.get('leveraged_or_inverse'):
            flag=' · 레버리지/인버스'
        elif CASH_RATE.search(raw_name):
            flag=' · 금리·현금형'
        elif PENSION_INELIGIBLE.search(raw_name):
            flag=' · 파생/합성확인'
        else:
            flag=' · `연금저축·DC/IRP`'
        pb1=pick.get('pullback_buy_1') or min((pick['close']+pick['prior_high20'])/2.0, pick['close']*0.992)
        pb2=pick.get('pullback_buy_2') or min(pick['prior_high20'], pick['close']*0.985)
        pb_col=f"**{pb1:,.0f}원** / {pb2:,.0f}원"
        stop_5=pick.get('stop_price_5pct') or (pick['close']*0.95)
        sma10=pick.get('sma10')
        ref_col=f"-5% {stop_5:,.0f}원 · SMA10 {sma10:,.0f}원" if sma10 else f"-5% {stop_5:,.0f}원"
        cmf_val=pick.get('cmf20',0.0)
        width_val=pick.get('range_width_ratio',0.0)
        lines.append(
            f"| {i} | [{name}](https://finance.naver.com/item/main.naver?code={code}) (`{code}`){flag} | "
            f"{pick['close']:,.0f}원 | {pb_col} | 기준 {pick['prior_high20']:,.0f}원 ({pick['distance_to_high_pct']:+.2f}%) | "
            f"{ref_col} | "
            f"수축비 {pick['contraction_ratio']:.2f} (변동폭 {width_val:.1%}) · "
            f"5일 {pick['volume_ratio5']:.2f}x (당일 {pick['volume_spike']:.2f}x) · CMF20 {cmf_val:+.2f} |"
        )
    if not picks:
        message='조건 충족 없음' if status in ('OK','PARTIAL') else '자료 확인 후 산출'
        lines.append(f'| - | {message} | - | - | - | - | - |')
    lines.append('')
    if status in ('OK','PARTIAL'):
        obs_rows = record.get('observation_rows')
        if not isinstance(obs_rows, list):
            obs_rows = build_etf_observation_rows(record)
        lines += [
            '#### 🔍 ETF 수축 관찰 및 포착 추적 (매수 추천 아님 · 경로 확인 전용)',
            '',
            '> 매수가·수량·손절선을 제시하지 않는 관찰 전용 기록입니다. **이전 포착 추적**은 최근 20거래일 내 포착 ETF의 이후 경로이며, **수축 관찰 (미돌파)**은 수축·수급 조건을 통과했으나 **20일 고점 미돌파로 제외**된 ETF입니다.',
            '',
            '| 구분 | ETF (연금계좌) | 최초 포착일 (당시 종가) | 완료일 종가 (포착 대비 경로) | 상대강도 · 수축 · 상태 |',
            '|:---|:---|:---|:---|:---|',
        ]
        for row in obs_rows:
            r_name = safe_text(row.get('name', row.get('code', '')))
            r_code = safe_text(row.get('code', ''))
            lines.append(
                f"| {row['category']} | [{r_name}](https://finance.naver.com/item/main.naver?code={r_code}) (`{r_code}`) | "
                f"{row['first_capture_str']} | {row['current_path_str']} | {row['status_str']} |"
            )
        if not obs_rows:
            lines.append('| - | 관찰 대상 없음 | - | - | - |')
        lines.append('')
    return '\n'.join(lines)


def update_readme(path: Path, panel: str) -> None:
    """Only own marker block, read immediately before writing to retain other panels."""
    text=path.read_text(encoding='utf-8')
    block=START+'\n'+panel.rstrip()+'\n'+END
    if text.count(START)==1 and text.count(END)==1:
        a=text.index(START); b=text.index(END,a)+len(END)
        new=text[:a]+block+text[b:]
    elif START in text or END in text: raise ValueError('MALFORMED_ETF_MARKERS')
    elif '<!-- DUAL_FORWARD:END -->' in text:
        anchor='<!-- DUAL_FORWARD:END -->'; pos=text.index(anchor)+len(anchor)
        new=text[:pos]+'\n\n'+block+'\n'+text[pos:]
    else: new=text.rstrip()+'\n\n'+block+'\n'
    if path.read_text(encoding='utf-8')!=text: raise RuntimeError('README_CHANGED_RETRY')
    temp=path.with_name(path.name+'.etf.tmp'); temp.write_text(new,encoding='utf-8'); temp.replace(path)
