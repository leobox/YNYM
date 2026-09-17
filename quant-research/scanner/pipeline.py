import pandas as pd
import numpy as np
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from scanner.data_adapter import fetch_universe, fetch_hourly_candles
from scanner.candle_cleaner import clean_bars, valid_ohlcv
from scanner.pattern import hourly_pattern
from scanner.breakout import confirmed_breakout
from scanner.indicators import _atr
from scanner.selector import select_top, build_watch
from scanner.renderer import attach_quotes, tag_repeats, diagnostics
from scanner.manifest import save_snapshot_run

@dataclass
class ScanResult:
    top5: pd.DataFrame
    watchlist: pd.DataFrame
    diagnostics: list[str]
    metadata: dict

def score_item(rec: dict, now: pd.Timestamp, config: dict) -> tuple[dict|None, str|None]:
    try:
        raw = fetch_hourly_candles(rec['code'], rec['market'])
        df = clean_bars(raw, now)
        if not valid_ohlcv(df):
            return None,'가격/거래량 정합성 오류'
        
        min_price = config.get('min_price', 2000)
        
        if len(df)<120 or df.Volume.iloc[-1]<=0 or df.Close.iloc[-1]<min_price:
            return None,'봉 부족/거래 정지/가격 조건'
        if now-df.index[-1]>pd.Timedelta(days=7):
            return None,'최근 7일 데이터 없음'
            
        s = hourly_pattern(df, volume_weight=config.get('volume_weight', 0.2), max_extension_atr=config.get('max_extension_atr')).iloc[-1]
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

def run_scan(config: dict | None = None) -> ScanResult:
    if config is None:
        config = {}
    scan_limit = config.get('scan_limit', 150)
    top_n = config.get('top_n', 5)
    mcap_min = config.get('mcap_min', 100_000_000_000)
    mcap_max = config.get('mcap_max', 5_000_000_000_000)
    min_price = config.get('min_price', 2000)
    max_workers = config.get('workers', 6)
    snapshot_dir = config.get('snapshot_dir', 'pattern_snapshots')
    save_snapshots = config.get('save_snapshots', True)
    
    now = pd.Timestamp.now(tz='Asia/Seoul')
    stocks = fetch_universe(scan_limit, mcap_min, mcap_max, min_price)
    
    rows = []
    errors = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for row, error in pool.map(lambda rec: score_item(rec, now, config), stocks):
            if row is not None:
                rows.append(row)
            else:
                errors.append(error)
                
    result = select_top(rows, top_n=top_n)
    watch = build_watch(rows)
    
    # attach quotes and tags for the final tables if not empty
    result, watch = attach_quotes([result, watch])
    if not result.empty:
        result = tag_repeats(result, now.strftime('%Y-%m-%d'), set())
        
    if save_snapshots:
        directory=Path(snapshot_dir)
        try:
            stamp=now.strftime('%Y%m%d_%H%M%S')
            snapshot_metadata = {
                'total_scanned': len(stocks), 'scored': len(rows),
                'errors': len(errors), 'timestamp': now.isoformat(),
            }
            save_snapshot_run(stamp, directory, {
                'universe': pd.DataFrame(stocks).assign(scan_time=now.isoformat()),
                'scores': pd.DataFrame(rows).assign(scan_time=now.isoformat()),
                'top5': result, 'watch': watch,
            }, config, snapshot_metadata)
        except OSError as exc:
            errors.append(f'기록 저장 실패: {str(exc)[:100]}')
            
    metadata = {
        'total_scanned': len(stocks),
        'scored': len(rows),
        'errors': len(errors),
        'timestamp': now.isoformat()
    }
    
    diag_msgs = diagnostics(rows) + errors[:3]
    
    return ScanResult(
        top5=result,
        watchlist=watch,
        diagnostics=diag_msgs,
        metadata=metadata
    )
