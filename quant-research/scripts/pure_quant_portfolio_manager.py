"""
Pure Quant Mode 2 Portfolio Manager (Production Operation Engine).

Design Architecture:
- Strategy: Mode 2 (Institutional Monthly Factor Rebalance + -15% Catastrophic Airbag Stop)
- Universe: Cross-sectional KRX equities (Daily OHLCV)
- Factor Model:
  1. Risk-Adjusted Momentum (Sharpe Momentum: Mom60_5 / Vol60 * sqrt(252)) [40%]
  2. Intermediate-Term Momentum (Jegadeesh-Titman Mom60_5) [30%]
  3. Smart Money Accumulation (Chaikin Money Flow CMF20) [30%]
- Regime Defense: Market Breadth (% > SMA60). If < 40%, shift/hold 100% Cash.
- Airbag Defense: -15.0% Stop Loss against catastrophic events.
- Portfolio Structure: 10 equal slots (10% allocation per slot).
- Rebalance Cycle: 20 trading days (~1 month).
- Safety Invariant: NEVER executes real market orders. Pure analytical state engine.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


import numpy as np
import pandas as pd

try:
    from scripts.rag_thesis_engine import RagThesisEngine
except ModuleNotFoundError:
    try:
        from rag_thesis_engine import RagThesisEngine
    except ModuleNotFoundError:
        import sys
        sys.path.append(str(Path(__file__).resolve().parent))
        from rag_thesis_engine import RagThesisEngine




DEFAULT_DATA_DIR = "quant-research/data/daily_3y"
DEFAULT_STATE_FILE = "quant-research/data/portfolio_state.json"


@dataclass
class Position:
    code: str
    qty: int
    entry_price: float
    entry_date: str
    stop_price: float
    days_held: int = 0
    last_price: float = 0.0


@dataclass
class PortfolioState:
    cash: float
    initial_capital: float
    last_rebalance_date: str | None = None
    last_signal_date: str | None = None
    positions: dict[str, dict] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)


def load_universe(data_dir: str, min_bars: int = 130) -> dict[str, pd.DataFrame]:
    files = glob.glob(os.path.join(data_dir, "*.csv"))
    universe = {}
    for f in sorted(files):
        code = os.path.splitext(os.path.basename(f))[0]
        try:
            df = pd.read_csv(f, parse_dates=["Date"], index_col="Date")
            df = df.sort_index()
            df = df[~df.index.duplicated(keep="first")]
            req = ["Open", "High", "Low", "Close", "Volume"]
            if not set(req).issubset(df.columns) or len(df) < min_bars:
                continue
            df = df.dropna(subset=req)
            df["High"] = df[["Open", "High", "Low", "Close"]].max(axis=1)
            df["Low"] = df[["Open", "High", "Low", "Close"]].min(axis=1)
            universe[code] = df
        except Exception:
            continue
    return universe


def calculate_market_breadth(universe: dict[str, pd.DataFrame], window: int = 60) -> pd.Series:
    close_dict = {code: df["Close"] for code, df in universe.items()}
    px = pd.DataFrame(close_dict).sort_index()
    ma = px.rolling(window, min_periods=window).mean()
    valid = ma.notna() & px.notna()
    above = (px > ma) & valid
    breadth = (above.sum(axis=1) / valid.sum(axis=1)) * 100.0
    return breadth


def compute_factor_rankings(
    universe: dict[str, pd.DataFrame],
    eval_date: pd.Timestamp,
    min_med_amt: float = 500_000_000.0
) -> pd.DataFrame:
    """
    Computes cross-sectional factor rankings on eval_date strictly using
    data available up to eval_date close (zero lookahead).
    """
    rows = []
    for code, df in universe.items():
        if eval_date not in df.index:
            continue
        hist = df.loc[:eval_date]
        if len(hist) < 125:
            continue

        c = hist["Close"]
        h = hist["High"]
        l = hist["Low"]
        v = hist["Volume"]
        amt = c * v

        current_c = float(c.iloc[-1])
        sma60 = float(c.rolling(60).mean().iloc[-1])
        sma120 = float(c.rolling(120).mean().iloc[-1])

        # Trend Filter: Price > SMA120 and SMA60 > SMA120
        if not (current_c > sma120 and sma60 > sma120):
            continue

        # Liquidity Filter: 20-day median trading value
        med_amt = float(amt.rolling(20).median().iloc[-1])
        if med_amt < min_med_amt:
            continue

        # 1. Intermediate Momentum (Mom60_5)
        c_t5 = float(c.iloc[-5])
        c_t60 = float(c.iloc[-60])
        if c_t60 <= 0:
            continue
        mom60_5 = (c_t5 - c_t60) / c_t60

        # 2. Risk-Adjusted Momentum (Sharpe Momentum)
        daily_ret = c.pct_change().dropna()
        if len(daily_ret) < 60:
            continue
        vol60 = float(daily_ret.iloc[-60:].std() * np.sqrt(252))
        if vol60 <= 0 or np.isnan(vol60):
            continue
        risk_adj_mom = mom60_5 / vol60

        # 3. Chaikin Money Flow (CMF 20)
        hl = (h.iloc[-20:] - l.iloc[-20:]).replace(0, np.nan)
        mf_mult = ((c.iloc[-20:] - l.iloc[-20:]) - (h.iloc[-20:] - c.iloc[-20:])) / hl
        mf_vol = mf_mult * v.iloc[-20:]
        sum_vol = float(v.iloc[-20:].sum())
        if sum_vol <= 0:
            continue
        cmf20 = float(mf_vol.sum() / sum_vol)

        rows.append({
            "code": code,
            "close": current_c,
            "mom60_5": mom60_5,
            "risk_adj_mom": risk_adj_mom,
            "cmf20": cmf20,
            "med_amt": med_amt
        })

    if not rows:
        return pd.DataFrame()

    res = pd.DataFrame(rows)
    res["rank_ramom"] = res["risk_adj_mom"].rank(pct=True)
    res["rank_mom"] = res["mom60_5"].rank(pct=True)
    res["rank_cmf"] = res["cmf20"].rank(pct=True)

    res["composite_score"] = (
        0.40 * res["rank_ramom"] +
        0.30 * res["rank_mom"] +
        0.30 * res["rank_cmf"]
    )
    return res.sort_values(by="composite_score", ascending=False).reset_index(drop=True)


class PortfolioManager:
    def __init__(self, state_file: str = DEFAULT_STATE_FILE, data_dir: str = DEFAULT_DATA_DIR, kb_path: str | None = None):
        self.state_file = Path(state_file)
        self.data_dir = data_dir
        self.universe: dict[str, pd.DataFrame] | None = None
        self.breadth: pd.Series | None = None
        self.rag = RagThesisEngine(kb_path=kb_path) if kb_path else RagThesisEngine()

    def load_data(self):

        if self.universe is None:
            self.universe = load_universe(self.data_dir)
            self.breadth = calculate_market_breadth(self.universe)

    def load_state(self) -> PortfolioState:
        if not self.state_file.exists():
            return PortfolioState(cash=1_000_000.0, initial_capital=1_000_000.0)
        with open(self.state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return PortfolioState(
            cash=data.get("cash", 1_000_000.0),
            initial_capital=data.get("initial_capital", 1_000_000.0),
            last_rebalance_date=data.get("last_rebalance_date"),
            last_signal_date=data.get("last_signal_date"),
            positions=data.get("positions", {}),
            history=data.get("history", [])
        )

    def save_state(self, state: PortfolioState):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=2, ensure_ascii=False)

    def initialize_portfolio(self, capital: float = 1_000_000.0):
        state = PortfolioState(cash=capital, initial_capital=capital)
        self.save_state(state)
        print(f"[OK] Portfolio initialized with {capital:,.0f} KRW.")

    def _resolve_date(self, requested: str | None) -> pd.Timestamp:
        """Resolve an observed session date; never substitute a fake normal regime."""
        self.load_data()
        observed = self.breadth.dropna().index
        if requested:
            dt = pd.Timestamp(requested)
            if dt not in observed:
                raise ValueError(f"No complete market-breadth observation for {dt.date()}")
            return dt
        if len(observed) == 0:
            raise ValueError("No complete market-breadth observation available")
        return observed[-1]

    def generate_daily_plan(self, as_of_date: str | None = None) -> dict[str, Any]:
        """Close-only virtual action plan for the next observed session.

        This never books fills or changes portfolio_state.json. An observed next
        session and its execution prices are required before virtual settlement.
        """
        self.load_data()
        state = self.load_state()
        dt = self._resolve_date(as_of_date)
        if state.last_signal_date and state.last_signal_date > str(dt.date()):
            raise ValueError("Portfolio state is newer than evaluation date")
        observed = self.breadth.dropna().loc[:dt].sort_index()
        if not np.isfinite(observed.to_numpy()).all():
            raise ValueError("Market breadth contains non-finite values")

        # Daily defense with two completed sessions above 50% for re-entry.
        defense = False
        recovery_streak = 0
        recovered_on = None
        for session_date, breadth_value in observed.items():
            if breadth_value < 40.0:
                defense, recovery_streak = True, 0
            elif defense:
                recovery_streak = recovery_streak + 1 if breadth_value >= 50.0 else 0
                if recovery_streak >= 2:
                    defense, recovery_streak = False, 0
                    recovered_on = session_date

        prev_rebalance = pd.Timestamp(state.last_rebalance_date) if state.last_rebalance_date else None
        if prev_rebalance is not None and prev_rebalance > dt:
            raise ValueError("Portfolio rebalance date is after evaluation date")
        sessions_since = (int((observed.index > prev_rebalance).sum()) if prev_rebalance is not None
                          else None)
        due = prev_rebalance is None or sessions_since >= 20 or recovered_on == dt
        rankings = compute_factor_rankings(self.universe, dt)
        if not rankings.empty:
            rankings = rankings.loc[np.isfinite(rankings["composite_score"])].sort_values(
                ["composite_score", "code"], ascending=[False, True]
            ).reset_index(drop=True)
        rank_map = {code: index + 1 for index, code in enumerate(rankings["code"])} if not rankings.empty else {}
        top_codes = [] if rankings.empty else rankings.head(10)["code"].tolist()

        prices = {}
        unavailable = []
        for code in state.positions:
            frame = self.universe.get(code)
            if frame is None or dt not in frame.index or frame.at[dt, "Volume"] <= 0:
                unavailable.append(code)
            else:
                prices[code] = float(frame.at[dt, "Close"])
        if unavailable:
            # Missing held prices must not be replaced with entry prices or zero.
            return {"as_of": str(dt.date()), "status": "DATA_INCOMPLETE",
                    "unavailable_positions": sorted(unavailable), "actions": [],
                    "note": "No virtual actions until held-position prices are observed."}

        equity = state.cash + sum(p["qty"] * prices[code] for code, p in state.positions.items())
        if not np.isfinite(equity) or equity <= 0:
            raise ValueError("Invalid portfolio equity")
        actions = []
        sell_codes = set()
        if defense:
            for code, pos in sorted(state.positions.items()):
                actions.append({"action": "SELL", "code": code, "qty": pos["qty"],
                                "reason": "BREADTH_BELOW_40"})
                sell_codes.add(code)
        else:
            for code, pos in sorted(state.positions.items()):
                frame = self.universe[code]
                hist = frame.loc[:dt]
                close = prices[code]
                sma120 = float(hist["Close"].rolling(120).mean().iloc[-1])
                stop = float(pos.get("stop_price", pos["entry_price"] * 0.85))
                if float(frame.at[dt, "Low"]) <= stop:
                    reason = "STOP_OBSERVED_REVIEW_FILL"
                elif due and code not in top_codes:
                    reason = "TOP_10_DROPOUT"
                elif rank_map.get(code, float("inf")) > 30 or close < sma120:
                    reason = "EARLY_EXIT_RANK_OR_SMA120"
                else:
                    reason = None
                if reason:
                    actions.append({"action": "SELL", "code": code, "qty": pos["qty"],
                                    "reason": reason})
                    sell_codes.add(code)

            if due:
                for code, pos in sorted(state.positions.items()):
                    if code in sell_codes:
                        continue
                    max_qty = int(np.floor(equity * .25 / prices[code]))
                    trim_qty = pos["qty"] - max_qty
                    if trim_qty > 0:
                        actions.append({"action": "TRIM", "code": code, "qty": trim_qty,
                                        "reason": "POSITION_CAP_25_PCT_ESTIMATE"})
                    else:
                        actions.append({"action": "HOLD", "code": code, "qty": pos["qty"],
                                        "reason": "TOP_10_SURVIVOR"})
            if due or len(state.positions) < 10:
                for code in top_codes:
                    if code not in state.positions and code not in sell_codes:
                        close = float(self.universe[code].at[dt, "Close"])
                        estimated_shares = int(np.floor((equity / 10.0) / (close * 1.00115)))
                        if estimated_shares > 0:
                            actions.append({"action": "BUY_CANDIDATE", "code": code,
                                            "target_value": equity / 10.0,
                                            "estimated_shares_at_close": estimated_shares,
                                            "reason": "TOP_10_VACANCY"})
                        else:
                            actions.append({"action": "SKIP", "code": code,
                                            "reason": "ONE_SHARE_EXCEEDS_SLOT_BUDGET"})

        return {"as_of": str(dt.date()), "execution": "next observed session; virtual fills only",
                "status": "DEFENSE" if defense else "ACTIVE", "breadth_pct": float(observed.iloc[-1]),
                "recovery_streak": recovery_streak, "rebalance_due": due,
                "sessions_since_rebalance": sessions_since, "equity_at_close": equity,
                "universe_stocks": len(self.universe), "data_last_date": str(self.breadth.dropna().index[-1].date()),
                "eligible": len(rankings), "top_codes": top_codes, "actions": actions,
                "note": "TRIM quantity uses the known close; recompute against the observed open before settlement."}

    def settle_daily_plan(self, as_of_date: str) -> dict[str, Any]:
        """Settle a virtual close signal after the next entire session is stored."""
        self.load_data()
        state = self.load_state()
        signal_date = self._resolve_date(as_of_date)
        signal_key = str(signal_date.date())
        if state.last_signal_date == signal_key:
            return {"as_of": signal_key, "already_applied": True}
        if state.last_signal_date and state.last_signal_date > signal_key:
            raise ValueError("Cannot settle an older signal after a newer one")
        observed = self.breadth.dropna().sort_index().index
        next_index = observed.searchsorted(signal_date, side="right")
        if next_index >= len(observed):
            raise ValueError("Next completed daily session is not stored; virtual fill deferred")
        execution_date = observed[next_index]
        plan = self.generate_daily_plan(signal_key)
        if plan["status"] == "DATA_INCOMPLETE":
            raise ValueError("Held-position price missing at signal close")
        actions = plan["actions"]
        required_codes = set(state.positions)
        for code in required_codes:
            frame = self.universe.get(code)
            if frame is None or execution_date not in frame.index or frame.at[execution_date, "Volume"] <= 0:
                raise ValueError(f"Held position lacks an executable daily bar: {code}")

        # Use only cash that existed before sells to fund same-open buys.
        starting_buy_cash = state.cash
        fills = []
        for action in actions:
            if action["action"] not in {"SELL", "TRIM"}:
                continue
            code = action["code"]
            pos = state.positions[code]
            open_price = float(self.universe[code].at[execution_date, "Open"])
            qty = pos["qty"]
            if action["action"] == "TRIM":
                equity_open = state.cash + sum(
                    p["qty"] * float(self.universe[c].at[execution_date, "Open"])
                    for c, p in state.positions.items()
                    if execution_date in self.universe[c].index
                )
                qty = max(0, pos["qty"] - int(np.floor(equity_open * .25 / open_price)))
            if qty <= 0:
                continue
            proceeds = qty * open_price * (1 - .001) * (1 - .00015 - .002)
            state.cash += proceeds
            pos["qty"] -= qty
            if pos["qty"] == 0:
                del state.positions[code]
            fills.append({"action": action["action"], "code": code, "qty": qty,
                          "price": open_price, "net_cash": proceeds, "reason": action["reason"]})

        for action in actions:
            if action["action"] != "BUY_CANDIDATE" or starting_buy_cash <= 0:
                continue
            code = action["code"]
            frame = self.universe[code]
            if code in state.positions or execution_date not in frame.index or frame.at[execution_date, "Volume"] <= 0:
                continue
            open_price = float(frame.at[execution_date, "Open"])
            execution_price = open_price * 1.001
            qty = int(min(action["target_value"], starting_buy_cash) / (execution_price * 1.00015))
            if qty <= 0:
                continue
            cost = qty * execution_price * 1.00015
            state.cash -= cost
            starting_buy_cash -= cost
            state.positions[code] = {"qty": qty, "entry_price": execution_price,
                                     "entry_date": str(execution_date.date()),
                                     "stop_price": execution_price * .85, "days_held": 0}
            fills.append({"action": "BUY", "code": code, "qty": qty,
                          "price": open_price, "net_cash": -cost, "reason": action["reason"]})

        # A stored full session permits a same-day virtual airbag check.
        for code, pos in list(state.positions.items()):
            frame = self.universe.get(code)
            if frame is None or execution_date not in frame.index or frame.at[execution_date, "Volume"] <= 0:
                continue
            row = frame.loc[execution_date]
            stop_price = float(pos.get("stop_price", pos["entry_price"] * .85))
            if float(row["Low"]) <= stop_price:
                exit_price = min(float(row["Open"]), stop_price)
                proceeds = pos["qty"] * exit_price * .999 * (1 - .00015 - .002)
                state.cash += proceeds
                fills.append({"action": "STOP", "code": code, "qty": pos["qty"],
                              "price": exit_price, "net_cash": proceeds, "reason": "AIRBAG_15_PCT"})
                del state.positions[code]

        if plan["rebalance_due"]:
            state.last_rebalance_date = signal_key
        state.last_signal_date = signal_key
        result = {"as_of": signal_key, "execution_date": str(execution_date.date()),
                  "virtual_fills": fills, "cash": state.cash, "positions": len(state.positions)}
        state.history.append(result)
        self.save_state(state)
        return result

    def get_status(self, as_of_date: str | None = None) -> dict[str, Any]:
        self.load_data()
        state = self.load_state()

        dt = self._resolve_date(as_of_date)

        b_val = float(self.breadth.loc[dt])
        regime_status = "BULL / NORMAL (>= 40%)" if b_val >= 40.0 else "BEAR / DEFENSE (< 40%)"

        pos_summary = []
        total_pos_val = 0.0

        for code, p in state.positions.items():
            df = self.universe.get(code)
            current_px = p["entry_price"]
            if df is not None and dt in df.index:
                current_px = float(df.loc[dt, "Close"])

            market_val = p["qty"] * current_px
            total_pos_val += market_val
            ret_pct = (current_px / p["entry_price"] - 1.0) * 100.0
            stop_px = p.get("stop_price", p["entry_price"] * 0.85)
            stop_dist = (current_px / stop_px - 1.0) * 100.0

            profile = self.rag.get_stock_profile(code)
            pos_summary.append({
                "code": code,
                "name": profile.get("name", code),
                "sector": profile.get("sector", "-"),
                "qty": p["qty"],
                "entry_price": p["entry_price"],
                "current_price": current_px,
                "stop_price": stop_px,
                "market_val": market_val,
                "ret_pct": ret_pct,
                "stop_dist": stop_dist,
                "entry_date": p.get("entry_date", "-"),
                "days_held": p.get("days_held", 0)
            })


        total_equity = state.cash + total_pos_val
        total_pnl = total_equity - state.initial_capital
        total_ret = (total_equity / state.initial_capital - 1.0) * 100.0

        return {
            "date": dt.strftime("%Y-%m-%d"),
            "breadth": b_val,
            "regime": regime_status,
            "cash": state.cash,
            "pos_val": total_pos_val,
            "total_equity": total_equity,
            "total_ret": total_ret,
            "total_pnl": total_pnl,
            "last_rebalance": state.last_rebalance_date,
            "positions": pos_summary
        }

    def check_catastrophic_stops(self, as_of_date: str | None = None) -> list[dict]:
        """
        Daily Airbag Check: Checks if any active holding fell below -15% stop price.
        Returns list of emergency stop triggers.
        """
        self.load_data()
        state = self.load_state()

        dt = self._resolve_date(as_of_date)

        triggers = []
        for code, p in state.positions.items():
            df = self.universe.get(code)
            if df is None or dt not in df.index:
                continue
            bar = df.loc[dt]
            stop_px = p.get("stop_price", p["entry_price"] * 0.85)

            if bar["Low"] <= stop_px:
                exit_price = min(bar["Open"], stop_px)
                triggers.append({
                    "code": code,
                    "reason": "AIRBAG_STOP_LOSS (-15%)",
                    "entry_price": p["entry_price"],
                    "stop_price": stop_px,
                    "exit_price": exit_price,
                    "qty": p["qty"],
                    "loss_pct": (exit_price / p["entry_price"] - 1.0) * 100.0
                })
        return triggers

    def generate_rebalance_orders(
        self,
        as_of_date: str | None = None,
        max_slots: int = 10,
        stop_pct: float = 0.15,
        min_breadth: float = 40.0,
        dry_run: bool = True
    ) -> dict[str, Any]:
        """
        Executes Mode 2 monthly rebalancing logic.
        Outputs SELL, BUY, and HOLD action orders.
        """
        self.load_data()
        state = self.load_state()

        all_dates = sorted(list(self.breadth.dropna().index))
        dt = self._resolve_date(as_of_date)

        # Find next trading date for execution price reference
        dt_idx = all_dates.index(dt) if dt in all_dates else len(all_dates) - 1
        exec_dt = all_dates[dt_idx + 1] if dt_idx + 1 < len(all_dates) else dt

        b_val = float(self.breadth.loc[dt])
        regime_ok = b_val >= min_breadth

        # Re-running an applied rebalance for the same session must be a no-op.
        # This protects the JSON ledger from duplicate history entries after a
        # retried GitHub/manual invocation.
        if not dry_run and state.last_rebalance_date == dt.strftime("%Y-%m-%d"):
            held_value = 0.0
            for code, position in state.positions.items():
                frame = self.universe.get(code)
                price = position.get("entry_price", 0.0)
                if frame is not None and dt in frame.index:
                    price = float(frame.loc[dt, "Close"])
                held_value += position.get("qty", 0) * price
            return {
                "date": dt.strftime("%Y-%m-%d"),
                "exec_date": dt.strftime("%Y-%m-%d"),
                "breadth": b_val,
                "regime": "ALREADY APPLIED",
                "total_equity": state.cash + held_value,
                "sell_orders": [],
                "buy_orders": [],
                "hold_orders": [],
                "projected_cash": state.cash,
                "already_applied": True,
            }

        sell_orders = []
        buy_orders = []
        hold_orders = []

        total_pos_val = 0.0
        current_positions = state.positions.copy()

        # Valuation of current positions
        for code, p in current_positions.items():
            df = self.universe.get(code)
            px = p["entry_price"]
            if df is not None and dt in df.index:
                px = float(df.loc[dt, "Close"])
            total_pos_val += p["qty"] * px

        total_equity = state.cash + total_pos_val

        if not regime_ok:
            # DEFENSE MODE: Liquidate all to cash
            for code, p in current_positions.items():
                df = self.universe.get(code)
                exec_px = p["entry_price"]
                if df is not None and exec_dt in df.index:
                    exec_px = float(df.loc[exec_dt, "Open"])
                sell_orders.append({
                    "code": code,
                    "action": "SELL_ALL (REGIME DEFENSE)",
                    "qty": p["qty"],
                    "est_price": exec_px,
                    "est_proceeds": p["qty"] * exec_px * 0.9975  # after fee/tax
                })
            action_plan = {
                "date": dt.strftime("%Y-%m-%d"),
                "exec_date": exec_dt.strftime("%Y-%m-%d"),
                "breadth": b_val,
                "regime": "BEAR / DEFENSE (< 40%) -> 100% CASH",
                "total_equity": total_equity,
                "sell_orders": sell_orders,
                "buy_orders": [],
                "hold_orders": []
            }
            if not dry_run:
                # Apply liquidation
                for order in sell_orders:
                    state.cash += order["est_proceeds"]
                state.positions.clear()
                state.last_rebalance_date = dt.strftime("%Y-%m-%d")
                self.save_state(state)
            return action_plan

        # NORMAL MODE (Breadth >= 40%): Select Top 10 Factor Stocks
        rankings = compute_factor_rankings(self.universe, dt)
        if rankings.empty:
            top_codes = []
        else:
            top_codes = list(rankings.head(max_slots)["code"])

        # 1. Check existing positions: Keep if still in top_codes, otherwise SELL
        kept_codes = set()
        cash_after_sells = state.cash

        for code, p in current_positions.items():
            df = self.universe.get(code)
            exec_px = p["entry_price"]
            if df is not None and exec_dt in df.index:
                exec_px = float(df.loc[exec_dt, "Open"])

            if code in top_codes:
                kept_codes.add(code)
                hold_orders.append({
                    "code": code,
                    "action": "HOLD",
                    "qty": p["qty"],
                    "current_price": exec_px,
                    "stop_price": p.get("stop_price", p["entry_price"] * (1.0 - stop_pct)),
                    "pnl_pct": (exec_px / p["entry_price"] - 1.0) * 100.0
                })
            else:
                net_proceeds = p["qty"] * exec_px * 0.9975
                cash_after_sells += net_proceeds
                sell_orders.append({
                    "code": code,
                    "action": "SELL (RANK DROP)",
                    "qty": p["qty"],
                    "est_price": exec_px,
                    "est_proceeds": net_proceeds,
                    "pnl_pct": (exec_px / p["entry_price"] - 1.0) * 100.0
                })

        # 2. Check empty slots to BUY
        needed_codes = [c for c in top_codes if c not in kept_codes]
        available_slots = max_slots - len(kept_codes)
        target_slot_budget = total_equity / float(max_slots)

        for code in needed_codes[:available_slots]:
            df = self.universe.get(code)
            if df is None or exec_dt not in df.index:
                continue
            exec_px = float(df.loc[exec_dt, "Open"])
            if exec_px <= 0:
                continue

            target_qty = int(target_slot_budget / (exec_px * 1.0015))  # buffer for fee/slip
            if target_qty <= 0:
                continue
            est_cost = target_qty * exec_px * 1.0015
            if est_cost > cash_after_sells:
                target_qty = int(cash_after_sells / (exec_px * 1.0015))
                est_cost = target_qty * exec_px * 1.0015

            if target_qty > 0:
                cash_after_sells -= est_cost
                buy_orders.append({
                    "code": code,
                    "action": "BUY_NEW",
                    "qty": target_qty,
                    "est_price": exec_px,
                    "est_cost": est_cost,
                    "target_stop_price": exec_px * (1.0 - stop_pct)
                })

        rag_theses = []
        if not rankings.empty:
            for _, r in rankings.head(max_slots).iterrows():
                c_code = r["code"]
                t = self.rag.generate_thesis(
                    code=c_code,
                    mom60_5=float(r["mom60_5"]),
                    risk_adj_mom=float(r["risk_adj_mom"]),
                    cmf20=float(r["cmf20"]),
                    composite_score=float(r["composite_score"]),
                    price=float(r["close"])
                )
                rag_theses.append(t)
        thesis_table_md = self.rag.render_markdown_table(rag_theses) if rag_theses else ""

        action_plan = {
            "date": dt.strftime("%Y-%m-%d"),
            "exec_date": exec_dt.strftime("%Y-%m-%d"),
            "breadth": b_val,
            "regime": "BULL / NORMAL (>= 40%)",
            "total_equity": total_equity,
            "target_slot_budget": target_slot_budget,
            "sell_orders": sell_orders,
            "buy_orders": buy_orders,
            "hold_orders": hold_orders,
            "projected_cash": cash_after_sells,
            "rag_theses": rag_theses,
            "thesis_table_md": thesis_table_md
        }

        if not dry_run:
            # Apply state transition
            # Remove sold positions
            for s in sell_orders:
                if s["code"] in state.positions:
                    del state.positions[s["code"]]
            # Add bought positions
            for b in buy_orders:
                state.positions[b["code"]] = {
                    "qty": b["qty"],
                    "entry_price": b["est_price"],
                    "entry_date": exec_dt.strftime("%Y-%m-%d"),
                    "stop_price": b["target_stop_price"],
                    "days_held": 0
                }
            state.cash = cash_after_sells
            state.last_rebalance_date = dt.strftime("%Y-%m-%d")
            state.history.append({
                "date": dt.strftime("%Y-%m-%d"),
                "equity": total_equity,
                "sells": len(sell_orders),
                "buys": len(buy_orders),
                "holds": len(hold_orders)
            })
            self.save_state(state)

        return action_plan


def print_status_report(status: dict):
    print("\n" + "=" * 95)
    print("             PORTFOLIO STATUS REPORT (MODE 2 INSTITUTIONAL)")
    print("=" * 95)
    print(f"As of Date         : {status['date']}")
    print(f"Market Breadth     : {status['breadth']:.1f}% ({status['regime']})")
    print(f"Total Equity       : {status['total_equity']:>15,.0f} KRW")
    print(f"Available Cash     : {status['cash']:>15,.0f} KRW")
    print(f"Invested Value     : {status['pos_val']:>15,.0f} KRW")
    print(f"Total Return (PnL) : {status['total_ret']:>14.2f}% ({status['total_pnl']:+,.0f} KRW)")
    print(f"Last Rebalance     : {status['last_rebalance'] or 'Initial'}")
    print("-" * 95)
    print(f"{'Code':<8} | {'Name':<10} | {'Sector':<12} | {'Qty':<5} | {'Entry':<10} | {'Current':<10} | {'PnL (%)':<8} | {'Airbag (-15%)':<18}")
    print("-" * 95)
    if not status["positions"]:
        print("  (No active positions held. Currently 100% Cash)")
    else:
        for p in status["positions"]:
            name = p.get('name', p['code'])[:8]
            sector = p.get('sector', '-')[:10]
            print(f"{p['code']:<8} | {name:<10} | {sector:<12} | {p['qty']:<5} | {p['entry_price']:>9,.0f} | {p['current_price']:>9,.0f} | {p['ret_pct']:>6.1f}% | {p['stop_price']:>9,.0f} ({p['stop_dist']:>4.1f}% safe)")
    print("=" * 95)


def print_rebalance_plan(plan: dict):
    print("\n" + "=" * 85)
    print("             MONTHLY REBALANCE ACTION PLAN (MODE 2)")
    print("=" * 85)
    print(f"Evaluation Date    : {plan['date']} Close")
    print(f"Execution Date     : {plan['exec_date']} Open")
    print(f"Market Breadth     : {plan['breadth']:.1f}% ({plan['regime']})")
    print(f"Total Portfolio    : {plan['total_equity']:>15,.0f} KRW")
    print(f"Target Per Slot    : {plan.get('target_slot_budget', 0):>15,.0f} KRW (10% Allocation)")
    print("-" * 85)

    if plan.get("thesis_table_md"):
        print("\n[📊 퀀트 팩터 랭킹 & RAG 알고리즘 판정이유]")
        print(plan["thesis_table_md"])
        print("-" * 85)

    print("\n[1] SELL ORDERS (전량 매도 - 순위 하락 / 레짐 방어)")
    if not plan["sell_orders"]:
        print("  - 매도 대상 없음")
    else:
        for s in plan["sell_orders"]:
            print(f"  * [SELL] {s['code']}: {s['qty']}주 @ 약 {s['est_price']:,.0f}원 (예상 회수금: {s['est_proceeds']:,.0f}원, 수익률: {s.get('pnl_pct', 0.0):+.1f}%)")

    print("\n[2] HOLD POSITIONS (보유 유지 - 10위권 유지)")
    if not plan["hold_orders"]:
        print("  - 유지 종목 없음")
    else:
        for h in plan["hold_orders"]:
            print(f"  * [HOLD] {h['code']}: {h['qty']}주 (현재가: {h['current_price']:,.0f}원, 손익: {h['pnl_pct']:+.1f}%, 에어백: {h['stop_price']:,.0f}원)")

    print("\n[3] BUY ORDERS (신규 매수 - 팩터 상위 진입)")
    if not plan["buy_orders"]:
        print("  - 신규 매수 없음")
    else:
        for b in plan["buy_orders"]:
            print(f"  * [BUY ] {b['code']}: {b['qty']}주 @ 약 {b['est_price']:,.0f}원 (예상 투입: {b['est_cost']:,.0f}원, 에어백 스톱: {b['target_stop_price']:,.0f}원)")

    print("-" * 85)
    print(f"Projected Cash After Rebalance: {plan.get('projected_cash', 0):>15,.0f} KRW")
    print("=" * 85)


def main():
    parser = argparse.ArgumentParser(description="Mode 2 Pure Quant Portfolio Manager with RAG Thesis")
    parser.add_argument("command", choices=["init", "status", "check-stops", "rebalance", "daily-plan", "explore"], help="Operation command")
    parser.add_argument("--cash", type=float, default=1_000_000.0, help="Initial cash for init")
    parser.add_argument("--date", type=str, default=None, help="Target evaluation date (YYYY-MM-DD)")
    parser.add_argument("--apply", action="store_true", help="Settle virtual fills only after the next full daily bar exists")
    parser.add_argument("--md-out", type=str, default=None, help="Optional output path to write markdown table")
    parser.add_argument("--state", type=str, default=DEFAULT_STATE_FILE, help="Path to portfolio_state.json")
    parser.add_argument("--data", type=str, default=DEFAULT_DATA_DIR, help="Path to daily data directory")
    parser.add_argument("--eps-data", type=str, default=None, help="T-094 quarterly EPS snapshot for read-only explore")


    args = parser.parse_args()
    mgr = PortfolioManager(state_file=args.state, data_dir=args.data)

    if args.command == "init":
        mgr.initialize_portfolio(args.cash)
    elif args.command == "explore":
        if args.apply:
            parser.error("explore is read-only; --apply is unavailable")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from research.dynamic_sue_rag_explorer import DEFAULT_EPS_PATH, explore, render_markdown
        try:
            result = explore(mgr, as_of_date=args.date, eps_path=args.eps_data or DEFAULT_EPS_PATH)
        except ValueError as exc:
            parser.error(str(exc))
        report = render_markdown(result)
        print(report)
        if args.md_out:
            out_p = Path(args.md_out)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(report, encoding="utf-8")
            print(f"[OK] Read-only explorer report written to {out_p}")
    elif args.command == "daily-plan":
        if args.apply:
            if not args.date:
                parser.error("daily-plan --apply requires an explicit signal --date")
            try:
                result = mgr.settle_daily_plan(as_of_date=args.date)
            except ValueError as exc:
                parser.error(str(exc))
        else:
            result = mgr.generate_daily_plan(as_of_date=args.date)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "status":
        st = mgr.get_status(as_of_date=args.date)
        print_status_report(st)
    elif args.command == "check-stops":
        stops = mgr.check_catastrophic_stops(as_of_date=args.date)
        print("\n=== DAILY AIRBAG STOP CHECK (-15%) ===")
        if not stops:
            print("[OK] No active holdings hit the -15% catastrophic stop.")
        else:
            for s in stops:
                print(f"[ALERT] {s['code']} triggered {s['reason']}: Exit @ {s['exit_price']:,.0f} (Loss: {s['loss_pct']:.1f}%)")
    elif args.command == "rebalance":
        if args.apply:
            mgr.load_data()
            dt = mgr._resolve_date(args.date)
            if mgr.breadth.dropna().index.searchsorted(dt, side="right") >= len(mgr.breadth.dropna()):
                parser.error("No next completed session; legacy rebalance cannot virtually apply a same-day open")
        dry_run = not args.apply
        plan = mgr.generate_rebalance_orders(as_of_date=args.date, dry_run=dry_run)
        print_rebalance_plan(plan)
        if args.md_out and plan.get("thesis_table_md"):
            out_p = Path(args.md_out)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            with open(out_p, "w", encoding="utf-8") as f:
                f.write(plan["thesis_table_md"])
            print(f"\n[OK] RAG markdown table written to {out_p}")
        if dry_run:
            print("\n* This was a DRY-RUN. Use '--apply' flag to persist changes into portfolio_state.json.")
        else:
            print("\n* State file successfully updated with new portfolio positions!")



if __name__ == "__main__":
    main()
