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
    compute_early_inception_3d_rankings,
    simulate_trade_with_weakening_exit,
)
from scripts.pure_quant_portfolio_manager import (  # noqa: E402
    calculate_market_breadth, compute_factor_rankings,
)

KST = ZoneInfo("Asia/Seoul")
DEFAULT_DATA = ROOT / "quant-research/data/daily_3y"
DEFAULT_RUNS = ROOT / "quant-research/data/research/T-110/runs"
STRATEGIES = ("early_inception_3d", "early_inception")
TOP_N = 5
PANEL_START = "<!-- DUAL_FORWARD:START -->"
PANEL_END = "<!-- DUAL_FORWARD:END -->"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def load_effective_record(run_dir: Path, day: str) -> dict | None:
    """Load non-destructive correction if present, otherwise the sealed record."""
    corr_path = run_dir / "corrections" / f"{day}.json"
    if corr_path.is_file():
        return json.loads(corr_path.read_text(encoding="utf-8"))
    sealed_path = run_dir / f"{day}.json"
    if sealed_path.is_file():
        return json.loads(sealed_path.read_text(encoding="utf-8"))
    return None


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
    eligible_primary = [code for code in primary_codes if code in universe]
    if not eligible_primary:
        raise ValueError("NO_VALID_STOCKS")
    if manifest.get("task") == "T-063" and len(eligible_primary) < 100:
        raise ValueError("PRIMARY_UNIVERSE_TOO_SMALL")
    symbol_by_code = {item["code"]: item["symbol"] for item in files}
    name_by_code = {}
    if manifest.get("universe_file"):
        universe_path = ROOT / "quant-research/data/vcp_snapshots" / manifest["universe_file"]
        if not universe_path.is_file():
            raise ValueError("UNIVERSE_SNAPSHOT_MISSING")
        expected_hash = manifest.get("universe_sha256")
        raw_univ = universe_path.read_bytes()
        univ_shas = {hashlib.sha256(raw_univ).hexdigest(),
                     hashlib.sha256(raw_univ.replace(b"\r\n", b"\n")).hexdigest()}
        if expected_hash and expected_hash not in univ_shas:
            raise ValueError("UNIVERSE_HASH_MISMATCH")
        names = pd.read_csv(universe_path, dtype={"code": str}, encoding="utf-8-sig")
        name_by_code = dict(zip(names["code"], names["name"]))
    last_dates = {code: frame.index[-1].date() for code, frame in universe.items()}
    if not last_dates:
        raise ValueError("NO_VALID_STOCKS")
    primary_dates = [last_dates[code] for code in eligible_primary if code in last_dates]
    as_of = max(set(primary_dates), key=lambda d: (primary_dates.count(d), d))
    fresh_codes = [code for code in eligible_primary if last_dates.get(code) == as_of]
    if len(fresh_codes) / len(eligible_primary) < 0.90:
        raise ValueError("INSUFFICIENT_SAME_DAY_COVERAGE")
    today = now.astimezone(KST).date()
    if as_of > today or (today - as_of).days > 3:
        raise ValueError("PRICE_SNAPSHOT_STALE_OR_FUTURE")
    fetched_kst = fetched_at.astimezone(KST)
    if fetched_kst.date() == as_of and fetched_kst.hour < 16:
        raise ValueError("SAME_DAY_CLOSE_UNVERIFIED")
    manifest_dates = [pd.Timestamp(item["last"]).date() for item in files if item["code"] in eligible_primary]
    manifest_last = (max(set(manifest_dates), key=lambda d: (manifest_dates.count(d), d))
                     if manifest_dates else max(pd.Timestamp(item["last"]).date() for item in files))
    if manifest_last != as_of:
        raise ValueError("MANIFEST_DATE_MISMATCH")
    return {"as_of": as_of.isoformat(), "fresh_codes": fresh_codes,
            "universe": universe, "hashes": hashes, "rejected": rejected,
            "fetched_at": fetched_at.isoformat(), "primary_codes": primary_codes,
            "symbol_by_code": symbol_by_code, "name_by_code": name_by_code}


