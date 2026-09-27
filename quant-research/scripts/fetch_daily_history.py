"""엣지 탐색기(T-063)용 야후 일봉 다년치 수신.

- 유니버스: data/vcp_snapshots/universe_*.csv 중 가장 최근 파일(실행 시점 상위 150 — 생존편향 있음)
- 종목당 1회 수신(range=3y, interval=1d), 종목 간 지연·유한 timeout·제한된 재시도
- 종목별 CSV(Open/High/Low/Close/AdjClose/Volume)는 data/daily_3y/ 에 저장(gitignore, 용량)
- 실행 manifest(수신 시각·행 수·해시·실패 종목)는 data/research/T-063/ 에 저장(추적)
- 실패를 0이나 빈 데이터로 위장하지 않는다. 주문/매수 API 없음.
"""
import sys
import json
import time
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pandas as pd

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "quant-research"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_vcp_scanner import get_json, get_universe  # noqa: E402  (유한 timeout + 지수 backoff 재시도)

SNAP_DIR = ROOT / "quant-research" / "data" / "vcp_snapshots"
DATA_DIR = ROOT / "quant-research" / "data"
OUT_DIR = DATA_DIR / "research" / "T-063"
CALL_DELAY_SEC = 0.4
# 기간에 못 미치는 신규 상장 종목은 제외하지 않고 행 수와 함께 기록만 한다(탐색기가 종목별 이력을 처리)
MIN_ROWS = {"3y": 700, "10y": 2000}


def latest_universe() -> tuple[Path, pd.DataFrame]:
    files = sorted(SNAP_DIR.glob("universe_*.csv"))
    if not files:
        raise RuntimeError("universe_*.csv 없음 — run_vcp_scanner.py를 먼저 실행")
    path = files[-1]
    df = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
    return path, df


def to_frame(item: dict) -> pd.DataFrame:
    quote = item["indicators"]["quote"][0]
    adj = item["indicators"].get("adjclose", [{}])[0].get("adjclose")
    if adj is None:
        raise ValueError("adjclose 없음")
    index = (pd.to_datetime(item["timestamp"], unit="s", utc=True)
             .tz_convert("Asia/Seoul").tz_localize(None).normalize())
    df = pd.DataFrame({"Open": quote["open"], "High": quote["high"], "Low": quote["low"],
                       "Close": quote["close"], "AdjClose": adj, "Volume": quote["volume"]}, index=index)
    df.index.name = "Date"
    return df


def include_unresolved(universe: pd.DataFrame, runs_dir: Path | None) -> tuple[pd.DataFrame, list[str]]:
    """Retain prices for earlier candidates that left today's ranking universe."""
    if runs_dir is None or not runs_dir.is_dir():
        return universe, []
    primary = set(universe["code"].astype(str))
    outcome_path = runs_dir.parent / "outcomes.json"
    resolved = set()
    if outcome_path.is_file():
        outcome_data = json.loads(outcome_path.read_text(encoding="utf-8"))
        resolved = {(row["signal_date"], row["strategy"], row["code"])
                    for row in outcome_data.get("resolved", [])}
    carryover = {}
    for path in sorted(runs_dir.glob("????-??-??.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        for strategy, block in record.get("strategies", {}).items():
            for pick in block.get("candidates", []):
                key = (record["as_of"], strategy, pick["code"])
                if key not in resolved and pick["code"] not in primary:
                    carryover[pick["code"]] = pick["market"]
    codes = sorted(carryover)
    if codes:
        extra = pd.DataFrame({"code": codes, "market": [carryover[code] for code in codes]})
        universe = pd.concat([universe, extra], ignore_index=True)
    return universe.drop_duplicates("code"), codes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", default="3y", choices=sorted(MIN_ROWS), help="수신 기간(원본은 data/daily_<range>/)")
    ap.add_argument("--universe", type=Path, help="재현 가능한 고정 종목군 CSV (미지정 시 최신 스냅샷)")
    ap.add_argument("--refresh-universe", action="store_true", help="현재 거래대금 상위 감시 종목군을 다시 조회")
    ap.add_argument("--carryover-runs", type=Path, help="이전 전진 신호의 종목을 후속 평가까지 추가 수신")
    args = ap.parse_args()
    if args.universe and args.refresh_universe:
        ap.error("--universe and --refresh-universe cannot be combined")
    period = args.range
    RAW_DIR = DATA_DIR / f"daily_{period}"
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(KST)
    if args.refresh_universe:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = fetched_at.strftime("%Y%m%d_%H%M%S")
        universe_path = SNAP_DIR / f"universe_{stamp}.csv"
        universe = pd.DataFrame(get_universe())
        universe.to_csv(universe_path, index=False, encoding="utf-8-sig")
    elif args.universe:
        universe_path = args.universe.resolve()
        universe = pd.read_csv(universe_path, dtype={"code": str}, encoding="utf-8-sig")
    else:
        universe_path, universe = latest_universe()
    primary_codes = universe["code"].astype(str).tolist()
    universe, carryover_codes = include_unresolved(universe, args.carryover_runs)
    files, failed = [], []

    for i, row in enumerate(universe.itertuples(index=False), 1):
        code, market = row.code, row.market
        symbol = code + (".KQ" if market == "KOSDAQ" else ".KS")
        try:
            payload = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                               {"range": period, "interval": "1d"}, timeout=10)["chart"]
            if not payload.get("result"):
                raise ValueError("result 없음")
            df = to_frame(payload["result"][0])
            path = RAW_DIR / f"{code}.csv"
            df.to_csv(path)
            files.append({"code": code, "symbol": symbol, "rows": int(len(df)),
                          "first": str(df.index.min().date()), "last": str(df.index.max().date()),
                          "null_close": int(df["Close"].isna().sum()),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        except Exception as e:
            failed.append({"code": code, "symbol": symbol, "error": str(e)[:120]})
        if i % 25 == 0:
            print(f"{i}/{len(universe)} 수신 (성공 {len(files)}, 실패 {len(failed)})", flush=True)
        time.sleep(CALL_DELAY_SEC)

    manifest = {
        "task": "T-063",
        "fetched_at": fetched_at.isoformat(),
        "source": f"Yahoo Finance chart API (비공식, range={period} interval=1d)",
        "universe_file": universe_path.name,
        "universe_sha256": hashlib.sha256(universe_path.read_bytes()).hexdigest(),
        "primary_codes": primary_codes,
        "carryover_codes": carryover_codes,
        "universe_note": "실행 시점 상위 150 — 생존편향, 상폐 종목 누락",
        "price_note": "AdjClose 포함. Close는 배당 미보정, OHLC 보정은 탐색기에서 AdjClose/Close 비율로 수행",
        "requested": int(len(universe)),
        "succeeded": len(files),
        "failed": failed,
        "short_history": [f["code"] for f in files if f["rows"] < MIN_ROWS[period]],
        "files": files,
    }
    out = OUT_DIR / f"fetch_manifest_{period}_{fetched_at.strftime('%Y%m%d_%H%M')}.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"완료: 요청 {len(universe)}, 성공 {len(files)}, 실패 {len(failed)}, 이력 짧은 종목 {len(manifest['short_history'])}")
    print(f"manifest: {out}")
    return 0 if files else 1


if __name__ == "__main__":
    sys.exit(main())
