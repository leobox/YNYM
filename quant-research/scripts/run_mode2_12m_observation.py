"""Replay the stored Mode 2 daily factor observation as a virtual portfolio.

No market requests, account access, or real orders are made.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from scripts.pure_quant_portfolio_manager import calculate_market_breadth, compute_factor_rankings
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pure_quant_portfolio_manager import calculate_market_breadth, compute_factor_rankings


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "quant-research/data/daily_3y"
OUT = ROOT / "quant-research/data/research/T-089"
SOURCE_MANIFEST = ROOT / "quant-research/data/research/T-063/fetch_manifest_20260920_1958.json"
START_CASH = 1_000_000.0
BUY_FEE, SELL_FEE, SELL_TAX, SLIP = 0.00015, 0.00015, 0.002, 0.001


def load_data(directory: Path):
    universe, hashes = {}, {}
    rejected = {"missing_ohlcv": 0, "invalid_ohlcv": 0, "zero_volume": 0}
    for path in sorted(directory.glob("*.csv")):
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        df = pd.read_csv(path, parse_dates=["Date"], index_col="Date").sort_index()
        if not {"Open", "High", "Low", "Close", "Volume"}.issubset(df.columns):
            raise ValueError(f"OHLCV columns missing: {path}")
        if df.index.has_duplicates:
            raise ValueError(f"Duplicate dates: {path}")
        required = df[["Open", "High", "Low", "Close", "Volume"]]
        missing = required.isna().any(axis=1) | ~np.isfinite(required).all(axis=1)
        invalid = (required[["Open", "High", "Low", "Close"]] <= 0).any(axis=1) | (df["Volume"] < 0)
        invalid |= (df["High"] < required[["Open", "Low", "Close"]].max(axis=1)) | (df["Low"] > required[["Open", "High", "Close"]].min(axis=1))
        rejected["missing_ohlcv"] += int(missing.sum())
        rejected["invalid_ohlcv"] += int((invalid & ~missing).sum())
        df = df.loc[~(missing | invalid)].copy()
        rejected["zero_volume"] += int((df["Volume"] == 0).sum())
        if len(df) >= 125:
            universe[path.stem] = df
    if not universe:
        raise ValueError("No usable daily data")
    return universe, hashes, rejected


def replay(universe, start=None, end=None, initial_cash=START_CASH, rebalance_every=20,
           min_activity_ratio=None, selection_mode="factor", min_med_amt=500_000_000.0):
    if selection_mode not in {"factor", "turnover"}:
        raise ValueError(f"Unknown selection mode: {selection_mode}")
    breadth = calculate_market_breadth(universe).dropna().sort_index()
    end = pd.Timestamp(end) if end else breadth.index[-1]
    start = pd.Timestamp(start) if start else end - pd.DateOffset(years=1)
    dates = breadth.index[(breadth.index >= start) & (breadth.index <= end)]
    if len(dates) < 2 or breadth.index.get_loc(dates[0]) == 0:
        raise ValueError("Insufficient evaluation dates or pre-start signal date")
    prior = breadth.index[breadth.index.get_loc(dates[0]) - 1]
    cash, positions, last_close = float(initial_cash), {}, {}
    trades, daily, decisions = [], [], []
    missing_held_bars = 0
    unfilled = 0

    def sell(code, date, price, reason):
        nonlocal cash
        pos = positions.pop(code)
        exec_price = float(price) * (1 - SLIP)
        proceeds = pos["qty"] * exec_price * (1 - SELL_FEE - SELL_TAX)
        cash += proceeds
        trades.append({"code": code, "entry_date": pos["date"], "exit_date": str(date.date()),
                       "qty": pos["qty"], "cost": pos["cost"], "proceeds": proceeds,
                       "pnl": proceeds - pos["cost"], "return_pct": 100 * (proceeds / pos["cost"] - 1),
                       "reason": reason})

    for i, date in enumerate(dates):
        signal_date = prior if i == 0 else dates[i - 1]
        rebalancing = i % rebalance_every == 0
        if rebalancing:
            regime = float(breadth.loc[signal_date])
            ranking = (compute_factor_rankings(universe, signal_date,
                                               min_med_amt=min_med_amt,
                                               min_activity_ratio=min_activity_ratio)
                       if regime >= 40 else pd.DataFrame())
            if not ranking.empty:
                key = "recent_amt" if selection_mode == "turnover" else "composite_score"
                ranking = ranking.loc[np.isfinite(ranking[key])].sort_values(
                    [key, "code"], ascending=[False, True]
                )
            picks = [] if ranking.empty else ranking.head(10)["code"].tolist()
            decisions.append({"signal_date": str(signal_date.date()), "execution_date": str(date.date()),
                              "breadth_pct": regime, "eligible": len(ranking), "picks": ",".join(picks)})
            # First sell existing positions at this session's open. Untradable holdings stay in the account.
            for code in list(positions):
                frame = universe[code]
                if date in frame.index and frame.at[date, "Volume"] > 0:
                    sell(code, date, frame.at[date, "Open"], "REBALANCE")
            equity_at_open = cash + sum(p["qty"] * last_close.get(code, p["entry_price"])
                                        for code, p in positions.items())
            slot_budget = equity_at_open / 10
            for code in picks:
                if code in positions:
                    continue
                frame = universe[code]
                if date not in frame.index or frame.at[date, "Volume"] <= 0:
                    unfilled += 1
                    continue
                entry = float(frame.at[date, "Open"]) * (1 + SLIP)
                qty = int(min(slot_budget, cash) / (entry * (1 + BUY_FEE)))
                if qty < 1:
                    unfilled += 1
                    continue
                cost = qty * entry * (1 + BUY_FEE)
                cash -= cost
                positions[code] = {"qty": qty, "entry_price": entry, "cost": cost, "date": str(date.date())}
        # After the open, a stop can fire, including on the entry session.
        for code in list(positions):
            frame = universe[code]
            if date not in frame.index or frame.at[date, "Volume"] <= 0:
                missing_held_bars += 1
                continue
            row = frame.loc[date]
            stop = positions[code]["entry_price"] * .85
            if row["Low"] <= stop:
                sell(code, date, min(float(row["Open"]), stop), "STOP_GAP" if row["Open"] <= stop else "STOP")
        for code in positions:
            frame = universe[code]
            if date in frame.index and frame.at[date, "Volume"] > 0:
                last_close[code] = float(frame.at[date, "Close"])
        if i == len(dates) - 1:
            for code in list(positions):
                frame = universe[code]
                if date not in frame.index or frame.at[date, "Volume"] <= 0:
                    raise ValueError(f"Cannot liquidate {code} on final date")
                sell(code, date, frame.at[date, "Close"], "FINAL")
        pos_val = sum(p["qty"] * last_close.get(code, p["entry_price"]) for code, p in positions.items())
        daily.append({"date": str(date.date()), "cash": cash, "position_value": pos_val,
                      "equity": cash + pos_val, "positions": len(positions),
                      "breadth_pct": float(breadth.loc[date])})
    return pd.DataFrame(trades), pd.DataFrame(daily), pd.DataFrame(decisions), {"unfilled": unfilled, "missing_held_bars": missing_held_bars}


def main():
    universe, hashes, rejected = load_data(DATA)
    source_manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    expected_hashes = {item["code"] + ".csv": item["sha256"] for item in source_manifest["files"]}
    if hashes != expected_hashes:
        raise ValueError("Stored daily CSV files differ from the T-063 source manifest")
    trades, daily, decisions, counts = replay(universe)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, frame in (("trades", trades), ("daily", daily), ("decisions", decisions)):
        frame.to_csv(OUT / f"{name}.csv", index=False, encoding="utf-8-sig")
    manifest = {"source": "stored Yahoo Finance daily CSV", "source_dir": str(DATA.relative_to(ROOT)),
                "source_manifest": str(SOURCE_MANIFEST.relative_to(ROOT)),
                "source_manifest_sha256": hashlib.sha256(SOURCE_MANIFEST.read_bytes()).hexdigest(),
                "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "files": len(hashes), "sha256": hashes, "rejected_rows": rejected,
                "universe_stocks": len(universe), "period": [daily.iloc[0]["date"], daily.iloc[-1]["date"]],
                "assumptions": {"initial_cash": START_CASH, "slots": 10, "rebalance_sessions": 20,
                                "stop_pct": .15, "breadth_floor_pct": 40,
                                "buy_fee": BUY_FEE, "sell_fee": SELL_FEE, "sell_tax": SELL_TAX, "slippage_each_side": SLIP},
                "counts": counts}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"period": manifest["period"], "stocks": len(universe), "trades": len(trades),
                      "decisions": len(decisions), "final_equity": daily.iloc[-1]["equity"], **counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
