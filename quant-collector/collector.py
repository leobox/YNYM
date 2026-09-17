"""
Quant Data Collector Script
---------------------------
GitHub Actions 또는 로컬에서 1시간 주기 및 모바일 수동(workflow_dispatch)으로 실행되어
시세/완료봉 데이터를 관측하고 멱등적으로 기록하는 스크립트입니다.

[안전 불변식]
- 실제 거래, 주문, 매수, 매도, 계좌 연동 API 작성 및 호출 절대 금지.
- 순수 공개 시세 데이터(Public Market Data)만 수집하여 파일로 저장합니다.
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
import requests

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
KST = timezone(timedelta(hours=9))

# 수집 대상 벤치마크 및 주요 관측 종목 (KOSPI/KOSDAQ 대표 및 시장 지표)
WATCHLIST = [
    {"code": "069500", "name": "KODEX 200", "symbol": "069500.KS"},
    {"code": "229200", "name": "KODEX 코스닥150", "symbol": "229200.KQ"},
    {"code": "005930", "name": "삼성전자", "symbol": "005930.KS"},
    {"code": "000660", "name": "SK하이닉스", "symbol": "000660.KS"},
    {"code": "035420", "name": "NAVER", "symbol": "035420.KS"},
    {"code": "005380", "name": "현대차", "symbol": "005380.KS"},
]


def get_current_kst() -> datetime:
    return datetime.now(KST)


def safe_request_json(url: str, params: Optional[Dict[str, Any]] = None, max_retries: int = 3, timeout: int = 10) -> Optional[Dict[str, Any]]:
    """타임아웃(10초) 및 지수 백오프(Exponential Backoff)를 적용한 안전한 HTTP GET 요청"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    backoff = 1.0

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == max_retries:
                print(f"[ERROR] 요청 최종 실패: {url} | 사유: {str(e)[:100]}", file=sys.stderr)
                return None
            time.sleep(backoff)
            backoff *= 2.0
    return None


def fetch_hourly_candles(symbol: str) -> Optional[Dict[str, Any]]:
    """Yahoo Finance 공개 엔드포인트에서 최근 60분봉 시세 수집 (비거래 조회 전용)"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "5d", "interval": "60m"}
    data = safe_request_json(url, params=params)
    if not data or "chart" not in data or not data["chart"].get("result"):
        return None

    result = data["chart"]["result"][0]
    meta = result.get("meta", {})
    timestamps = result.get("timestamp", [])
    quotes = result.get("indicators", {}).get("quote", [{}])[0]

    if not timestamps or not quotes:
        return None

    # 최신 5개 완료봉 추출
    candles = []
    for i in range(len(timestamps)):
        t_utc = datetime.fromtimestamp(timestamps[i], timezone.utc)
        t_kst = t_utc.astimezone(KST)
        o = quotes.get("open", [None])[i]
        h = quotes.get("high", [None])[i]
        l = quotes.get("low", [None])[i]
        c = quotes.get("close", [None])[i]
        v = quotes.get("volume", [None])[i]

        if None in (o, h, l, c, v):
            continue

        candles.append({
            "time_kst": t_kst.strftime("%Y-%m-%d %H:%M"),
            "open": round(float(o), 2),
            "high": round(float(h), 2),
            "low": round(float(l), 2),
            "close": round(float(c), 2),
            "volume": int(v)
        })

    return {
        "symbol": symbol,
        "currency": meta.get("currency", "KRW"),
        "regular_market_price": meta.get("regularMarketPrice"),
        "recent_candles": candles[-5:]  # 가장 최근 5개 봉만 보관
    }


def fetch_naver_quote(code: str) -> Optional[Dict[str, Any]]:
    """네이버 증권 모바일 공개 API에서 실시간 보조 시세 스냅샷 조회"""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    data = safe_request_json(url, timeout=10)
    if not data:
        return None

    def parse_num(val):
        if val is None:
            return None
        try:
            return float(str(val).replace(",", ""))
        except ValueError:
            return None

    return {
        "code": code,
        "stock_name": data.get("stockName"),
        "current_price": parse_num(data.get("closePrice")),
        "change_ratio": parse_num(data.get("fluctuationsRatio")),
        "market_status": data.get("marketStatus"),
        "traded_at": data.get("localTradedAt")
    }


def save_record_idempotent(record: Dict[str, Any], now_kst: datetime):
    """
    멱등적(Idempotent) 데이터 저장:
    - 날짜별 폴더 구조: data/{YYYYMMDD}/snapshot_{HH}.json (동일 시간대 재실행 시 덮어써서 중복 방지)
    - 일별 집계 로그: data/daily_{YYYYMMDD}.jsonl (동일 시각 레코드 중복 방지)
    """
    date_str = now_kst.strftime("%Y%m%d")
    hour_str = now_kst.strftime("%H")
    time_str = now_kst.strftime("%Y-%m-%d %H:%M:%S")

    day_dir = os.path.join(DATA_DIR, date_str)
    os.makedirs(day_dir, exist_ok=True)

    # 1. 시각별 원자적 스냅샷 파일 (동일 시간에 n번 돌아도 항상 1개의 파일로 멱등 보장)
    snapshot_path = os.path.join(day_dir, f"snapshot_{hour_str}00.json")
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    # 2. 일별 로그 파일 (중복 방지 확인 후 추가)
    log_path = os.path.join(DATA_DIR, f"daily_{date_str}.jsonl")
    existing_timestamps = set()
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    existing_timestamps.add(entry.get("timestamp_kst"))
                except Exception:
                    pass

    # 이미 동일 분 초에 기록된 게 없으면 추가
    if time_str not in existing_timestamps:
        summary_line = {
            "timestamp_kst": time_str,
            "status": record["status"],
            "collected_count": len(record.get("items", [])),
            "errors": record.get("errors", [])
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary_line, ensure_ascii=False) + "\n")

    print(f"[{time_str} KST] 멱등 데이터 저장 완료: {snapshot_path}")


def collect():
    now_kst = get_current_kst()
    timestamp_str = now_kst.strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== [Quant Collector] {timestamp_str} KST 데이터 수집 시작 ===")

    items = []
    errors = []

    for target in WATCHLIST:
        code = target["code"]
        name = target["name"]
        symbol = target["symbol"]

        print(f"-> 수집 중: {name} ({code})...")
        naver_info = fetch_naver_quote(code)
        hourly_info = fetch_hourly_candles(symbol)

        if not naver_info and not hourly_info:
            errors.append({"code": code, "error": "모든 시세 수집 실패"})
            continue

        item = {
            "code": code,
            "name": name,
            "naver_quote": naver_info,
            "hourly_candles": hourly_info
        }
        items.append(item)

    record = {
        "timestamp_kst": timestamp_str,
        "status": "success" if items else "failed",
        "total_targets": len(WATCHLIST),
        "collected_count": len(items),
        "items": items,
        "errors": errors
    }

    save_record_idempotent(record, now_kst)
    print(f"=== [Quant Collector] 수집 종료: 성공 {len(items)} / 오류 {len(errors)} ===\n")


if __name__ == "__main__":
    collect()
