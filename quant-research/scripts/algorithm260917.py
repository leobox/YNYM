"""Colab: paste this whole file into one cell and run. Table only, no orders."""
import sys
import subprocess
import importlib
import time
from html import escape
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

SCAN_LIMIT = 150
TOP_N = 5
WORKERS = 6
MCAP_MIN, MCAP_MAX = 100_000_000_000, 5_000_000_000_000
MIN_PRICE = 2000
VOLUME_WEIGHT = 0.2
MAX_EXTENSION_ATR = None
SAVE_SNAPSHOTS = True
SNAPSHOT_DIR = 'pattern_snapshots'
_PATTERN_SEEN = globals().get('_PATTERN_SEEN', set())


def get_json(url, params=None):
    last = None
    for attempt in range(2):
        try:
            r = requests.get(url, params=params, headers={'User-Agent':'Mozilla/5.0'}, timeout=20)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            if attempt == 0:
                time.sleep(.5)
    raise RuntimeError(str(last)[:140])


def universe():
    # Official-company list is used only to exclude ETFs/ETNs from Naver's mixed list.
    try:
        fdr = importlib.import_module('FinanceDataReader')
    except ImportError:
        subprocess.check_call([sys.executable,'-m','pip','install','-q','finance-datareader'])
        fdr = importlib.import_module('FinanceDataReader')
    listing = fdr.StockListing('KRX')
    valid = set(listing.loc[listing.Market.isin(['KOSPI','KOSDAQ','KOSDAQ GLOBAL']), 'Code'].astype(str))
    if not valid:
        raise RuntimeError('상장 종목 목록이 비어 있습니다. 잠시 후 다시 실행하세요.')
    rows = []
    number = lambda x: pd.to_numeric(str(x).replace(',',''), errors='coerce')
    for market in ('KOSPI','KOSDAQ'):
        for page in range(1,31):
            stocks = get_json(f'https://m.stock.naver.com/api/stocks/marketValue/{market}',
                              {'page':page,'pageSize':100})['stocks']
            if not stocks:
                break
            for x in stocks:
                code, name = x['itemCode'], x['stockName']
                cap, price = number(x['marketValue'])*1e8, number(x['closePrice'])
                if code not in valid or not code.endswith('0') or '스팩' in name or '리츠' in name:
                    continue
                if MCAP_MIN <= cap <= MCAP_MAX and price >= MIN_PRICE:
                    rows.append({'code':code,'name':name,'market':market,
                                 'price':price,'market_cap':cap,'quote_time':x.get('localTradedAt'),
                                 'amount':number(x['accumulatedTradingValue'])})
            if number(stocks[-1]['marketValue'])*1e8 < MCAP_MIN:
                break
    if not rows:
        raise RuntimeError('조회 가능한 중소형주가 없습니다. 잠시 후 다시 실행하세요.')
    return (pd.DataFrame(rows).dropna(subset=['amount']).sort_values('amount',ascending=False)
            .drop_duplicates('code').head(SCAN_LIMIT).to_dict('records'))


def completed_bars(item, now):
    df = pd.DataFrame(item['indicators']['quote'][0],
                      index=pd.to_datetime(item['timestamp'],unit='s',utc=True).tz_convert('Asia/Seoul'))
    df = df.rename(columns=str.title)[['Open','High','Low','Close','Volume']].dropna()
    df = df[~df.index.duplicated()].sort_index()
    df = df[(df.index.minute==0)&(df.index.second==0)&(df.index.hour>=9)&(df.index.hour<=15)]
    # Yahoo may append a zero-volume 15:00 quote placeholder after the session.
    df = df[~((df.index.hour==15)&(df.Volume==0)&(df.High==df.Low))]
    ends = df.index+pd.to_timedelta(np.where(df.index.hour==15,30,60),unit='m')
    return df[ends<=now]


def fetch(rec, now):
    symbol = rec['code']+('.KQ' if rec['market']=='KOSDAQ' else '.KS')
    payload = get_json('https://query1.finance.yahoo.com/v8/finance/chart/'+symbol,
                       {'range':'60d','interval':'60m'})['chart']
    if not payload['result']:
        raise RuntimeError(str(payload.get('error')))
    return completed_bars(payload['result'][0],now)


