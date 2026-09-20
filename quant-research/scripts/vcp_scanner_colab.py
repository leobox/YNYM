"""Colab: paste this whole file into one cell and run. Table only, no orders.
VCP (Volatility Contraction Pattern) 슈퍼 신고가 스캐너 모바일 Colab 운영 버전.
"""
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
MAX_EXTENSION_ATR = 3.8

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
    """네이버 증권 시가총액 순위에서 중소형주(1000억~5조원) 유니버스 추출."""
    try:
        fdr = importlib.import_module('FinanceDataReader')
    except ImportError:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'finance-datareader'])
        fdr = importlib.import_module('FinanceDataReader')
    listing = fdr.StockListing('KRX')
    valid = set(listing.loc[listing.Market.isin(['KOSPI', 'KOSDAQ', 'KOSDAQ GLOBAL']), 'Code'].astype(str))
    if not valid:
        raise RuntimeError('상장 종목 목록이 비어 있습니다.')
    rows = []
    number = lambda x: pd.to_numeric(str(x).replace(',', ''), errors='coerce')
    for market in ('KOSPI', 'KOSDAQ'):
        for page in range(1, 31):
            stocks = get_json(f'https://m.stock.naver.com/api/stocks/marketValue/{market}',
                              {'page': page, 'pageSize': 100})['stocks']
            if not stocks:
                break
            for x in stocks:
                code, name = x['itemCode'], x['stockName']
                cap, price = number(x['marketValue']) * 1e8, number(x['closePrice'])
                if code not in valid or not code.endswith('0') or '스팩' in name or '리츠' in name:
                    continue
                if MCAP_MIN <= cap <= MCAP_MAX and price >= MIN_PRICE:
                    rows.append({
                        'code': code, 'name': name, 'market': market,
                        'price': price, 'market_cap': cap, 'quote_time': x.get('localTradedAt'),
                        'amount': number(x['accumulatedTradingValue'])
                    })
            if number(stocks[-1]['marketValue']) * 1e8 < MCAP_MIN:
                break
    if not rows:
        raise RuntimeError('조회 가능한 종목이 없습니다.')
    return (pd.DataFrame(rows).dropna(subset=['amount']).sort_values('amount', ascending=False)
            .drop_duplicates('code').head(SCAN_LIMIT).to_dict('records'))

