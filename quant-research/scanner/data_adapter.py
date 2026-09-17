import time
import subprocess
import sys
import importlib
import requests
import pandas as pd
import numpy as np

def get_json(url: str, params: dict | None = None) -> dict:
    """HTTP helper with retry and timeout."""
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

def fetch_universe(scan_limit: int, mcap_min: float, mcap_max: float, min_price: float) -> list[dict]:
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
                if mcap_min <= cap <= mcap_max and price >= min_price:
                    rows.append({'code':code,'name':name,'market':market,
                                 'price':price,'market_cap':cap,'quote_time':x.get('localTradedAt'),
                                 'amount':number(x['accumulatedTradingValue'])})
            if number(stocks[-1]['marketValue'])*1e8 < mcap_min:
                break
    if not rows:
        raise RuntimeError('조회 가능한 중소형주가 없습니다. 잠시 후 다시 실행하세요.')
    return (pd.DataFrame(rows).dropna(subset=['amount']).sort_values('amount',ascending=False)
            .drop_duplicates('code').head(scan_limit).to_dict('records'))

def fetch_hourly_candles(code: str, market: str) -> dict:
    symbol = code+('.KQ' if market=='KOSDAQ' else '.KS')
    payload = get_json('https://query1.finance.yahoo.com/v8/finance/chart/'+symbol,
                       {'range':'60d','interval':'60m'})['chart']
    if not payload['result']:
        raise RuntimeError(str(payload.get('error')))
    return payload['result'][0]

def fetch_live_quote(code: str) -> dict:
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