def _atr(df):
    previous = df.Close.shift(1)
    tr = pd.concat([df.High-df.Low,(df.High-previous).abs(),(df.Low-previous).abs()],axis=1).max(axis=1)
    return tr.rolling(14).mean().replace(0,np.nan)


# Same scoring as strategy.hourly_pattern; only ATR dependency is inlined.
def hourly_pattern(df, require_volume=True, volume_weight=0.2, max_extension_atr=None):
    """Causal MA/volume pattern scores; research hypothesis, not calibrated odds."""
    c, v = df['Close'], df['Volume']
    f, m = c.rolling(12).mean(), c.rolling(26).mean()
    atr = _atr(df)
    gap = (f - m) / atr
    fs, ms = (f - f.shift(3)) / atr, (m - m.shift(3)) / atr
    # Compare volume per minute because the final regular-session bar is 30 min.
    minutes = pd.Series(np.where(df.index.hour == 15, 30, 60), index=df.index)
    rate = v / minutes
    vr = rate.rolling(3).mean() / rate.shift(3).rolling(20).mean().replace(0, np.nan)
    clip = lambda x: x.clip(0, 100)
    proximity = clip(100 - gap.abs() * 40)
    direction = clip(50 + fs * 60 + ms * 30)
    volume = clip((vr - 0.7) / 1.8 * 100)
    room = clip(100 - ((c - m) / atr - 1).clip(lower=0) * 25)
    contraction = clip(50 + (gap.shift(6).abs() - gap.abs()) * 30)
    rebound = (proximity + direction + volume + room + contraction) / 5
    prior_width = (df.High.shift(1).rolling(6).max() - df.Low.shift(1).rolling(6).min()) / atr
    base = clip(100 - prior_width * 15)
    breakout = clip(50 + (c - df.High.shift(1).rolling(6).max()) / atr * 50)
    expansion = (base + breakout + direction + volume + room) / 5
    # A low MA gap alone must never qualify a falling chart.
    eligible = (fs > 0) & (ms > ms.shift(3)) & (c > f) & (c > m)
    if require_volume:
        eligible &= vr >= 1
    eligible &= (gap.shift(1).rolling(20).min() < 0)
    eligible &= c >= 2000
    score = np.maximum(rebound, expansion)
    if volume_weight != 0.2:
        score = (score - 0.2 * volume) * (1 - volume_weight) / 0.8 + volume_weight * volume
    if max_extension_atr is not None:
        eligible &= (c - m) / atr <= max_extension_atr
    out = pd.DataFrame({'score': score, 'volume_ratio': vr,
                        'pattern': np.where(rebound >= expansion, 'rebound', 'base_breakout'),
                        'above60': c > c.rolling(60).mean(),
                        'eligible': eligible}, index=df.index)
    out.loc[~np.isfinite(out.score), 'eligible'] = False
    return out


def same_hour_volume(df):
    """Latest completed bar / mean of up to 20 earlier bars at the same hour."""
    last = df.index[-1]
    previous = df.loc[(df.index < last) & (df.index.hour == last.hour), 'Volume'].tail(20)
    baseline = previous.mean()
    return float(df.Volume.iloc[-1] / baseline) if len(previous) >= 5 and baseline > 0 else np.nan