def completed_bars(item, now):
    """야후 파이낸스 60분봉 중 현재 시각(now) 기준 완료된 봉만 엄격 추출."""
    df = pd.DataFrame(item['indicators']['quote'][0],
                      index=pd.to_datetime(item['timestamp'], unit='s', utc=True).tz_convert('Asia/Seoul'))
    df = df.rename(columns=str.title)[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
    df = df[~df.index.duplicated()].sort_index()
    df = df[(df.index.minute == 0) & (df.index.second == 0) & (df.index.hour >= 9) & (df.index.hour <= 15)]
    df = df[~((df.index.hour == 15) & (df.Volume == 0) & (df.High == df.Low))]
    ends = df.index + pd.to_timedelta(np.where(df.index.hour == 15, 30, 60), unit='m')
    return df[ends <= now]

def fetch(rec, now):
    symbol = rec['code'] + ('.KQ' if rec['market'] == 'KOSDAQ' else '.KS')
    payload = get_json('https://query1.finance.yahoo.com/v8/finance/chart/' + symbol,
                       {'range': '60d', 'interval': '60m'})['chart']
    if not payload['result']:
        raise RuntimeError(str(payload.get('error')))
    return completed_bars(payload['result'][0], now)

def _atr(df, period=14):
    previous = df.Close.shift(1)
    tr = pd.concat([df.High - df.Low, (df.High - previous).abs(), (df.Low - previous).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean().replace(0, np.nan)

def detect_vcp(df):
    """완료봉 기준 VCP(변동성 축소 패턴) 슈퍼 신고가 판정."""
    c, h, l, o, v = df['Close'], df['High'], df['Low'], df['Open'], df['Volume']
    
    ma5 = c.rolling(5).mean()
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60, min_periods=20).mean()
    
    bull_aligned = (ma5 > ma20) & (ma20 > ma60) & (c > ma20)
    
    # 피봇 기준선 (직전 20봉 고가)
    pivot_level = h.shift(1).rolling(20).max()
    is_breakout = (c > pivot_level) & (c > o)
    
    # 변동성 수축 파동
    range_20 = (h.shift(1).rolling(20).max() - l.shift(1).rolling(20).min()) / c.shift(1).replace(0, np.nan)
    range_10 = (h.shift(1).rolling(10).max() - l.shift(1).rolling(10).min()) / c.shift(1).replace(0, np.nan)
    range_5 = (h.shift(1).rolling(5).max() - l.shift(1).rolling(5).min()) / c.shift(1).replace(0, np.nan)
    
    vcp_ratio = range_5 / range_20.replace(0, np.nan)
    is_3t = (range_5 < range_10) & (range_10 < range_20)
    is_2t = (range_5 < range_20)
    vcp_stage = np.where(is_3t, '3T (3단계수축)', np.where(is_2t, '2T (2단계수축)', '수축미달'))
    
    v_ma20 = v.shift(1).rolling(20).mean().replace(0, np.nan)
    v_ma5 = v.shift(1).rolling(5).mean().replace(0, np.nan)
    vol_dryup = v_ma5 / v_ma20
    vol_spike = v / v_ma20
    
    atr = _atr(df)
    extension_atr = (c - ma20) / atr.replace(0, np.nan)
    
    eligible = (
        is_breakout & bull_aligned &
        (vcp_ratio <= 0.65) &
        (vol_spike >= 1.5) &
        (extension_atr <= MAX_EXTENSION_ATR) &
        (c >= MIN_PRICE) & (v > 0)
    )
    
    score_vcp = ((1.0 - vcp_ratio.clip(0, 1.0)) * 40.0).fillna(0)
    score_vol = (vol_spike.clip(1.0, 5.0) / 5.0 * 35.0).fillna(0)
    score_ext = ((1.0 - (extension_atr.clip(0, 4.0) / 4.0)) * 25.0).fillna(0)
    
    score = score_vcp + score_vol + score_ext
    score = score.where(np.isfinite(score), 0.0)
    
    return pd.DataFrame({
        'eligible': eligible.fillna(False),
        'score': score.round(1),
        'vcp_ratio': vcp_ratio.round(3),
        'vcp_stage': vcp_stage,
        'vol_spike': vol_spike.round(2),
        'vol_dryup': vol_dryup.round(2),
        'pivot_level': pivot_level.round(0),
        'extension_atr': extension_atr.round(2),
        'bull_aligned': bull_aligned.fillna(False)
    }, index=df.index)

def render_html(top5, watchlist, now_str):
    """모바일 최적화 다크 테마 HTML 렌더러."""
    css = """
    <style>
      .vcp-wrap { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #121418; color: #E1E4EA; padding: 14px; border-radius: 12px; max-width: 600px; margin: auto; }
      .vcp-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #282C34; padding-bottom: 10px; margin-bottom: 12px; }
      .vcp-title { font-size: 18px; font-weight: 700; color: #4E95FF; margin: 0; }
      .vcp-time { font-size: 12px; color: #8B949E; }
      .vcp-card { background: #1C2026; border-radius: 8px; padding: 12px; margin-bottom: 10px; border-left: 4px solid #30363D; }
      .vcp-card.match { border-left-color: #00E676; }
      .vcp-card.watch { border-left-color: #FFB300; }
      .vcp-row1 { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
      .vcp-name { font-size: 16px; font-weight: 700; color: #FFFFFF; }
      .vcp-code { font-size: 12px; color: #8B949E; margin-left: 6px; }
      .vcp-score { font-size: 14px; font-weight: 700; color: #00E676; }
      .vcp-row2 { display: flex; justify-content: space-between; font-size: 13px; color: #C9D1D9; }
      .badge { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600; }
      .badge-green { background: rgba(0, 230, 118, 0.15); color: #00E676; }
      .badge-amber { background: rgba(255, 179, 0, 0.15); color: #FFB300; }
      .vcp-meta { font-size: 12px; color: #8B949E; margin-top: 6px; line-height: 1.4; }
      .no-data { text-align: center; color: #8B949E; padding: 20px; font-size: 14px; }
    </style>
    """
    html = [f"<div class='vcp-wrap'>{css}"]
    html.append(f"<div class='vcp-header'><h3 class='vcp-title'>🎯 VCP 슈퍼 신고가 스캐너</h3><span class='vcp-time'>{escape(now_str)} (KST)</span></div>")
    
    html.append("<div style='margin-bottom:8px; font-size:13px; font-weight:600; color:#00E676;'>🏆 최종 후보 (조건 100% 충족)</div>")
    if top5:
        for r in top5:
            html.append(f"""
            <div class='vcp-card match'>
              <div class='vcp-row1'>
                <div><span class='vcp-name'>{escape(r['name'])}</span><span class='vcp-code'>{escape(r['code'])}</span></div>
                <div class='vcp-score'>점수 {r['score']}점</div>
              </div>
              <div class='vcp-row2'>
                <div><span class='badge badge-green'>{escape(r['vcp_stage'])}</span> 수축비 {r['vcp_ratio']}</div>
                <div>거래량 폭발 <b>{r['vol_spike']}배</b></div>
              </div>
              <div class='vcp-meta'>
                종가: {r['price']:,}원 | 피봇돌파선: {r['pivot_level']:,}원 | 이격: {r['extension_atr']} ATR
              </div>
            </div>
            """)
    else:
        html.append("<div class='no-data'>현재 VCP 돌파 조건을 100% 충족한 최종 후보가 없습니다. (무리한 추격 금지)</div>")
        
    html.append("<div style='margin-top:14px; margin-bottom:8px; font-size:13px; font-weight:600; color:#FFB300;'>👀 관찰 후보 (수축 완료, 돌파 임박)</div>")
    if watchlist:
        for r in watchlist:
            html.append(f"""
            <div class='vcp-card watch'>
              <div class='vcp-row1'>
                <div><span class='vcp-name'>{escape(r['name'])}</span><span class='vcp-code'>{escape(r['code'])}</span></div>
                <div style='font-size:13px; color:#FFB300;'>피봇 근접</div>
              </div>
              <div class='vcp-row2'>
                <div><span class='badge badge-amber'>{escape(r['vcp_stage'])}</span> 수축비 {r['vcp_ratio']}</div>
                <div>거래량 마름 {r['vol_dryup']}배</div>
              </div>
              <div class='vcp-meta'>
                현재가: {r['price']:,}원 | 피봇저항: {r['pivot_level']:,}원 (돌파 대기)
              </div>
            </div>
            """)
    else:
        html.append("<div class='no-data'>관찰 후보 없음</div>")
        
    html.append("<div style='margin-top:10px; font-size:11px; color:#6E7681; text-align:center;'>※ 본 스캐너는 완료봉 기준 연구용 지표이며, 주문/매수 API를 포함하지 않습니다.</div>")
    html.append("</div>")
    return "".join(html)

def run():
    # [T-052] 이 Colab 사본은 60분봉에 일봉용 창을 적용하는 옛 로직이라 2026-09-20 감사에서 미채택(랜덤 대비 12백분위).
    # 일봉 재설계본은 GitHub Actions 신호판(README, quant-research/data/vcp_snapshots/latest_vcp.md)에만 반영됐다.
    # Colab 단일 셀 동기화는 후속 작업이며, 그때까지 낡은 신호를 내지 않도록 실행을 막는다.
    print("이 Colab 스캐너는 T-052에서 운영 중단되었습니다. 루트 README의 '추세 돌파 신호판'을 확인하세요.")
    return
    now = pd.Timestamp.now(tz='Asia/Seoul')
    now_str = now.strftime('%Y-%m-%d %H:%M')
    print(f"[{now_str}] VCP 슈퍼 신고가 스캐닝 시작 (유니버스 {SCAN_LIMIT}개)...")
    
    uni = universe()
    scored = []
    
    def process(rec):
        try:
            df = fetch(rec, now)
            if len(df) < 60 or df.Volume.iloc[-1] <= 0:
                return None
            res = detect_vcp(df).iloc[-1]
            c = df.Close.iloc[-1]
            h = df.High.iloc[-1]
            return {
                'code': rec['code'], 'name': rec['name'], 'price': int(c),
                'eligible': bool(res['eligible']), 'score': float(res['score']),
                'vcp_stage': str(res['vcp_stage']), 'vcp_ratio': float(res['vcp_ratio']),
                'vol_spike': float(res['vol_spike']), 'vol_dryup': float(res['vol_dryup']),
                'pivot_level': int(res['pivot_level']), 'extension_atr': float(res['extension_atr']),
                'near_pivot': bool(c >= res['pivot_level'] * 0.96 and c < res['pivot_level'] and res['vcp_ratio'] <= 0.65)
            }
        except Exception:
            return None
            
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = pool.map(process, uni)
        for r in results:
            if r is not None:
                scored.append(r)
                
    # 최종 후보: eligible == True, score 내림차순 상위 5개
    matches = [r for r in scored if r['eligible']]
    top5 = sorted(matches, key=lambda x: (-x['score'], x['code']))[:TOP_N]
    
    # 관찰 후보: eligible은 아니지만 수축 완료 후 피봇 4% 턱밑에 대기 중인 종목 상위 5개
    watches = [r for r in scored if not r['eligible'] and r['near_pivot']]
    watchlist = sorted(watches, key=lambda x: (x['vcp_ratio'], x['code']))[:TOP_N]
    
    html_out = render_html(top5, watchlist, now_str)
    
    try:
        from IPython.display import HTML, display
        display(HTML(html_out))
    except ImportError:
        print("\n=== [VCP 슈퍼 신고가 스캐너 결과] ===")
        print(f"최종 후보: {len(top5)}개, 관찰 종목: {len(watchlist)}개")
        for i, r in enumerate(top5, 1):
            print(f"[{i}] {r['name']}({r['code']}): {r['price']}원, {r['vcp_stage']}, 점수 {r['score']}")
            
if __name__ == '__main__':
    run()
