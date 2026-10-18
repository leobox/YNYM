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
        if v.iloc[-1]<=0 or close<sma20 or (close<sma60 and sma20<sma60) or amount<500_000_000: continue
        funnel['trend_liquidity']+=1
        mom=float(c.iloc[-5]/c.iloc[-60]-1)
        if mom>.25 or close/sma60>1.18: continue
        funnel['nonextended']+=1
        h20=float(h.iloc[-21:-1].max()); l20=float(l.iloc[-21:-1].min())
        r20=h20-l20; r60=float(h.iloc[-61:-1].max()-l.iloc[-61:-1].min())
        contraction=r20/r60 if r60>0 else 1.
        width=r20/close
        if width>.25 or contraction>.85: continue
        funnel['contraction']+=1
        prior=float(v.iloc[-25:-5].median())
        vr=float(v.iloc[-5:].median()/prior) if prior>0 else 0.
        spike=float(v.iloc[-1]/prior) if prior>0 else 0.
        multiplier=(2*c.iloc[-20:]-h.iloc[-20:]-l.iloc[-20:])/(h.iloc[-20:]-l.iloc[-20:]).replace(0,np.nan)
        cmf=float((multiplier*v.iloc[-20:]).sum()/v.iloc[-20:].sum()) if v.iloc[-20:].sum()>0 else -1.
        if not np.isfinite([mom,h20,l20,r20,r60,contraction,width,vr,spike,cmf]).all(): continue
        if (vr<1.2 and spike<1.35) or cmf<-.05: continue
        funnel['ignition']+=1
        rows.append(dict(code=code,close=close,prior_high20=h20,
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
        frame['score']=(.35*frame.volume_score.rank(pct=True)+.25*frame.compression.rank(pct=True)+
                        .2*frame.range_pos.rank(pct=True)+.2*frame.cmf20.rank(pct=True))
        stages[stage]=frame.sort_values(['score','median_amount','code'],ascending=[False,False,True]).head(5).to_dict('records')
    return dict(stages=stages,funnel=funnel,qualified_before_top5=dict(
                    breakout=sum(r['stage']=='breakout' for r in rows),watch=sum(r['stage']=='watch' for r in rows)),
                breadth_pct=100*breadth_above/breadth_valid if breadth_valid else None,breadth_denominator=breadth_valid)


def safe_text(value: str) -> str:
    return escape(str(value)).replace('|','&#124;').replace('\n',' ').replace('[','&#91;').replace(']','&#93;')


def render_panel(record: dict) -> str:
    labels={'OK':'정상 확인','PARTIAL':'일부 조회 실패 · 조회된 ETF만 비교','STALE':'자료가 오래되어 후보 산출 보류',
            'ERROR':'조회 실패 · 후보 산출 보류','INSUFFICIENT_COVERAGE':'동일 날짜 자료 부족 · 후보 산출 보류'}
    status=record['status']; result=record.get('result',{})
    lines=['## 📊 ETF 상승 초입 관찰 (연금계좌 매수 가능)','',
           f"> 실행 상태: {labels[status]} · 기준 완료 일봉 `{record.get('as_of') or '미확인'}`",
           f"> 조회: `{record['fetched_at']}` · 연금저축·퇴직연금(DC/IRP) 매수 가능 ETF `{record.get('selected_count',0)}`개 중 동일 날짜 `{record.get('fresh_count',0)}`개",'']
    scope='레버리지·인버스 포함' if record.get('include_leveraged') else '레버리지·인버스, 선물·합성 파생형, 금리·머니마켓·채권 파킹형 제외'
    lines += [f'- **연금계좌 대상 범위:** 실제 **연금저축펀드·퇴직연금(DC/IRP)** 계좌에서 매수 가능한 국내·해외 주식·섹터·테마 및 `KRX금현물` ETF ({scope}, 거래대금 상위 100개).',
              '- **활용 안내:** 개별주 상승 초입과 동일한 완료 일봉 수축·수급 점화 기준으로 **① 20일 돌파 확정**과 **② 20일 수축 돌파임박**을 각각 최대 5개 선별합니다.','']
    if result.get('breadth_pct') is not None:
        f=result.get('funnel',{})
        q=result.get('qualified_before_top5',{'breakout':0,'watch':0})
        lines += [
            f"> 관찰군 60일선(SMA60) 상회 비율 **{result['breadth_pct']:.1f}%** (`{result['breadth_denominator']}`개 기준, 해외·금현물 포함 참고치)",
            '',
            f"> **ETF 게이트별 생존 (`{f.get('evaluated',0)}`개 평가):** "
            f"추세·대금 `{f.get('trend_liquidity',0)}` → "
            f"비과열 `{f.get('nonextended',0)}` → "
            f"20일 수축 `{f.get('contraction',0)}` → "
            f"수급 점화 `{f.get('ignition',0)}` → "
            f"**최종 통과: 돌파 확정 `{q.get('breakout',0)}`건 / 수축 돌파임박 `{q.get('watch',0)}`건**",
            '',
        ]
    cash_picks=sum(bool(CASH_RATE.search(p.get('name',''))) for picks in result.get('stages',{}).values() for p in picks)
    if cash_picks and status in ('OK','PARTIAL'):
        lines += [f'> 표시 종목 중 **금리·현금형 {cash_picks}개**가 포함되어 있습니다(상품명 기준). 이들 상품의 고점 갱신은 주식형 ETF의 상승초입과 성격이 다릅니다.','']
    if record.get('errors'):lines += [f"> 조회 실패 {len(record['errors'])}개 · 과거 값을 최신 값으로 대신하지 않았습니다.",'']
    for stage,title in [('breakout','20일 돌파 확정'),('watch','20일 수축 돌파임박')]:
        lines += [f'### ETF 상승 초입 · {title}','',
                  '| 순위 | ETF (연금계좌) | 완료일 종가 | 20일 기준고점 (돌파이격) | 참조 손절 / 추세선 | 상대점수 | 수치 근거 |',
                  '|---:|:---|---:|:---|:---|---:|:---|']
        picks=result.get('stages',{}).get(stage,[]) if status in ('OK','PARTIAL') else []
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
            stop_5=pick.get('stop_price_5pct') or (pick['close']*0.95)
            sma10=pick.get('sma10')
            ref_col=f"-5% {stop_5:,.0f}원 · SMA10 {sma10:,.0f}원" if sma10 else f"-5% {stop_5:,.0f}원"
            cmf_val=pick.get('cmf20',0.0)
            width_val=pick.get('range_width_ratio',0.0)
            score_val=pick.get('score',0.0)
            lines.append(
                f"| {i} | [{name}](https://finance.naver.com/item/main.naver?code={code}) (`{code}`){flag} | "
                f"{pick['close']:,.0f}원 | 기준 {pick['prior_high20']:,.0f}원 ({pick['distance_to_high_pct']:+.2f}%) | "
                f"{ref_col} | {score_val:.3f} | "
                f"수축비 {pick['contraction_ratio']:.2f} (변동폭 {width_val:.1%}) · "
                f"5일 {pick['volume_ratio5']:.2f}x (당일 {pick['volume_spike']:.2f}x) · CMF20 {cmf_val:+.2f} |"
            )
        if not picks:
            message='조건 충족 없음' if status in ('OK','PARTIAL') else '자료 확인 후 산출'
            lines.append(f'| - | {message} | - | - | - | - | - |')
        lines.append('')
    lines += ['<details>','<summary>ETF 상승 초입 (연금계좌) 관찰 기준 및 유의사항</summary>','',
              '- **연금계좌 적격 필터:** 연금저축펀드·퇴직연금(DC/IRP)에서 매수 불가능한 **레버리지·인버스(Tab 3)**, 파생상품 위험평가액 한도(40%)로 제한되는 **선물·원자재파생·합성형 ETF(`선물`, `WTI`, `합성` 등 — 단 실물 `KRX금현물`은 허용)**, 그리고 일간 이자 누적으로 차트가 왜곡되는 **금리·머니마켓·채권 파킹형(Tab 6·7)**을 모두 제외한 실물 주식·섹터·테마·해외지수·금현물 ETF 상위 100개를 평가합니다.',
              '- **추세·대금:** 종가 ≥ SMA20, SMA20/60 정배열(또는 종가 ≥ SMA60), 20일 중앙 거래대금 ≥ 5억원.',
              '- **비과열·수축·수급:** 60일 모멘텀 ≤ 25%, SMA60 이격 ≤ 1.18배, 20일 변동폭 ≤ 25%, 수축비(20일/60일 변동폭) ≤ 0.85, 5일 거래량비 ≥ 1.20x(또는 당일 ≥ 1.35x), CMF20 ≥ −0.05.',
              '- **돌파 확정 vs 수축 돌파임박:** 종가 ≥ 직전 20일 고점이면 **20일 돌파 확정**, 기초 수축·수급 점화를 모두 충족한 채 직전 20일 고점 아래에서 돌파를 준비 중이면 **20일 수축 돌파임박**으로 분류합니다.',
              '- 퇴직연금(DC/IRP)은 안전자산 30% 의무 편입 규정(주식형 ETF 최대 70% 한도)이 적용되므로 계좌별 위험자산 한도를 확인하세요. 네이버 금융 공개 일봉 기준의 관찰 참조값이며 실제 주문·체결 기능은 포함하지 않습니다.','', '</details>','']
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