def confirmed_breakout(df):
    """Exact hold filter from backtest_clear_trigger, using completed bars only."""
    c=df.Close
    fast=c.rolling(12).mean();medium=c.rolling(26).mean()
    trend=(fast>fast.shift(3))&(medium>medium.shift(3))&(c>fast)&(c>medium)
    level=df.High.shift(1).rolling(6).max()
    location=(c-df.Low)/(df.High-df.Low).replace(0,np.nan)
    extension=(c-medium)/_atr(df)
    amount=c*df.Volume
    typical=amount.groupby(df.index.hour).transform(lambda x:x.shift(1).rolling(20,min_periods=5).median())
    ratio=amount/typical.replace(0,np.nan)
    breakout=trend&(c>level)&(c>df.Open)&(location>=.7)&(extension<=4)
    liquid=breakout&(amount>=1e9)
    trigger=liquid&(amount>=typical*2)
    hold_price=trend&(df.Low>=level.shift(1))&(c>=c.shift(1))&(extension<=4)
    held=trigger.shift(1,fill_value=False)&hold_price
    return pd.DataFrame({'hold':held,'previous_breakout':breakout.shift(1,fill_value=False),
                         'previous_liquid':liquid.shift(1,fill_value=False),
                         'previous_trigger':trigger.shift(1,fill_value=False),'waiting':trigger,
                         'hold_price':hold_price,'current_amount':amount/1e8,
                         'current_ratio':ratio,'current_level':level,'trigger_amount':amount.shift(1)/1e8,
                         'trigger_ratio':ratio.shift(1),'breakout_level':level.shift(1)},index=df.index)


def valid_ohlcv(df):
    if df.empty or df.index.has_duplicates or not df.index.is_monotonic_increasing:
        return False
    if not np.isfinite(df[['Open','High','Low','Close','Volume']].to_numpy(dtype=float)).all():
        return False
    bad = ((df[['Open','High','Low','Close']]<=0).any(axis=1) | (df.Volume<0) |
           (df.High<df[['Open','Close']].max(axis=1)) | (df.Low>df[['Open','Close']].min(axis=1)))
    return not bool(bad.any())


def tag_repeats(result, day, seen):
    result = result.copy()
    labels=[]
    for code in result['코드']:
        item=(day,code)
        labels.append('재등장' if item in seen else '신규')
        seen.add(item)
    result.insert(3,'등장',labels)
    return result


def score(rec, now):
    try:
        df = fetch(rec,now)
        if not valid_ohlcv(df):
            return None,'가격/거래량 정합성 오류'
        if len(df)<120 or df.Volume.iloc[-1]<=0 or df.Close.iloc[-1]<MIN_PRICE:
            return None,'봉 부족/거래 정지/가격 조건'
        if now-df.index[-1]>pd.Timedelta(days=7):
            return None,'최근 7일 데이터 없음'
        s = hourly_pattern(df,volume_weight=VOLUME_WEIGHT,max_extension_atr=MAX_EXTENSION_ATR).iloc[-1]
        confirmation = confirmed_breakout(df).iloc[-1]
        matched = bool(s.eligible and confirmation.hold)
        if not np.isfinite(s.score):
            return None,'지표 계산 불가'
        c = df.Close
        delta = c.diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
        rsi = 100-100/(1+gain.iloc[-1]/loss.iloc[-1]) if loss.iloc[-1]>0 else (100 if gain.iloc[-1]>0 else 50)
        fast,medium = c.rolling(12).mean(),c.rolling(26).mean()
        return {'종목':rec['name'],'코드':rec['code'],
                '구분':'일치' if matched else '제외',
                '패턴':'돌파 후 유지',
                '점수':float(s.score),'RSI':round(float(rsi),1),
                '12/26선':'12>26' if fast.iloc[-1]>medium.iloc[-1] else '12≤26',
                '이격ATR':round(float((c.iloc[-1]-medium.iloc[-1])/_atr(df).iloc[-1]),2),
                '돌파봉대금(추정·억원)':round(float(confirmation.trigger_amount),1),
                '돌파봉대금배수':round(float(confirmation.trigger_ratio),2),
                '돌파선':round(float(confirmation.breakout_level),2),
                '돌파봉종가':float(c.iloc[-2]),
                '돌파봉(KST)':df.index[-2].strftime('%m-%d %H:%M'),
                '기준봉(KST)':df.index[-1].strftime('%m-%d %H:%M'),
                '_base':bool(s.eligible),
                '_breakout':bool(s.eligible and confirmation.previous_breakout),
                '_liquid':bool(s.eligible and confirmation.previous_liquid),
                '_trigger':bool(s.eligible and confirmation.previous_trigger),
                '_waiting':bool(s.eligible and confirmation.waiting),
                '_hold_price':bool(confirmation.hold_price),
                '_relative':bool(confirmation.trigger_ratio>=2),
                '_current_amount':float(confirmation.current_amount),
                '_current_ratio':float(confirmation.current_ratio),
                '_current_level':float(confirmation.current_level),
                '_current_close':float(c.iloc[-1]),
                '_bar_time':df.index[-1].isoformat(),
                '_match':matched},None
    except Exception as e:
        return None,str(e)[:140]


