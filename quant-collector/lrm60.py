"""LRM-60 v2.0 completed-bar signals and a read-only, forward paper account.

No brokerage, account, order, or execution API is used. Fills below are
historical OHLC assumptions recorded only after a bar is complete.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd


VERSION = "lrm60_v2"
EXPERIMENT_VERSION = "lrm60_v2_experiment_20260924"
INITIAL_CASH = 1_000_000.0
MAX_SLOTS = 3
FEE_SIDE = 0.0023 / 2
SLIPPAGE = 0.0015


def inspect_signal(df: pd.DataFrame, ts: pd.Timestamp) -> dict | None:
    """Inspect one completed 60m bar using only its prefix and earlier bars."""
    if ts not in df.index:
        return None
    idx = df.index.get_loc(ts)
    if not isinstance(idx, (int, np.integer)) or idx < 26:
        return None
    prefix = df.iloc[:idx + 1]
    if prefix.index.has_duplicates or not prefix.index.is_monotonic_increasing:
        return None
    bar = prefix.iloc[-1]
    ohlcv = prefix[["Open", "High", "Low", "Close", "Volume"]].to_numpy(dtype=float)
    if not np.isfinite(ohlcv).all() or (prefix["Volume"] < 0).any():
        return None
    if (prefix["High"] < prefix[["Open", "Low", "Close"]].max(axis=1)).any():
        return None
    if (prefix["Low"] > prefix[["Open", "High", "Close"]].min(axis=1)).any():
        return None
    if ((prefix.index.hour == 15) & (prefix["Volume"] == 0) &
            (prefix["High"] == prefix["Low"])).any():
        return None

    same_slot = prefix.iloc[:-1]
    same_slot = same_slot[same_slot.index.time == ts.time()]
    amounts = (same_slot["Close"] * same_slot["Volume"]).tail(20)
    if len(amounts) < 5:
        return None
    sma12 = float(prefix["Close"].tail(12).mean())
    sma26 = float(prefix["Close"].tail(26).mean())
    sma26_prev = float(prefix["Close"].iloc[-27:-1].mean())
    level = float(prefix["High"].iloc[-21:-1].max())
    width = float(bar["High"] - bar["Low"])
    if sma26_prev <= 0 or width <= 0 or not np.isfinite([sma12, sma26, level]).all():
        return None

    amount = float(bar["Close"] * bar["Volume"])
    r_vol = amount / max(float(amounts.median()), 1e7)
    d_base = (float(bar["Open"]) - sma26_prev) / sma26_prev * 100
    cli = (float(bar["Close"]) - float(bar["Low"])) / width
    gates = {
        "trend": bool(bar["Close"] > sma12 > sma26),
        "breakout": bool(bar["Close"] > level),
        "liquidity": bool(amount >= 1e9 and r_vol >= 2.5),
        "candle": bool(cli >= 0.70 and bar["Close"] > bar["Open"] and -1 <= d_base <= 3),
    }
    return {
        "bar_ts": ts.isoformat(), "signal_close": float(bar["Close"]),
        "breakout_level": level, "amount_e8": round(amount / 1e8, 2),
        "r_vol": r_vol, "d_base_pct": round(d_base, 3),
        "cli": round(cli, 4), "gates": gates,
        "passed": all(gates.values()),
    }


def regime_at(index_feeds: dict[str, pd.DataFrame], market: str,
              ts: pd.Timestamp) -> bool | None:
    """Prior *completed* daily index close above its 20-day SMA; None fails closed."""
    daily = index_feeds.get(market)
    if daily is None or "Close" not in daily or daily.index.has_duplicates:
        return None
    dates = pd.to_datetime(daily.index)
    prior = daily.loc[dates.date < ts.date(), "Close"].tail(20)
    if len(prior) < 20 or not np.isfinite(prior.to_numpy(dtype=float)).all():
        return None
    if (ts.date() - pd.Timestamp(prior.index[-1]).date()).days > 5:
        return None
    return bool(prior.iloc[-1] > prior.mean())


class LRM60PaperBook:
    """Idempotent paper-only three-slot account, advanced by completed bars."""

    def __init__(self, path: Path, experimental: bool = False):
        self.path = Path(path)
        self.experimental = experimental
        self.version = EXPERIMENT_VERSION if experimental else VERSION
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("version") != self.version:
                raise ValueError("LRM-60 state version mismatch")
        else:
            self.state = {
                "version": self.version, "cash": INITIAL_CASH, "equity": INITIAL_CASH,
                "positions": {}, "pending": [], "trades": [],
                "last_bar_ts": None, "last_candidates": [], "gate_counts": {},
                "coverage": {}, "data_gaps": [], "regime": {},
            }

    def _stop(self, pos: dict) -> float:
        return max(pos["entry_price"] * .96,
                   pos["breakout_level"] * (.97 if self.experimental else .99))

    def _partial(self, code: str, price: float, reason: str, ts: pd.Timestamp) -> None:
        pos = self.state["positions"][code]
        qty = pos["qty"] // 2
        if qty < 1:
            return
        cost = pos["cost"] * qty / pos["qty"]
        proceeds = qty * price * (1 - FEE_SIDE)
        self.state["cash"] += proceeds
        self.state["trades"].append({
            "code": code, "name": pos["name"], "entry_ts": pos["entry_ts"],
            "exit_ts": ts.isoformat(), "entry_price": pos["entry_price"],
            "exit_price": round(price, 4), "qty": qty,
            "pnl": round(proceeds - cost, 2), "reason": reason,
        })
        pos["qty"] -= qty
        pos["cost"] -= cost
        pos["target_1_done"] = True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".lrm60-", suffix=".tmp", delete=False) as out:
                tmp_name = out.name
                json.dump(self.state, out, ensure_ascii=False, indent=2, allow_nan=False)
                out.write("\n")
            os.replace(tmp_name, self.path)
        finally:
            if tmp_name and os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _sell(self, code: str, price: float, reason: str, ts: pd.Timestamp) -> None:
        pos = self.state["positions"].pop(code)
        proceeds = pos["qty"] * price * (1 - FEE_SIDE)
        self.state["cash"] += proceeds
        self.state["trades"].append({
            "code": code, "name": pos["name"], "entry_ts": pos["entry_ts"],
            "exit_ts": ts.isoformat(), "entry_price": pos["entry_price"],
            "exit_price": round(price, 4), "qty": pos["qty"],
            "pnl": round(proceeds - pos["cost"], 2), "reason": reason,
        })

    @staticmethod
    def _bar(feeds: dict[str, pd.DataFrame], code: str, ts: pd.Timestamp):
        df = feeds.get(code)
        return None if df is None or ts not in df.index else df.loc[ts]

    def advance(self, feeds: dict[str, pd.DataFrame], metadata: dict[str, dict],
                index_feeds: dict[str, pd.DataFrame] | None = None) -> bool:
        times = sorted({ts for df in feeds.values() for ts in df.index})
        if not times:
            return False
        last = self.state["last_bar_ts"]
        if last is None:
            times = times[-1:]  # launch forward; do not replay today's universe as historical holdings
        else:
            last_ts = pd.Timestamp(last)
            times = [ts for ts in times if ts > last_ts]
        for ts in times:
            self._step(ts, feeds, metadata, index_feeds or {})
        if times:
            self.save()
        return bool(times)

    def _step(self, ts: pd.Timestamp, feeds: dict[str, pd.DataFrame], metadata: dict[str, dict],
              index_feeds: dict[str, pd.DataFrame]) -> None:
        state = self.state
        positions = state["positions"]
        starting = set(positions)
        selected = state["pending"]
        entry_cash = state["cash"]
        gap_codes = [code for code in starting if
                     (self._bar(feeds, code, ts) is None or
                      self._bar(feeds, code, ts)["Volume"] <= 0)]
        if gap_codes:
            state["data_gaps"].append({"bar_ts": ts.isoformat(), "codes": sorted(gap_codes)})
            state["data_gaps"] = state["data_gaps"][-50:]

        equity_open = state["cash"]
        for code, pos in positions.items():
            bar = self._bar(feeds, code, ts)
            equity_open += pos["qty"] * (float(bar["Open"]) if bar is not None else pos["last_close"])
        slot_cap = equity_open / MAX_SLOTS

        # Existing holdings: opening gaps and timeout before the intrabar path.
        for code in starting:
            bar = self._bar(feeds, code, ts)
            if bar is None or bar["Volume"] <= 0:
                continue
            pos = positions[code]
            stop = self._stop(pos)
            target = pos["entry_price"] * 1.08
            trail = pos["peak_price"] * .975 if pos["peak_price"] >= pos["entry_price"] * 1.04 else None
            if bar["Open"] <= stop:
                self._sell(code, float(bar["Open"]) * (1 - SLIPPAGE), "STOP_GAP", ts)
            elif bar["Open"] >= target:
                self._sell(code, float(bar["Open"]) * (1 - SLIPPAGE), "TARGET_GAP", ts)
            elif trail is not None and bar["Open"] <= trail:
                self._sell(code, float(bar["Open"]) * (1 - SLIPPAGE), "TRAILING_GAP", ts)
            elif pos["bars_held"] >= 30:
                self._sell(code, float(bar["Open"]) * (1 - SLIPPAGE), "TIME_EXPIRATION", ts)
            elif (self.experimental and not pos.get("target_1_done") and
                  bar["Open"] >= pos["entry_price"] * 1.05):
                self._partial(code, float(bar["Open"]) * (1 - SLIPPAGE), "PARTIAL_GAP", ts)

        # Pending selections were fixed at the previous close. No replacements.
        if not gap_codes:
            for sig in selected:
                code = sig["code"]
                if code in starting or code in positions or len(starting) + len(selected) > MAX_SLOTS:
                    continue
                bar = self._bar(feeds, code, ts)
                if bar is None or bar["Volume"] <= 0:
                    continue
                if bar["Open"] > sig["signal_close"] * 1.025:
                    continue
                entry_price = float(bar["Open"]) * (1 + SLIPPAGE)
                if entry_price <= sig["breakout_level"] * .99:
                    continue
                budget = min(slot_cap, entry_cash)
                qty = int(budget // (entry_price * (1 + FEE_SIDE)))
                if qty <= 0:
                    continue
                cost = qty * entry_price * (1 + FEE_SIDE)
                state["cash"] -= cost
                entry_cash -= cost
                positions[code] = {
                    "code": code, "name": sig["name"], "market": sig["market"],
                    "entry_ts": ts.isoformat(),
                    "entry_price": entry_price, "breakout_level": sig["breakout_level"],
                    "qty": qty, "cost": cost, "peak_price": entry_price,
                    "last_close": entry_price, "bars_held": 0, "target_1_done": False,
                }

        # Intrabar path, including newly filled positions. Previous peak only.
        for code in list(positions):
            bar = self._bar(feeds, code, ts)
            if bar is None or bar["Volume"] <= 0:
                continue
            pos = positions[code]
            stop = self._stop(pos)
            target = pos["entry_price"] * 1.08
            trail = pos["peak_price"] * .975 if pos["peak_price"] >= pos["entry_price"] * 1.04 else None
            if bar["Low"] <= stop:
                self._sell(code, stop * (1 - SLIPPAGE), "STOP_TOUCH", ts)
            elif trail is not None and bar["Low"] <= trail:
                self._sell(code, trail * (1 - SLIPPAGE), "TRAILING_STOP", ts)
            elif bar["High"] >= target:
                self._sell(code, target, "TARGET_TOUCH", ts)
            else:
                if (self.experimental and not pos.get("target_1_done") and
                        bar["High"] >= pos["entry_price"] * 1.05):
                    self._partial(code, pos["entry_price"] * 1.05, "PARTIAL_TOUCH", ts)
                pos["peak_price"] = max(pos["peak_price"], float(bar["High"]))
                pos["last_close"] = float(bar["Close"])
                pos["bars_held"] += 1

        state["equity"] = (None if any(self._bar(feeds, code, ts) is None or
                                       self._bar(feeds, code, ts)["Volume"] <= 0
                                       for code in positions) else
                           state["cash"] + sum(p["qty"] * p["last_close"] for p in positions.values()))

        inspected = []
        coverage = {"scanned": len(feeds), "evaluated": 0, "passed": 0,
                    "missing_bar": 0, "insufficient": 0}
        gate_counts = {key: 0 for key in ("trend", "breakout", "liquidity", "candle")}
        for code, df in feeds.items():
            if ts not in df.index:
                coverage["missing_bar"] += 1
                continue
            result = inspect_signal(df, ts)
            if result is None:
                coverage["insufficient"] += 1
                continue
            coverage["evaluated"] += 1
            for key, passed in result["gates"].items():
                gate_counts[key] += int(passed)
            if result["passed"]:
                coverage["passed"] += 1
                meta = metadata.get(code, {})
                inspected.append({"code": code, "name": meta.get("name", code),
                                  "market": meta.get("market", ""), **result})
        inspected.sort(key=lambda row: (-row["r_vol"], row["code"]))
        free = MAX_SLOTS - len(positions)
        regime = {market: regime_at(index_feeds, market, ts)
                  for market in ("KOSPI", "KOSDAQ")} if self.experimental else {}
        eligible = [row for row in inspected if row["code"] not in positions and
                    (not self.experimental or regime.get(row["market"]) is True)]
        state["pending"] = eligible[:free]
        state["last_candidates"] = inspected[:10]
        state["regime"] = regime
        state["gate_counts"] = gate_counts
        state["coverage"] = coverage
        state["last_bar_ts"] = ts.isoformat()
