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
    compute_early_contraction_rankings,
    compute_early_inception_rankings,
    simulate_trade_with_weakening_exit,
)
from scripts.pure_quant_portfolio_manager import (  # noqa: E402
    calculate_market_breadth, compute_factor_rankings,
)

KST = ZoneInfo("Asia/Seoul")
DEFAULT_DATA = ROOT / "quant-research/data/daily_3y"
DEFAULT_RUNS = ROOT / "quant-research/data/research/T-110/runs"
STRATEGIES = ("early_inception", "early_contraction")
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
        ("early_inception", compute_early_inception_rankings, "inception_score"),
        ("early_contraction", compute_early_contraction_rankings, "contraction_score"),
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
    valid_strategy_sets = (
        set(STRATEGIES),
        {"early_inception_3d", "early_inception"},
        {"mode2", "early_inception"},
    )
    for path in sorted(run_dir.glob("????-??-??.json")):
        saved = load_effective_record(run_dir, path.stem)
        if saved is None:
            continue
        if set(saved.get("strategies", {})) not in valid_strategy_sets:
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
        final_contraction = 0
    else:
        ranks_contraction = compute_early_contraction_rankings(universe, eval_ts)
        final_contraction = 0 if ranks_contraction.empty else int(len(ranks_contraction))
    return {
        "eligible": eligible,
        "trend_liquidity": trend_liquidity,
        "not_overextended": not_overextended,
        "contraction": contraction,
        "breakout_20d": breakout_20d,
        "final_basic": final_basic,
        "final_contraction": final_contraction,
    }


def compute_action_directive(
    record: dict,
    regime_context: dict | None = None,
) -> dict[str, str]:
    """Translate sealed operational breadth, regime trend, and candidate counts into a deterministic manual action guide."""
    raw_breadth = record.get("market_breadth_pct")
    try:
        breadth = float(raw_breadth)
    except (TypeError, ValueError):
        breadth = float("nan")

    if not np.isfinite(breadth):
        return {
            "code": "DATA_INVALID",
            "headline": "⚠️ [판단 보류 · 매수 중단] 시장 폭 수치가 유효하지 않습니다. 신규 매수를 멈추고 데이터 상태를 먼저 확인하세요.",
            "new_buy": "전면 보류 (0원) — 정상 완료 일봉과 시장 폭이 확인될 때까지 신규 진입하지 마세요.",
            "holding": "기존 보유분은 각 종목의 -5% 보호 손절가 및 SMA10(10일선) 이탈 기준을 수동 점검하세요.",
            "cash_plan": "대기 자금 현금·파킹 유지.",
        }

    strategies = record.get("strategies") or {}
    n_breakout = len((strategies.get("early_inception") or {}).get("candidates") or [])
    n_contraction = len((strategies.get("early_contraction") or {}).get("candidates") or [])
    regime_status = (
        regime_context.get("status")
        if isinstance(regime_context, dict) and regime_context.get("status")
        else ("HALTED" if breadth < 40.0 else "STABLE")
    )

    if breadth < 40.0 or regime_status == "HALTED":
        return {
            "code": "HALTED_CASH",
            "headline": "🛑 [신규 매수 중단 · 현금/파킹 대피] 지금은 사지 마세요. 보유 종목도 손절·추세선 이탈 시 즉시 현금화하세요.",
            "new_buy": "전면 금지 (0원) — 시장 폭 40% 미만 약세 차단 구간입니다. 기준을 낮춘 억지 매수나 물타기를 금지합니다.",
            "holding": "표의 -5% 보호 손절가 장중 도달 시 즉시 현금화, 또는 종가가 SMA10(10일선) 아래로 마감하면 다음 날 시초가에 전량 현금화하세요.",
            "cash_plan": "대기 자금 100% 현금 또는 파킹형 ETF(CD금리·머니마켓) 유지 (시장 폭 40% 회복 전까지 관망).",
        }

    if regime_status == "WEAKENING":
        if n_breakout == 0 and n_contraction == 0:
            return {
                "code": "WEAKENING_NO_SIGNAL_CASH",
                "headline": "🛑 [관망 · 현금 유지] 레짐이 꺾이고 돌파 후보도 0건입니다. 지금은 사지 말고 현금(파킹)을 지키세요.",
                "new_buy": "매수 금지 (0원) — 변동성 수축 베이스가 무너진 휴식기입니다. 빈칸을 채우려 임의로 종목을 고르지 마세요.",
                "holding": "기존 보유분만 관리 — -5% 손절가 도달 시 즉시 현금화, 종가가 SMA10(10일선)을 이탈하거나 수익권(+2% 이상) 고점 대비 -6% 밀리면 다음 시초가에 현금화하세요.",
                "cash_plan": "유휴 자금 100% 현금·파킹 유지 (레짐 둔화 진정 및 신규 돌파 확정 출현 대기).",
            }
        return {
            "code": "WEAKENING_DEFENSIVE",
            "headline": "⚠️ [신규 매수 보류 · 방어 우선] 시장 폭이 급락(둔화) 중입니다. 신규 진입은 쉬거나 평소 비중의 절반 이하로만 제한하세요.",
            "new_buy": "원칙적 관망 권고 — 둔화 구간의 소수 돌파는 손절 빈도가 높습니다. 진입하더라도 1슬롯 비중을 절반(계좌의 1/6 이하)으로 줄이고 다음 날 시초가가 20일 기준고점 위를 지킬 때만 대응하세요.",
            "holding": "-5% 보호 손절가 및 SMA10(10일선) 종가 이탈 시 미련 없이 즉시 현금화하세요 (추가 물타기 금지).",
            "cash_plan": "현금·파킹 비중 70~100% 방어 유지.",
        }

    if n_breakout == 0 and n_contraction == 0:
        return {
            "code": "STABLE_WAIT_CASH",
            "headline": "⏸️ [현금 대기] 시장은 안정적이나 오늘 조건을 통과한 종목이 없습니다. 현금을 쥐고 다음 신호를 기다리세요.",
            "new_buy": "대기 (0원) — 조건 충족 후보가 나올 때까지 쉬는 것도 전략의 일부입니다.",
            "holding": "기존 보유분은 -5% 손절가·SMA10 종가 이탈 및 수익권 고점 대비 -6% 트레일링 기준에 따라 유지·현금화하세요.",
            "cash_plan": "현금·파킹 대기 (신규 돌파 출현 시 투입 준비).",
        }

    if n_breakout == 0 and n_contraction > 0:
        return {
            "code": "STABLE_WATCH_PIVOT",
            "headline": "👀 [길목 대기 · 고점 돌파 확인 후 매수] 미리 선취매하지 말고, 아래 ‘수축 돌파임박’ 후보가 20일 기준고점을 실제로 넘을 때 진입하세요.",
            "new_buy": f"선취매 보류 — ‘수축 돌파임박’ 후보({n_contraction}건)를 관심종목에 두고 표의 ‘20일 기준고점’ 돌파 확인 시 슬롯당 계좌 1/3 이내로 분할 진입하세요.",
            "holding": "기존 보유분은 -5% 손절가·SMA10 이탈 전까지 추세를 유지하고, 이탈 시 즉시 현금화하세요.",
            "cash_plan": "돌파 확인 전까지 현금·파킹 대기.",
        }

    return {
        "code": "STABLE_BUY_READY",
        "headline": "✅ [이제 분할 매수 가능] 레짐 안정 + ‘20일 돌파 확정’ 신호가 켜졌습니다. 아래 확정 후보를 규칙대로 분할 진입하세요.",
        "new_buy": f"‘20일 돌파 확정’ 상위 후보({n_breakout}건) 대상 최대 3슬롯(종목당 계좌의 1/3 이내) 분할 진입 — 다음 거래일 시초가가 20일 기준고점 위를 유지할 때 진입하고, 과도한 시초가 갭상승(+5% 초과) 추격은 피하세요.",
        "holding": "진입 즉시 -5% 보호 손절가를 걸고, 종가가 SMA10(10일선) 아래로 내려가거나 수익권(+2% 이상) 고점 대비 -6% 하락 마감하면 다음 날 시초가에 전량 현금화하세요.",
        "cash_plan": "3슬롯 외 잔여 자금은 현금·파킹 유지 (동일 종목 중복 물타기 금지).",
    }