def build_record(finding: dict, manifest_path: Path) -> dict:
    as_of = pd.Timestamp(finding["as_of"])
    universe = {code: finding["universe"][code] for code in finding["fresh_codes"]}
    # Match calculate_market_breadth's aligned 60-row window. One symbol with a
    # stray session must not leave almost the entire universe out of its denominator.
    closes = pd.DataFrame({code: frame["Close"] for code, frame in universe.items()}).sort_index()
    eligible = closes.rolling(60, min_periods=60).mean().notna() & closes.notna()
    breadth_count = int(eligible.loc[as_of].sum())
    if breadth_count / len(universe) < 0.90:
        raise ValueError("MARKET_BREADTH_COVERAGE_INSUFFICIENT")
    breadth = float(calculate_market_breadth(universe).loc[as_of])
    if not np.isfinite(breadth):
        raise ValueError("MARKET_BREADTH_UNAVAILABLE")
    strategies = {}
    for key, ranker, score_name in (
        ("early_inception_3d", compute_early_inception_3d_rankings, "inception_3d_score"),
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


BACKFILL_LOOKBACK_SESSIONS = 5
MANIFEST_DIR = ROOT / "quant-research/data/research/T-063"


def manifest_by_sha(sha: str) -> dict | None:
    for path in sorted(MANIFEST_DIR.glob("fetch_manifest_3y_*.json"), reverse=True):
        raw = path.read_bytes()
        if sha in {hashlib.sha256(raw).hexdigest(), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()}:
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def backfill_missing(finding: dict, manifest_path: Path, run_dir: Path,
                     lookback: int = BACKFILL_LOOKBACK_SESSIONS) -> list[str]:
    """Seal recent sessions that the schedule missed, using only bars up to that session.

    Every frame is truncated at the missed session before ranking, so no later bar can
    influence the decision. The record is flagged with ``backfill`` so it is never mistaken
    for a same-evening observation. Existing records are never overwritten.
    """
    existing = {path.stem for path in run_dir.glob("????-??-??.json")}
    if not existing:
        return []
    first, as_of = min(existing), finding["as_of"]
    full = finding["universe"]
    eligible_primary = [code for code in finding["primary_codes"] if code in full]
    if not eligible_primary:
        return []
    counts: dict[str, int] = {}
    for code in eligible_primary:
        for stamp in full[code].index:
            key = stamp.date().isoformat()
            counts[key] = counts.get(key, 0) + 1
    sessions = sorted(day for day, n in counts.items()
                      if first < day < as_of and n / len(eligible_primary) >= 0.90)[-lookback:]
    sealed = []
    for day in sessions:
        if day in existing:
            continue
        # Rankings are cross-sectional: reuse the universe of the last sealed day before the gap.
        reference = max(d for d in existing if d < day)
        ref_record = load_effective_record(run_dir, reference)
        if ref_record is None:
            continue
        ref_manifest = manifest_by_sha(ref_record["source"]["manifest_sha256"])
        primary = (ref_manifest or {}).get("primary_codes") or finding["primary_codes"]
        cut = pd.Timestamp(day)
        universe = {code: frame.loc[:cut] for code, frame in full.items()}
        universe = {code: frame for code, frame in universe.items() if len(frame) >= 125}
        eligible_ref = [code for code in primary if code in universe]
        if not eligible_ref:
            continue
        fresh = [code for code in eligible_ref
                 if universe[code].index[-1] == cut]
        if len(fresh) / len(eligible_ref) < 0.90:
            continue
        past = dict(finding, as_of=day, universe=universe, fresh_codes=fresh, primary_codes=primary)
        record = build_record(past, manifest_path)
        record.pop("run_id")
        record["backfill"] = {
            "recovered_from_as_of": as_of,
            "universe_reference_date": reference,
            "universe_reference_found": ref_manifest is not None,
            "note": "스케줄 누락 복구. 해당 거래일까지의 봉만 잘라 계산. 종목군은 직전 봉인일 기준, 수신 시각은 사후(수정주가 소급 반영 가능).",
        }
        record["run_id"] = hashlib.sha256(_canonical(record)).hexdigest()[:20]
        target = run_dir / f"{day}.json"
        try:
            with target.open("x", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
        except FileExistsError:
            continue
        sealed.append(day)
    return sealed


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
        saved = load_effective_record(run_dir, path.stem)
        if saved is None:
            continue
        if set(saved.get("strategies", {})) not in (set(STRATEGIES), {"mode2", "early_inception"}):
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


def consecutive_observation_days(record: dict, run_dir: Path,
                                 universe: dict[str, pd.DataFrame]) -> dict[tuple[str, str], int]:
    """Count consecutive completed market sessions in the same strategy's sealed top picks."""
    as_of = pd.Timestamp(record["as_of"]).date()
    sessions = sorted({stamp.date() for frame in universe.values()
                       for stamp in frame.index if stamp.date() <= as_of}, reverse=True)
    previous = [day for day in sessions if day < as_of]
    cache: dict[str, dict | None] = {}
    counts = {}
    for strategy, block in record["strategies"].items():
        for pick in block["candidates"]:
            code = pick["code"]
            days = 1
            for day in previous:
                key = day.isoformat()
                if key not in cache:
                    cache[key] = load_effective_record(run_dir, key)
                prior = cache[key]
                if prior is None or not any(item["code"] == code for item in
                                         prior.get("strategies", {}).get(strategy, {}).get("candidates", [])):
                    break
                days += 1
            counts[(strategy, code)] = days
    return counts


def compute_regime_context(
    record: dict,
    run_dir: Path | None = None,
    universe: dict[str, pd.DataFrame] | None = None,
    window: int = 5,
) -> dict | None:
    """Summarize recent market breadth trajectory up to record['as_of'] (zero lookahead)."""
    if "as_of" not in record or "market_breadth_pct" not in record:
        return None
    as_of_str = str(record["as_of"])
    current_breadth = float(record["market_breadth_pct"])
    if not np.isfinite(current_breadth):
        return None

    by_day: dict[str, float] = {as_of_str: current_breadth}
    if run_dir is not None and run_dir.is_dir():
        for path in sorted(run_dir.glob("????-??-??.json")):
            day = path.stem
            if day <= as_of_str:
                eff = load_effective_record(run_dir, day)
                if eff is not None and isinstance(eff.get("market_breadth_pct"), (int, float)):
                    val = float(eff["market_breadth_pct"])
                    if np.isfinite(val):
                        by_day[day] = val

    if len(by_day) < window and universe:
        try:
            series = calculate_market_breadth(universe).loc[:pd.Timestamp(as_of_str)].dropna()
            for ts, val in series.tail(window).items():
                day_key = pd.Timestamp(ts).date().isoformat()
                if day_key <= as_of_str and day_key not in by_day and np.isfinite(float(val)):
                    by_day[day_key] = float(val)
        except Exception:
            pass

    by_day[as_of_str] = current_breadth
    history = [(d, round(v, 1)) for d, v in sorted(by_day.items())[-window:]]
    if len(history) < 2:
        return None

    cur_rounded = history[-1][1]
    start_val = history[0][1]
    peak_val = max(val for _, val in history)
    delta_window = round(cur_rounded - start_val, 1)
    drawdown_from_peak = round(cur_rounded - peak_val, 1)

    consecutive_drops = 0
    for idx in range(len(history) - 1, 0, -1):
        if history[idx][1] < history[idx - 1][1]:
            consecutive_drops += 1
        else:
            break

    if current_breadth < 40.0:
        status = "HALTED"
        badge = "🔴 레짐 차단 (40% 미만)"
    elif drawdown_from_peak <= -5.0 or delta_window <= -5.0 or consecutive_drops >= 3:
        status = "WEAKENING"
        badge = "🟡 레짐 둔화 주의"
    else:
        status = "STABLE"
        badge = "🟢 레짐 안정/확장"

    return {
        "status": status,
        "badge": badge,
        "history": [{"date": d, "breadth_pct": v} for d, v in history],
        "delta_pct": delta_window,
        "drawdown_from_peak_pct": drawdown_from_peak,
        "consecutive_drops": consecutive_drops,
    }


def compute_gate_funnel(
    universe: dict[str, pd.DataFrame] | None,
    eval_date: pd.Timestamp | str,
    market_breadth_pct: float | None = None,
    min_med_amt: float = 500_000_000.0,
    max_mom60_5: float = 0.25,
    max_range_ratio: float = 0.25,
    max_contraction_ratio: float = 0.85,
    min_vol_ratio: float = 1.20,
) -> dict | None:
    """Count stocks surviving each sequential Early Inception gate up to eval_date close."""
    if not universe:
        return None
    eval_ts = pd.Timestamp(eval_date)
    eligible = 0
    trend_liquidity = 0
    not_overextended = 0
    contraction = 0
    breakout_20d = 0
    final_basic = 0

    for df in universe.values():
        if eval_ts not in df.index:
            continue
        hist = df.loc[:eval_ts]
        if len(hist) < 125:
            continue
        c = hist["Close"]
        h = hist["High"]
        l = hist["Low"]
        v = hist["Volume"]
        amt = c * v

        current_c = float(c.iloc[-1])
        if current_c <= 0:
            continue
        eligible += 1

        sma20 = float(c.rolling(20).mean().iloc[-1])
        sma60 = float(c.rolling(60).mean().iloc[-1])
        if current_c < sma20 or (current_c < sma60 and sma20 < sma60):
            continue
        med_amt = float(amt.rolling(20).median().iloc[-1])
        if not np.isfinite(med_amt) or med_amt < min_med_amt:
            continue
        trend_liquidity += 1

        c_t5 = float(c.iloc[-5])
        c_t60 = float(c.iloc[-60])
        if c_t60 <= 0:
            continue
        mom60_5 = (c_t5 - c_t60) / c_t60
        ratio_to_sma60 = current_c / sma60 if sma60 > 0 else 999.0
        if mom60_5 > max_mom60_5 or ratio_to_sma60 > 1.18:
            continue
        not_overextended += 1

        prior_h20 = float(h.iloc[-21:-1].max()) if len(h) >= 21 else float(h.iloc[:-1].max())
        prior_l20 = float(l.iloc[-21:-1].min()) if len(l) >= 21 else float(l.iloc[:-1].min())
        prior_h60 = float(h.iloc[-61:-1].max()) if len(h) >= 61 else float(h.iloc[:-1].max())
        prior_l60 = float(l.iloc[-61:-1].min()) if len(l) >= 61 else float(l.iloc[:-1].min())
        if prior_h20 <= 0 or prior_l20 <= 0 or prior_h60 <= 0 or prior_l60 <= 0:
            continue
        range_20 = prior_h20 - prior_l20
        range_60 = prior_h60 - prior_l60
        range_width_ratio = range_20 / current_c
        contraction_ratio = range_20 / range_60 if range_60 > 0 else 1.0
        if range_width_ratio > max_range_ratio or contraction_ratio > max_contraction_ratio:
            continue
        contraction += 1

        if current_c < prior_h20:
            continue
        breakout_20d += 1

        prior_vol = float(v.iloc[-25:-5].median()) if len(v) >= 25 else float(v.iloc[:-5].median())
        recent_vol = float(v.iloc[-5:].median())
        vol_ratio = recent_vol / prior_vol if prior_vol > 0 else 0.0
        vol_spike_1d = float(v.iloc[-1]) / prior_vol if prior_vol > 0 else 0.0
        if vol_ratio < min_vol_ratio and vol_spike_1d < 1.35:
            continue

        hl = (h.iloc[-20:] - l.iloc[-20:]).replace(0, np.nan)
        mf_mult = ((c.iloc[-20:] - l.iloc[-20:]) - (h.iloc[-20:] - c.iloc[-20:])) / hl
        mf_vol = mf_mult * v.iloc[-20:]
        sum_vol = float(v.iloc[-20:].sum())
        cmf20 = float(mf_vol.sum() / sum_vol) if sum_vol > 0 else -1.0
        if cmf20 < -0.05:
            continue
        final_basic += 1

    if eligible == 0:
        return None
    if market_breadth_pct is not None and float(market_breadth_pct) < 40.0:
        final_basic = 0
        final_3d = 0
    else:
        ranks_3d = compute_early_inception_3d_rankings(universe, eval_ts)
        final_3d = 0 if ranks_3d.empty else int(len(ranks_3d))
    return {
        "eligible": eligible,
        "trend_liquidity": trend_liquidity,
        "not_overextended": not_overextended,
        "contraction": contraction,
        "breakout_20d": breakout_20d,
        "final_basic": final_basic,
        "final_3d": final_3d,
    }


def render_panel(
    record: dict,
    observed_days: dict[tuple[str, str], int],
    regime_context: dict | None = None,
    gate_funnel: dict | None = None,
) -> str:
    """Render a sealed decision without recomputing or relabelling its strategy."""
    strategies = record["strategies"]
    focus_blocks = [block for key, block in strategies.items()
                    if key == "early_inception_3d" or
                    (key == "mode2" and block["score_field"] == "inception_3d_score")]
    focus_codes = {pick["code"] for block in focus_blocks for pick in block["candidates"]}
    breadth = record["market_breadth_pct"]
    lamp = "🟢 관찰 가능" if breadth >= 40.0 else "🔴 후보 선별 중단"
    fetched_kst = datetime.fromisoformat(record["fetched_at"]).astimezone(KST)
    fetched_label = fetched_kst.strftime("%Y-%m-%d %H:%M KST")
    lines = ["### 🌐 시장 상황", "",
             f'> {lamp} · 시장 폭(SMA60) **{breadth:.1f}%** · 기준 완료 일봉 `{record["as_of"]}`']
    if regime_context and len(regime_context.get("history", [])) >= 2:
        hist_items = regime_context["history"]
        trail = " → ".join(f'{item["breadth_pct"]:.1f}% (`{item["date"][5:]}`)' for item in hist_items)
        span = len(hist_items) - 1
        delta = regime_context["delta_pct"]
        dd = regime_context["drawdown_from_peak_pct"]
        lines.extend([
            "",
            f'> {regime_context["badge"]} · **최근 {len(hist_items)}거래일 추이:** {trail} '
            f'({span}일 변화 **{delta:+.1f}%p**, 고점 대비 **{dd:+.1f}%p**)',
        ])
    if gate_funnel and gate_funnel.get("eligible", 0) > 0:
        lines.extend([
            "",
            f'> **게이트별 생존 (`{gate_funnel["eligible"]}`종목 평가):** '
            f'추세·대금 `{gate_funnel["trend_liquidity"]}` → '
            f'비과열 `{gate_funnel["not_overextended"]}` → '
            f'20일 수축 `{gate_funnel["contraction"]}` → '
            f'20일 고점돌파 `{gate_funnel["breakout_20d"]}` → '
            f'**최종 통과: 기본 `{gate_funnel["final_basic"]}`건 / 3일 집중 `{gate_funnel["final_3d"]}`건**',
        ])
    lines.append("")
    for strategy, block in strategies.items():
        is_focus = strategy == "early_inception_3d" or (
            strategy == "mode2" and block["score_field"] == "inception_3d_score")
        is_basic = strategy == "early_inception"
        title = "상승초입 3일 이내" if is_focus else ("상승 초입" if is_basic else "모드 2 · 과거 봉인 기록")
        if is_basic:
            lines.extend([
                "### 🧭 관찰 단계 안내", "",
                "- **기본 관찰 — 상승 초입:** 완만한 수축·거래량 조건으로 후보를 탐색합니다.",
                "- **집중 관찰 — 상승초입 3일 이내:** 별도 엔진의 압축돌파·거래량폭발 조건으로 선별합니다.",
                "- 두 엔진은 각각 최대 5개를 고릅니다. 기본 표의 **3일 목록 포함** 딱지는 같은 평가일의 집중 관찰 목록에도 있는 종목입니다.",
                "- **기본 목록만**은 집중 목록에 없다는 뜻이며 조건 탈락을 뜻하지 않습니다. 두 목록은 순위와 조건이 달라 포함 관계가 보장되지 않습니다.",
                "- 관찰 후보는 매수 확정이 아닙니다. 다음 날 갭·장중 흐름은 별도 확인 대상이며 이 딱지에 반영되지 않습니다.", "",
            ])
        lines.extend([f'### {title}', "",
                      f'> 갱신: `{fetched_label}` · 기준 완료 일봉: `{record["as_of"]}`', "",
                      "| 순위 | 종목 | 연속 포착 | 평가일 종가 | 상대점수 | 관찰 구분 | 수치 근거 |",
                      "|---:|:---|---:|---:|---:|:---|:---|"])
        for rank, pick in enumerate(block["candidates"], 1):
            name = str(pick["name"]).replace("|", "/").replace("\n", " ")
            if is_focus:
                setup_tag = f'[{pick["setup_type"]}] ' if pick.get("setup_type") else ''
                volume = (f'5일 {pick["vol_ratio"]:.2f}x' if pick["vol_ratio"] >= 1.2
                          else f'당일 {pick["vol_spike_1d"]:.2f}x')
                reason = (f'{setup_tag}수축비 {pick["contraction_ratio"]:.2f} · '
                          f'윗꼬리 {pick["upper_wick_ratio"]:.1%} · {volume} · CMF20 {pick["cmf20"]:+.2f}')
                badge = "🎯 3일 목록 포함"
            elif is_basic:
                volume = (f'5일 {pick["vol_ratio"]:.2f}x' if pick["vol_ratio"] >= 1.2
                          else f'당일 {pick["vol_spike_1d"]:.2f}x (5일 {pick["vol_ratio"]:.2f}x)')
                reason = (f'20일 변동폭 {pick["range_width_ratio"]:.1%} · '
                          f'{volume} · CMF20 {pick["cmf20"]:+.2f}')
                badge = ("🎯 3일 목록 포함" if pick["code"] in focus_codes else
                         "🟢 기본 목록만" if focus_blocks else "⚪ 3일 판정 기록 없음")
            else:
                reason = (f'60일 모멘텀 {pick["mom60_5"]:+.1%} · '
                          f'위험조정 모멘텀 {pick["risk_adj_mom"]:.2f} · CMF20 {pick["cmf20"]:+.2f}')
                badge = "과거 모드 2"
            score_val = pick[block["score_field"]]
            days = observed_days.get((strategy, pick["code"]), 1)
            lines.append(f'| {rank} | {name} (`{pick["code"]}`) | {days}거래일째 | {pick["close"]:,.0f}원 | '
                         f'{score_val:.3f} | {badge} | {reason} |')
        if not block["candidates"]:
            lines.append("| - | 조건 충족 없음 | - | - | - | - | - |")
        lines.append("")
    return "\n".join(lines)


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
        if str(exc) in {"PRICE_SNAPSHOT_STALE_OR_FUTURE", "SAME_DAY_CLOSE_UNVERIFIED",
                        "MARKET_BREADTH_COVERAGE_INSUFFICIENT", "INSUFFICIENT_SAME_DAY_COVERAGE"}:
            print(json.dumps({"status": "SKIPPED", "reason": str(exc)}, ensure_ascii=False))
            return 0
        if str(exc) != "SIGNAL_DATE_ALREADY_SEALED_WITH_DIFFERENT_INPUT":
            raise
        # The sealed record stays untouched; the fresh snapshot is used only for gap recovery.
        finding = inspect(args.data, args.manifest)
        prior = load_effective_record(args.runs, finding["as_of"])
        result = {"reused": True, "record": prior, "finding": finding}
    finding = result["finding"]
    backfilled = backfill_missing(finding, args.manifest, args.runs)
    outcomes = resolve(args.runs, finding["universe"], finding["as_of"])
    out_path = args.runs.parent / "outcomes.json"
    out_path.write_text(json.dumps(outcomes, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                        encoding="utf-8")
    record = (load_effective_record(args.runs, finding["as_of"]) or result["record"]) if result["reused"] else result["record"]
    counts = {key: len(block["candidates"]) for key, block in record["strategies"].items()}
    observed_days = consecutive_observation_days(record, args.runs, finding["universe"])
    fresh_codes = finding.get("fresh_codes") or list(finding["universe"])
    eval_universe = {code: finding["universe"][code] for code in fresh_codes if code in finding["universe"]}
    regime_context = compute_regime_context(record, args.runs, eval_universe)
    gate_funnel = compute_gate_funnel(
        eval_universe, record["as_of"], market_breadth_pct=record.get("market_breadth_pct")
    )
    panel = render_panel(record, observed_days, regime_context=regime_context, gate_funnel=gate_funnel)
    (args.runs.parent / "latest.md").write_text(panel, encoding="utf-8")
    if args.readme:
        update_readme_panel(args.readme, panel)
    status = "CAPTURED" if not result["reused"] else ("BACKFILLED" if backfilled else "REUSED")
    print(json.dumps({"as_of": finding["as_of"], "signal_count": counts,
                      "status": status, "backfilled": backfilled,
                      "reused": result["reused"], "resolved": len(outcomes["resolved"]),
                      "pending": outcomes["pending_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