def top_five(rows):
    if not rows:
        return pd.DataFrame()
    candidates = pd.DataFrame(rows)
    result = (candidates[candidates['_match']].sort_values(['점수','코드'],ascending=[False,True])
              .head(TOP_N).drop(columns=[c for c in candidates if c.startswith('_')]).reset_index(drop=True))
    result['점수'] = result['점수'].round(1)
    result.insert(0,'순위',range(1,len(result)+1))
    return result


def diagnostics(rows):
    """Explain empty results without changing eligibility or the ranking."""
    if not rows:
        return []
    count=lambda key:sum(bool(r[key]) for r in rows)
    times=pd.Series([pd.Timestamp(r['_bar_time']) for r in rows])
    distribution=times.value_counts().sort_index()
    stamps=' / '.join(f'{t:%m-%d %H:%M} {n}종목' for t,n in distribution.items())
    return [f"조건 통과: 기본패턴 {count('_base')} → 직전봉 돌파 {count('_breakout')} → 10억원 {count('_liquid')} → 동시간 2배 {count('_trigger')} → 유지 확인 {count('_match')}",
            f"기본패턴·현재봉 돌파·대금 충족 {count('_waiting')}종목: 다음 봉 유지 확인 전이며 매수 후보가 아닙니다.",
            '기준봉 분포(KST·봉 시작): '+stamps,
            '종목별 마지막 완료봉 기준입니다. 과거 시각이면 지연 또는 거래일 여부를 확인하세요.']


def watch_table(rows):
    observations=[]
    for r in rows:
        if r['_match'] or not r['_base']:
            continue
        if r['_waiting']:
            observations.append({'종목':r['종목'],'코드':r['코드'],'단계':'현재봉 돌파·대금 충족',
                '확인할 조건':'다음 완료봉 유지 확인',
                '대금(추정·억원)':round(r['_current_amount'],1),'동시간배수':round(r['_current_ratio'],2),
                '돌파선':round(r['_current_level'],2),'돌파봉종가':r['_current_close'],'돌파봉(KST)':r['기준봉(KST)'],
                '기준봉(KST)':r['기준봉(KST)']})
    return pd.DataFrame(observations)


def naver_quote(code):
    number=lambda x:pd.to_numeric(str(x).replace(',',''),errors='coerce')
    try:
        q=get_json(f'https://m.stock.naver.com/api/stock/{code}/basic')
        price=number(q.get('closePrice'))
        if not np.isfinite(price) or price<=0:
            raise ValueError('현재가 없음')
        result={'현재가':float(price),'당일등락률':number(q.get('fluctuationsRatio')),
                '시세시각':q.get('localTradedAt') or '시각 미제공',
                '시세출처':'네이버 국내 · 통합 여부 미확인',
                '시세상태':q.get('marketStatus','미확인'),'시세오류':''}
    except Exception:
        return {'현재가':np.nan,'시세오류':'시세 조회 실패'}
    try:
        info=get_json(f'https://m.stock.naver.com/api/stock/{code}/integration')
        values={x['code']:x.get('value') for x in info.get('totalInfos',[])}
        result['당일고가']=number(values.get('highPrice'))
        result['당일누적대금']=values.get('accumulatedTradingValue') or '미제공'
    except Exception:
        result['당일누적대금']='조회 실패'
    return result


def attach_quotes(tables):
    codes=sorted({str(code) for t in tables if not t.empty for code in t['코드']})
    quotes={code:naver_quote(code) for code in codes}
    output=[]
    for table in tables:
        t=table.copy()
        if not t.empty:
            extra=pd.DataFrame([quotes[str(code)] for code in t['코드']],index=t.index)
            for col in extra:t[col]=extra[col]
        output.append(t)
    return output


