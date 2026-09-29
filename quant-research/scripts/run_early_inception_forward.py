"""Seal Mode 2 and Early Inception daily observations and resolve older signals.

Research-only virtual observations. This module has no broker or order access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "quant-research"))

from research.early_inception_engine import (  # noqa: E402
    compute_early_inception_rankings,
    simulate_trade_with_weakening_exit,
)
from scripts.pure_quant_portfolio_manager import (  # noqa: E402
    calculate_market_breadth, compute_factor_rankings,
)

KST = ZoneInfo("Asia/Seoul")
DEFAULT_DATA = ROOT / "quant-research/data/daily_3y"
DEFAULT_RUNS = ROOT / "quant-research/data/research/T-110/runs"
STRATEGIES = ("mode2", "early_inception")
TOP_N = 5
PANEL_START = "<!-- DUAL_FORWARD:START -->"
PANEL_END = "<!-- DUAL_FORWARD:END -->"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def load_data(directory: Path) -> tuple[dict[str, pd.DataFrame], dict[str, str], dict[str, int]]:
    universe, hashes = {}, {}
    rejected = {"missing_ohlcv": 0, "invalid_ohlcv": 0}
    for path in sorted(directory.glob("*.csv")):
        hashes[path.name] = _sha(path)
        frame = pd.read_csv(path, parse_dates=["Date"], index_col="Date").sort_index()
        if frame.index.has_duplicates:
            raise ValueError(f"DUPLICATE_DATES:{path.name}")
        required = frame[["Open", "High", "Low", "Close", "Volume"]]
        missing = required.isna().any(axis=1) | ~np.isfinite(required).all(axis=1)
        invalid = ((required[["Open", "High", "Low", "Close"]] <= 0).any(axis=1)
                   | (required["Volume"] < 0)
                   | (required["High"] < required[["Open", "Low", "Close"]].max(axis=1))
                   | (required["Low"] > required[["Open", "High", "Close"]].min(axis=1)))
        rejected["missing_ohlcv"] += int(missing.sum())
        rejected["invalid_ohlcv"] += int((invalid & ~missing).sum())
        frame = frame.loc[~(missing | invalid)].copy()
        if len(frame) >= 125:
            universe[path.stem] = frame
    if not universe:
        raise ValueError("NO_VALID_STOCKS")
    return universe, hashes, rejected


def inspect(data_dir: Path, manifest_path: Path, now: datetime | None = None) -> dict:
    """Validate an explicit source snapshot before computing any signal."""
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    if not manifest_path.is_file():
        raise ValueError("SOURCE_MANIFEST_MISSING")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fetched_at = datetime.fromisoformat(manifest["fetched_at"])
    if fetched_at.tzinfo is None or fetched_at > now + timedelta(minutes=5):
        raise ValueError("INVALID_FETCH_TIME")
    if manifest.get("failed"):
        raise ValueError("SOURCE_FETCH_PARTIAL")
    files = manifest.get("files", [])
    if not files or len(files) != manifest.get("succeeded"):
        raise ValueError("SOURCE_FILE_COUNT_MISMATCH")
    universe, hashes, rejected = load_data(data_dir)
    for item in files:
        filename = f'{item["code"]}.csv'
        if hashes.get(filename) != item.get("sha256"):
            raise ValueError(f"SOURCE_HASH_MISMATCH:{filename}")
    if len(hashes) != len(files):
        raise ValueError("UNDECLARED_SOURCE_FILES")
    primary_codes = manifest.get("primary_codes") or [item["code"] for item in files]
    if not primary_codes or not set(primary_codes).issubset({path.removesuffix('.csv') for path in hashes}):
        raise ValueError("PRIMARY_UNIVERSE_INCOMPLETE")
    if manifest.get("task") == "T-063" and len(primary_codes) < 140:
        raise ValueError("PRIMARY_UNIVERSE_TOO_SMALL")
    symbol_by_code = {item["code"]: item["symbol"] for item in files}
    name_by_code = {}
    if manifest.get("universe_file"):
        universe_path = ROOT / "quant-research/data/vcp_snapshots" / manifest["universe_file"]
        if not universe_path.is_file():
            raise ValueError("UNIVERSE_SNAPSHOT_MISSING")
        expected_hash = manifest.get("universe_sha256")
        if expected_hash and _sha(universe_path) != expected_hash:
            raise ValueError("UNIVERSE_HASH_MISMATCH")
        names = pd.read_csv(universe_path, dtype={"code": str}, encoding="utf-8-sig")
        name_by_code = dict(zip(names["code"], names["name"]))
    last_dates = {code: frame.index[-1].date() for code, frame in universe.items()}
    if not last_dates:
        raise ValueError("NO_VALID_STOCKS")
    as_of = max(last_dates.values())
    fresh_codes = [code for code in primary_codes if last_dates.get(code) == as_of]
    if len(fresh_codes) / len(primary_codes) < 0.90:
        raise ValueError("INSUFFICIENT_SAME_DAY_COVERAGE")
    today = now.astimezone(KST).date()
    if as_of > today or (today - as_of).days > 3:
        raise ValueError("PRICE_SNAPSHOT_STALE_OR_FUTURE")
    fetched_kst = fetched_at.astimezone(KST)
    if fetched_kst.date() == as_of and fetched_kst.hour < 16:
        raise ValueError("SAME_DAY_CLOSE_UNVERIFIED")
    manifest_last = max(pd.Timestamp(item["last"]).date() for item in files)
    if manifest_last != as_of:
        raise ValueError("MANIFEST_DATE_MISMATCH")
    return {"as_of": as_of.isoformat(), "fresh_codes": fresh_codes,
            "universe": universe, "hashes": hashes, "rejected": rejected,
            "fetched_at": fetched_at.isoformat(), "primary_codes": primary_codes,
            "symbol_by_code": symbol_by_code, "name_by_code": name_by_code}


def build_record(finding: dict, manifest_path: Path) -> dict:
    as_of = pd.Timestamp(finding["as_of"])
    universe = {code: finding["universe"][code] for code in finding["fresh_codes"]}
    breadth = float(calculate_market_breadth(universe).loc[as_of])
    if not np.isfinite(breadth):
        raise ValueError("MARKET_BREADTH_UNAVAILABLE")
    strategies = {}
    for key, ranker, score_name in (
        ("mode2", compute_factor_rankings, "composite_score"),
        ("early_inception", compute_early_inception_rankings, "inception_score"),
    ):
        ranks = ranker(universe, as_of) if breadth >= 40.0 else pd.DataFrame()
        candidates = [] if ranks.empty else ranks.head(TOP_N).to_dict(orient="records")
        candidates = json.loads(pd.DataFrame(candidates).to_json(orient="records")) if candidates else []
        for pick in candidates:
            symbol = finding["symbol_by_code"][pick["code"]]
            pick["market"] = "KOSDAQ" if symbol.endswith(".KQ") else "KOSPI"
            pick["name"] = finding["name_by_code"].get(pick["code"], pick["code"])
        strategies[key] = {"score_field": score_name, "candidates": candidates}
    source = {
        "manifest_sha256": _sha(manifest_path),
        "price_sha256": finding["hashes"],
        "engine_sha256": _sha(ROOT / "quant-research/research/early_inception_engine.py"),
        "runner_sha256": _sha(Path(__file__)),
        "mode2_sha256": _sha(ROOT / "quant-research/scripts/pure_quant_portfolio_manager.py"),
    }
    record = {"schema_version": 2, "strategies": strategies,
              "as_of": finding["as_of"], "fetched_at": finding["fetched_at"],
              "source": source, "universe_count": len(finding["primary_codes"]),
              "fresh_count": len(universe), "rejected_rows": finding["rejected"],
              "market_breadth_pct": breadth,
              "note": "완료 일봉 관찰. 다음 봉 시가 가상 진입만 후속 평가하며 실제 주문·매수 추천이 아님."}
    record["run_id"] = hashlib.sha256(_canonical(record)).hexdigest()[:20]
    return record


def capture(data_dir: Path, manifest_path: Path, run_dir: Path,
            now: datetime | None = None) -> dict:
    finding = inspect(data_dir, manifest_path, now=now)
    record = build_record(finding, manifest_path)
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / f'{record["as_of"]}.json'
    def same_decision(prior: dict) -> bool:
        return (prior.get("schema_version") == record["schema_version"]
                and prior.get("as_of") == record["as_of"]
                and prior.get("source", {}).get("price_sha256") == record["source"]["price_sha256"]
                and prior.get("source", {}).get("engine_sha256") == record["source"]["engine_sha256"]
                and prior.get("source", {}).get("runner_sha256") == record["source"]["runner_sha256"]
                and prior.get("source", {}).get("mode2_sha256") == record["source"]["mode2_sha256"]
                and prior.get("strategies") == record["strategies"])
    if target.exists():
        prior = json.loads(target.read_text(encoding="utf-8"))
        if not same_decision(prior):
            raise ValueError("SIGNAL_DATE_ALREADY_SEALED_WITH_DIFFERENT_INPUT")
        return {"path": str(target), "reused": True, "record": prior, "finding": finding}
    # Write a complete temporary file, then install it only if the day is free.
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=run_dir,
                                         prefix=".early-", delete=False) as stream:
            temp_name = stream.name
            json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_name, target)
    except FileExistsError:
        prior = json.loads(target.read_text(encoding="utf-8"))
        if not same_decision(prior):
            raise ValueError("SIGNAL_DATE_ALREADY_SEALED_WITH_DIFFERENT_INPUT")
        return {"path": str(target), "reused": True, "record": prior, "finding": finding}
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
    return {"path": str(target), "reused": False, "record": record, "finding": finding}


def _simulate_mode2(frame: pd.DataFrame, signal_day: pd.Timestamp) -> dict:
    """Independent 20-session Mode 2 virtual trade with a -15% airbag."""
    index = frame.index.get_loc(signal_day)
    future = frame.iloc[index + 1:index + 21]
    entry = float(future.iloc[0]["Open"])
    if entry <= 0 or float(future.iloc[0]["Volume"]) <= 0:
        return {"status": "NO_ENTRY_VOLUME"}
    stop = entry * 0.85
    exit_price = float(future.iloc[-1]["Close"])
    exit_date = future.index[-1]
    reason = "MAX_HORIZON"
    held = 20
    for i, (day, bar) in enumerate(future.iterrows(), 1):
        if float(bar["Open"]) <= stop:
            exit_price, exit_date, reason, held = float(bar["Open"]), day, "AIRBAG_STOP_LOSS", i
            break
        if float(bar["Low"]) <= stop:
            exit_price, exit_date, reason, held = stop, day, "AIRBAG_STOP_LOSS", i
            break
    cost = entry * 1.001 * 1.00015
    proceeds = exit_price * 0.999 * (1 - 0.00015 - 0.002)
    net_return = (proceeds / cost - 1) * 100
    return {"status": "COMPLETED", "entry_date": str(future.index[0].date()),
            "entry_price": entry, "exit_date": str(exit_date.date()),
            "exit_price": exit_price, "exit_reason": reason, "holding_days": held,
            "net_return_pct": net_return, "win": bool(net_return > 0)}


def resolve(run_dir: Path, universe: dict[str, pd.DataFrame], as_of: str) -> dict:
    """Evaluate sealed independent signals only after their full 20-session horizon."""
    outcomes = []
    pending = 0
    for path in sorted(run_dir.glob("????-??-??.json")):
        saved = json.loads(path.read_text(encoding="utf-8"))
        if set(saved.get("strategies", {})) != set(STRATEGIES):
            raise ValueError(f"WRONG_STRATEGIES:{path.name}")
        signal_day = pd.Timestamp(saved["as_of"])
        for strategy, block in saved["strategies"].items():
            for pick in block["candidates"]:
                code = pick["code"]
                frame = universe.get(code)
                if frame is None or signal_day not in frame.index:
                    pending += 1
                    continue
                signal_index = frame.index.get_loc(signal_day)
                future = frame.iloc[signal_index + 1:]
                if len(future) < 20 or future.index[19] > pd.Timestamp(as_of):
                    pending += 1
                    continue
                if strategy == "mode2":
                    result = _simulate_mode2(frame.loc[:pd.Timestamp(as_of)], signal_day)
                else:
                    result = simulate_trade_with_weakening_exit(frame.loc[:pd.Timestamp(as_of)], signal_day)
                # The engine's excursion fields can include the exit day's full high/low
                # after an opening-price exit. Only retain fields observable at the fill.
                visible = {key: result.get(key) for key in
                           ("status", "entry_date", "entry_price", "exit_date", "exit_price",
                            "exit_reason", "holding_days", "net_return_pct", "win")}
                outcomes.append({"signal_date": saved["as_of"], "strategy": strategy,
                                 "code": code, "run_id": saved["run_id"], **visible})
    return {"as_of": as_of, "resolved": outcomes, "pending_count": pending,
            "note": "독립 가상 신호 결과. 계좌 성과나 실제 체결이 아님."}


def update_readme_panel(path: Path, panel: str) -> None:
    """Replace only the dedicated dashboard panel; preserve other agents' text."""
    source = path.read_text(encoding="utf-8")
    if source.count(PANEL_START) != 1 or source.count(PANEL_END) != 1:
        raise ValueError("README_PANEL_MARKERS_MISSING_OR_DUPLICATED")
    start = source.index(PANEL_START) + len(PANEL_START)
    end = source.index(PANEL_END)
    if start > end:
        raise ValueError("README_PANEL_MARKERS_REVERSED")
    updated = source[:start] + "\n" + panel.strip() + "\n" + source[end:]
    path.write_text(updated, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--readme", type=Path, help="메인 README의 DUAL_FORWARD 패널 갱신")
    args = parser.parse_args()
    try:
        result = capture(args.data, args.manifest, args.runs)
    except ValueError as exc:
        if str(exc) in {"PRICE_SNAPSHOT_STALE_OR_FUTURE", "SAME_DAY_CLOSE_UNVERIFIED"}:
            print(json.dumps({"status": "SKIPPED", "reason": str(exc)}, ensure_ascii=False))
            return 0
        raise
    finding = result["finding"]
    outcomes = resolve(args.runs, finding["universe"], finding["as_of"])
    out_path = args.runs.parent / "outcomes.json"
    out_path.write_text(json.dumps(outcomes, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                        encoding="utf-8")
    record = result["record"]
    counts = {key: len(block["candidates"]) for key, block in record["strategies"].items()}
    breadth = record["market_breadth_pct"]
    lamp = "🟢 관찰 가능" if breadth >= 40.0 else "🔴 후보 선별 중단"
    fetched_kst = datetime.fromisoformat(record["fetched_at"]).astimezone(KST)
    fetched_label = fetched_kst.strftime("%Y-%m-%d %H:%M KST")
    lines = ["### 🌐 시장 상황", "",
             f'> {lamp} · 시장 폭(SMA60) **{breadth:.1f}%** · 기준 완료 일봉 `{record["as_of"]}`',
             ""]
    for strategy, block in record["strategies"].items():
        lines.extend([f'### {"모드 2" if strategy == "mode2" else "상승 초입"}', "",
                      f'> 갱신: `{fetched_label}` · 기준 완료 일봉: `{record["as_of"]}`', "",
                      "| 순위 | 종목 | 평가일 종가 | 상대점수 | 수치 근거 |",
                      "|---:|:---|---:|---:|:---|"])
        for rank, pick in enumerate(block["candidates"], 1):
            name = str(pick["name"]).replace("|", "/").replace("\n", " ")
            if strategy == "mode2":
                reason = (f'60→5거래일 {pick["mom60_5"]:+.1%} · 위험조정 '
                          f'{pick["risk_adj_mom"]:.2f} · CMF20 {pick["cmf20"]:+.2f}')
            else:
                volume = (f'5일 {pick["vol_ratio"]:.2f}x' if pick["vol_ratio"] >= 1.2
                          else f'당일 {pick["vol_spike_1d"]:.2f}x (5일 {pick["vol_ratio"]:.2f}x)')
                reason = (f'20일 변동폭 {pick["range_width_ratio"]:.1%} · '
                          f'{volume} · CMF20 {pick["cmf20"]:+.2f}')
            lines.append(f'| {rank} | {name} (`{pick["code"]}`) | {pick["close"]:,.0f}원 | '
                         f'{pick[block["score_field"]]:.3f} | {reason} |')
        if not block["candidates"]:
            lines.append("| - | 조건 충족 없음 | - | - | - |")
    lines.append("")
    panel = "\n".join(lines)
    (args.runs.parent / "latest.md").write_text(panel, encoding="utf-8")
    if args.readme:
        update_readme_panel(args.readme, panel)
    print(json.dumps({"as_of": finding["as_of"], "signal_count": counts,
                      "status": "REUSED" if result["reused"] else "CAPTURED",
                      "reused": result["reused"], "resolved": len(outcomes["resolved"]),
                      "pending": outcomes["pending_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