def render_panel(
    record: dict,
    observed_days: dict[tuple[str, str], int],
    regime_context: dict | None = None,
    gate_funnel: dict | None = None,
) -> str:
    """Render a sealed decision with Final Early Inception tracks (`early_inception_3d` retired)."""
    strategies = record["strategies"]
    breadth = record["market_breadth_pct"]
    lamp = "🟢 관찰 가능" if breadth >= 40.0 else "🔴 후보 선별 중단"
    fetched_kst = datetime.fromisoformat(record["fetched_at"]).astimezone(KST)
    fetched_label = fetched_kst.strftime("%Y-%m-%d %H:%M KST")
    directive = compute_action_directive(record, regime_context=regime_context)
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
        final_contraction = gate_funnel.get("final_contraction", 0)
        lines.extend([
            "",
            f'> **게이트별 생존 (`{gate_funnel["eligible"]}`종목 평가):** '
            f'추세·대금 `{gate_funnel["trend_liquidity"]}` → '
            f'비과열 `{gate_funnel["not_overextended"]}` → '
            f'20일 수축 `{gate_funnel["contraction"]}` → '
            f'**최종 통과: 돌파 확정 `{gate_funnel["final_basic"]}`건 (고점돌파 `{gate_funnel["breakout_20d"]}`) / '
            f'수축 돌파임박 `{final_contraction}`건**',
        ])
    lines.extend([
        "",
        "### 🎯 오늘의 대응 지침 (규칙 기반 요약)", "",
        f'> **{directive["headline"]}**',
        "",
        f'- **신규 매수:** {directive["new_buy"]}',
        f'- **보유 종목 (현금화 기준):** {directive["holding"]}',
        f'- **대기 자금:** {directive["cash_plan"]}',
        "- *참고: 완료 일봉 운영 규칙(`시장 폭 40%`·`5일 추이`·`후보 수`·`-5%/SMA10 청산`)을 그대로 번역한 수동 대응 기준표이며, 실제 주문·계좌 연동 기능은 없습니다.*",
        "",
        "### 🧭 최종 상승 초입 관찰 안내", "",
        "- **① 상승 초입 · 20일 돌파 확정:** 20일 변동성 수축 베이스에서 직전 20일 고점을 종가로 돌파하고 수급이 점화된 정석 후보입니다 (단기 `+5%` 도달 탄력 우위).",
        "- **② 상승 초입 · 20일 수축 돌파임박:** 타이트한 20일 수축(`수축비 ≤ 0.65`) 상태에서 직전 20일 고점 `-3.0%` 이내에 근접하며 거래량이 먼저 점화된 길목 후보입니다 (기대수익률·손익비 우위, 실제 고점 돌파 여부 확인 권장).",
        "- **전략 정비 (`T-131`):** 기존 `상승초입 3일 이내`는 거래량 5배 급등 추격에 따른 과열 물림·손절률 열위가 3개년 백필에서 확인되어 **폐기**되었습니다.",
        "- **MTS 실전 참조:** `20일 기준고점 (돌파이격)`은 직전 20일 최고가 대비 현재 종가 위치이며, `참조 손절 / 추세선`은 평가일 종가 대비 `-5%` 보호 손절선과 `SMA10`(단기 추세 약화 기준선)입니다. 실제 주문·체결 기능이 없는 관찰 전용 기록입니다.",
        "",
    ])

    has_legacy_mode2 = (
        "mode2" in strategies
        and strategies["mode2"].get("score_field") == "composite_score"
    )
    if has_legacy_mode2:
        display_blocks = [
            ("mode2", "모드 2 · 과거 봉인 기록", strategies["mode2"]),
            ("early_inception", "상승 초입 · 20일 돌파 확정",
             strategies.get("early_inception", {"score_field": "inception_score", "candidates": []})),
        ]
    else:
        display_blocks = [
            ("early_inception", "상승 초입 · 20일 돌파 확정",
             strategies.get("early_inception", {"score_field": "inception_score", "candidates": []})),
            ("early_contraction", "상승 초입 · 20일 수축 돌파임박",
             strategies.get("early_contraction", {"score_field": "contraction_score", "candidates": []})),
        ]

    for strategy, title, block in display_blocks:
        lines.extend([
            f"### {title}", "",
            f'> 갱신: `{fetched_label}` · 기준 완료 일봉: `{record["as_of"]}`', "",
            "| 순위 | 종목 | 연속 포착 | 평가일 종가 | 20일 기준고점 (돌파이격) | 참조 손절 / 추세선 | 상대점수 | 수치 근거 |",
            "|---:|:---|---:|---:|:---|:---|---:|:---|",
        ])
        for rank, pick in enumerate(block["candidates"], 1):
            name = str(pick["name"]).replace("|", "/").replace("\n", " ")
            if pick.get("prior_h20"):
                ext_pct = pick.get("breakout_ext_pct", (pick["close"] / pick["prior_h20"] - 1.0) * 100.0)
                pivot_col = f'{pick["prior_h20"]:,.0f}원 ({ext_pct:+.1f}%)'
            else:
                pivot_col = "-"

            stop_5 = pick.get("stop_price_5pct") or (pick["close"] * 0.95)
            if pick.get("sma10"):
                ref_col = f'-5% {stop_5:,.0f}원 · SMA10 {pick["sma10"]:,.0f}원'
            else:
                ref_col = f'-5% {stop_5:,.0f}원'

            if strategy == "mode2":
                reason = (f'60일 모멘텀 {pick["mom60_5"]:+.1%} · '
                          f'위험조정 모멘텀 {pick["risk_adj_mom"]:.2f} · CMF20 {pick["cmf20"]:+.2f}')
            else:
                volume = (f'5일 {pick["vol_ratio"]:.2f}x (당일 {pick["vol_spike_1d"]:.2f}x)'
                          if pick["vol_ratio"] >= 1.2
                          else f'당일 {pick["vol_spike_1d"]:.2f}x (5일 {pick["vol_ratio"]:.2f}x)')
                if "contraction_ratio" in pick:
                    reason = (f'수축비 {pick["contraction_ratio"]:.2f} '
                              f'(변동폭 {pick["range_width_ratio"]:.1%}) · '
                              f'{volume} · CMF20 {pick["cmf20"]:+.2f}')
                else:
                    reason = (f'20일 변동폭 {pick["range_width_ratio"]:.1%} · '
                              f'{volume} · CMF20 {pick["cmf20"]:+.2f}')
            score_val = pick[block["score_field"]]
            days = observed_days.get((strategy, pick["code"]), 1)
            lines.append(
                f'| {rank} | {name} (`{pick["code"]}`) | {days}거래일째 | {pick["close"]:,.0f}원 | '
                f'{pivot_col} | {ref_col} | {score_val:.3f} | {reason} |'
            )
        if not block["candidates"]:
            lines.append("| - | 조건 충족 없음 | - | - | - | - | - | - |")
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