def mobile_table_html(table):
    def price(x):
        return f'{x:,.0f}원' if pd.notna(x) else '—'
    def change(x):
        if pd.isna(x):return '<span class="muted">—</span>'
        color='up' if x>0 else ('down' if x<0 else 'muted')
        return f'<b class="{color}">{x:+.2f}%</b>'
    body=[]
    watch='단계' in table.columns
    for r in table.to_dict('records'):
        current=r.get('현재가',np.nan);level=r['돌파선'];close=r.get('돌파봉종가',np.nan)
        name=escape(str(r['종목']));code=escape(str(r['코드']))
        state='다음 봉 확인' if watch else '조건 충족'
        tag='wait' if watch else 'ready'
        amount=r['대금(추정·억원)'] if watch else r['돌파봉대금(추정·억원)']
        ratio=r['동시간배수'] if watch else r['돌파봉대금배수']
        gap=lambda ref:(current/ref-1)*100 if pd.notna(current) and pd.notna(ref) and ref>0 else np.nan
        stamp=str(r.get('시세시각','시세 없음'))
        try:stamp=pd.Timestamp(stamp).tz_convert('Asia/Seoul').strftime('%m-%d %H:%M:%S')
        except (ValueError,TypeError):pass
        note=escape(str(r.get('시세오류','')))
        high=r.get('당일고가',np.nan)
        retreat=f'{current-high:+,.0f}원' if pd.notna(current) and pd.notna(high) else '—'
        body.append(f'<tr class="heading"><td colspan="2"><b>{name}</b> <small>{code}</small>'
                    f'<span class="badge {tag}">{state}</span></td></tr>'
                    f'<tr><td>네이버 현재가</td><td><strong>{price(current)}</strong> {note}</td></tr>'
                    f'<tr><td>돌파선</td><td>{price(level)} <span class="muted">대비</span> {change(gap(level))}</td></tr>'
                    f'<tr><td>돌파봉 종가</td><td>{price(close)} <span class="muted">대비</span> {change(gap(close))}</td></tr>'
                    f'<tr><td>당일 등락</td><td>전일 대비 {change(r.get("당일등락률",np.nan))}</td></tr>'
                    f'<tr><td>당일 고가</td><td><b>{price(high)}</b><br>현재가 차이 {retreat}<br>고가 대비 {change(gap(high))}</td></tr>'
                    f'<tr><td>거래대금</td><td>당일 {escape(str(r.get("당일누적대금","미제공")))}<br><small>돌파봉 추정 {amount:,.1f}억 · {ratio:,.2f}배</small></td></tr>'
                    f'<tr class="time"><td colspan="2">시세 {escape(stamp)} KST · {escape(str(r.get("시세상태","미확인")))}<br>'
                    f'돌파 {escape(str(r["돌파봉(KST)"]))} · 데이터 {escape(str(r["기준봉(KST)"]))}<br>'
                    f'{escape(str(r.get("시세출처","네이버 시세 없음")))}</td></tr>')
    return ('<style>.scan-mobile{width:100%;max-width:480px;border-collapse:collapse;table-layout:fixed;font:14px/1.55 sans-serif;color:#edf2fa;background:#111827;color-scheme:dark;}'
            '.scan-mobile td{padding:6px 10px;text-align:left;vertical-align:top;overflow-wrap:anywhere;white-space:normal;border-bottom:1px solid #2b374b;}'
            '.scan-mobile .heading{background:#233047;}.scan-mobile strong{font-size:21px;}.scan-mobile small,.scan-mobile .time{font-size:12px;}'
            '.scan-mobile .muted,.scan-mobile small{color:#b4c2d6;}.scan-mobile .up{color:#ff929c;}.scan-mobile .down{color:#87baff;}'
            '.scan-mobile .badge{float:right;padding:2px 7px;border-radius:5px;font-size:12px;font-weight:bold;}'
            '.scan-mobile .wait{background:#574216;color:#ffe09a;}.scan-mobile .ready{background:#164c3b;color:#a0efd1;}'
            '.scan-mobile .time td{padding-bottom:14px;border-bottom:3px solid #465570;color:#b4c2d6;}</style>'
            '<table class="scan-mobile"><colgroup><col style="width:34%"><col style="width:66%"></colgroup><tbody>'+''.join(body)+'</tbody></table>')


def show_table(table):
    try:
        from IPython import get_ipython
        from IPython.display import display, HTML
        if get_ipython() is None:
            print(table.to_string(index=False))
        else:
            display(HTML(mobile_table_html(table)))
    except ImportError:
        print(table.to_string(index=False))


def show_diagnostics(lines):
    try:
        from IPython import get_ipython
        from IPython.display import display, HTML
        if get_ipython() is not None:
            display(HTML('<details><summary>조회 상세 펼치기</summary><div style="font-size:12px;overflow-wrap:anywhere">'+
                         '<br>'.join(escape(line) for line in lines)+'</div></details>'))
            return
    except ImportError:
        pass
    print('\n'.join(lines))


def run():
    started = time.time()
    now = pd.Timestamp.now(tz='Asia/Seoul')
    print(f'패턴 탐색 · {now:%Y-%m-%d %H:%M} KST · 최대 {SCAN_LIMIT}종목')
    stocks = universe()
    rows,errors = [],[]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for row,error in pool.map(lambda rec:score(rec,now),stocks):
            if row is not None:
                rows.append(row)
            else:
                errors.append(error)
    result = top_five(rows)
    watch = watch_table(rows)
    result,watch = attach_quotes([result,watch])
    if not result.empty:
        result = tag_repeats(result,now.strftime('%Y-%m-%d'),_PATTERN_SEEN)
    if SAVE_SNAPSHOTS:
        directory=Path(SNAPSHOT_DIR)
        try:
            directory.mkdir(parents=True,exist_ok=True)
            stamp=now.strftime('%Y%m%d_%H%M%S')
            pd.DataFrame(stocks).assign(scan_time=now.isoformat()).to_csv(directory/f'{stamp}_universe.csv',index=False,encoding='utf-8-sig')
            pd.DataFrame(rows).assign(scan_time=now.isoformat()).to_csv(directory/f'{stamp}_scores.csv',index=False,encoding='utf-8-sig')
            result.to_csv(directory/f'{stamp}_top5.csv',index=False,encoding='utf-8-sig')
            watch.to_csv(directory/f'{stamp}_watch.csv',index=False,encoding='utf-8-sig')
            saved_note=f'조회 기록: {directory} (런타임 초기화 전 내려받아 보관하세요)'
        except OSError as e:
            print('기록 저장 실패:',str(e)[:80])
    print(f'조회 {len(stocks)} · 채점 {len(rows)} · 제외 {len(errors)} · {time.time()-started:.0f}초')
    show_diagnostics(diagnostics(rows)+errors[:3]+([saved_note] if SAVE_SNAPSHOTS and 'saved_note' in locals() else []))
    print(f'매수 검토 {len(result)}개 · 관찰 {len(watch)}건')
    if not result.empty or not watch.empty:
        print('빨강 + / 파랑 − = 가격 차이. 매수·매도 지시 아님. 시세는 실행할 때만 갱신됩니다.')
        print('패턴 가격은 야후, 현재가는 네이버입니다. 일중 고가·대금은 별도 조회 값입니다.')
    if not result.empty:
        show_table(result)
    if not watch.empty:
        print('관찰 · 조건 미충족, 매수 신호 아님')
        show_table(watch)
    if result.empty:
        print('돌파 후 유지 조건을 충족한 종목이 없습니다.' if rows else '조회 가능한 데이터가 없습니다.')

        return result
    print('돌파 후 유지 조건 통과 종목만 최대 5개. 점수는 상승 확률이 아닙니다. 매도는 직접 판단하세요.')
    print('돌파봉 추정 거래대금 10억원 이상·동시간 중앙값 2배 이상, 다음 완료봉 가격 유지 확인.')
    print('거래대금=종가×거래량 추정치. 배수 기준=이전 최대 20일 같은 시간 중앙값(최소 5일).')
    print('기준봉은 완료봉의 시작 시각입니다. 야후 지연·15시 봉 누락 및 통합/KRX 차이를 MTS에서 확인하세요.')
    return result


if __name__=='__main__':
    PICKS = run()