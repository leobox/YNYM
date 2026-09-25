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
        # Match the research runner's shift(5) / shift(60) definition.
        c_t5 = float(c.iloc[-6])
        c_t60 = float(c.iloc[-61])
        if c_t60 <= 0:
            continue
        mom60_5 = (c_t5 - c_t60) / c_t60

        # 2. Risk-Adjusted Momentum (Sharpe Momentum)
        daily_ret = c.pct_change().dropna()
        if len(daily_ret) < 60:
            continue
        vol60 = float(daily_ret.iloc[-60:].std() * np.sqrt(252))
        if vol60 <= 0 or not np.isfinite(vol60):
            continue
        risk_adj_mom = mom60_5 / vol60

        # 3. Chaikin Money Flow (CMF 20)
        hl = (h.iloc[-20:] - l.iloc[-20:]).replace(0, np.nan)
        if hl.isna().any():
            continue
        mf_mult = ((c.iloc[-20:] - l.iloc[-20:]) - (h.iloc[-20:] - c.iloc[-20:])) / hl
        mf_vol = mf_mult * v.iloc[-20:]
        sum_vol = float(v.iloc[-20:].sum())
        if sum_vol <= 0:
            continue
        cmf20 = float(mf_vol.sum() / sum_vol)
        if not np.isfinite(cmf20):
            continue

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
    def __init__(self, state_file: str = DEFAULT_STATE_FILE, data_dir: str = DEFAULT_DATA_DIR):
        self.state_file = Path(state_file)
        self.data_dir = data_dir
        self.universe: dict[str, pd.DataFrame] | None = None
        self.breadth: pd.Series | None = None

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
            if df is None or dt not in df.index:
                raise ValueError(f"{code}: held position has no daily bar for {dt.date()}")
            current_px = float(df.loc[dt, "Close"])

            market_val = p["qty"] * current_px
            total_pos_val += market_val
            ret_pct = (current_px / p["entry_price"] - 1.0) * 100.0
            stop_px = p.get("stop_price", p["entry_price"] * 0.85)
            stop_dist = (current_px / stop_px - 1.0) * 100.0

            pos_summary.append({
                "code": code,
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
                raise ValueError(f"{code}: held position has no daily bar for {dt.date()}")
            bar = df.loc[dt]
            if not np.isfinite(float(bar["Volume"])) or float(bar["Volume"]) <= 0:
                raise ValueError(f"{code}: held position has no tradable volume for {dt.date()}")
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

        # At the latest close the next open is unknown. Only a historical
        # session with an observed next bar may be applied to a paper ledger.
        dt_idx = all_dates.index(dt)
        exec_dt = (all_dates[dt_idx + 1]
                   if not dry_run and dt_idx + 1 < len(all_dates) else None)
        if not dry_run and exec_dt is None and state.last_rebalance_date != dt.strftime("%Y-%m-%d"):
            raise ValueError("Cannot apply: next session open has not been observed")
        execution_label = exec_dt.strftime("%Y-%m-%d") if exec_dt is not None else "NEXT_SESSION"
        reference_basis = "next_session_open" if exec_dt is not None else "signal_close_estimate"

        b_val = float(self.breadth.loc[dt])
        regime_ok = b_val >= min_breadth

        # Re-running an applied rebalance for the same session must be a no-op.
        # This protects the JSON ledger from duplicate history entries after a
        # retried GitHub/manual invocation.
        if not dry_run and state.last_rebalance_date == dt.strftime("%Y-%m-%d"):
            held_value = 0.0
            for code, position in state.positions.items():
                frame = self.universe.get(code)
                if frame is None or dt not in frame.index:
                    raise ValueError(f"{code}: held position has no daily bar for {dt.date()}")
                price = float(frame.loc[dt, "Close"])
                held_value += position.get("qty", 0) * price
            return {
                "date": dt.strftime("%Y-%m-%d"),
                "exec_date": execution_label,
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
            if df is None or dt not in df.index:
                raise ValueError(f"{code}: held position has no daily bar for {dt.date()}")
            px = float(df.loc[dt, "Close"])
            total_pos_val += p["qty"] * px

        total_equity = state.cash + total_pos_val

        if not regime_ok:
            # DEFENSE MODE: Liquidate all to cash
            for code, p in current_positions.items():
                df = self.universe.get(code)
                if exec_dt is not None and exec_dt in df.index:
                    exec_px = float(df.loc[exec_dt, "Open"])
                elif exec_dt is None:
                    exec_px = float(df.loc[dt, "Close"])
                else:
                    raise ValueError(f"{code}: no next-session open for paper liquidation")
                sell_orders.append({
                    "code": code,
                    "action": "SELL_ALL (REGIME DEFENSE)",
                    "qty": p["qty"],
                    "est_price": exec_px,
                    "est_proceeds": p["qty"] * exec_px * 0.9975  # after fee/tax
                })
            action_plan = {
                "date": dt.strftime("%Y-%m-%d"),
                "exec_date": execution_label,
                "price_basis": reference_basis,
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
            if exec_dt is not None and exec_dt in df.index:
                exec_px = float(df.loc[exec_dt, "Open"])
            elif exec_dt is None:
                exec_px = float(df.loc[dt, "Close"])
            else:
                raise ValueError(f"{code}: no next-session open for paper rebalance")

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
            if df is None or (exec_dt is not None and exec_dt not in df.index):
                continue
            exec_px = float(df.loc[exec_dt, "Open"] if exec_dt is not None else df.loc[dt, "Close"])
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

        action_plan = {
            "date": dt.strftime("%Y-%m-%d"),
            "exec_date": execution_label,
            "price_basis": reference_basis,
            "breadth": b_val,
            "regime": "BULL / NORMAL (>= 40%)",
            "total_equity": total_equity,
            "target_slot_budget": target_slot_budget,
            "sell_orders": sell_orders,
            "buy_orders": buy_orders,
            "hold_orders": hold_orders,
            "projected_cash": cash_after_sells
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
    print("\n" + "=" * 75)
    print("             PORTFOLIO STATUS REPORT (MODE 2 INSTITUTIONAL)")
    print("=" * 75)
    print(f"As of Date         : {status['date']}")
    print(f"Market Breadth     : {status['breadth']:.1f}% ({status['regime']})")
    print(f"Total Equity       : {status['total_equity']:>15,.0f} KRW")
    print(f"Available Cash     : {status['cash']:>15,.0f} KRW")
    print(f"Invested Value     : {status['pos_val']:>15,.0f} KRW")
    print(f"Total Return (PnL) : {status['total_ret']:>14.2f}% ({status['total_pnl']:+,.0f} KRW)")
    print(f"Last Rebalance     : {status['last_rebalance'] or 'Initial'}")
    print("-" * 75)
    print(f"{'Code':<8} | {'Qty':<6} | {'Entry Price':<12} | {'Current Price':<13} | {'PnL (%)':<8} | {'Airbag Stop (-15%)':<18}")
    print("-" * 75)
    if not status["positions"]:
        print("  (No active positions held. Currently 100% Cash)")
    else:
        for p in status["positions"]:
            print(f"{p['code']:<8} | {p['qty']:<6} | {p['entry_price']:>10,.0f} | {p['current_price']:>11,.0f} | {p['ret_pct']:>6.1f}% | {p['stop_price']:>10,.0f} ({p['stop_dist']:>4.1f}% safe)")
    print("=" * 75)


def print_rebalance_plan(plan: dict):
    print("\n" + "=" * 75)
    print("             MONTHLY REBALANCE ACTION PLAN (MODE 2)")
    print("=" * 75)
    print(f"Evaluation Date    : {plan['date']} Close")
    print(f"Execution Reference: {plan['exec_date']} ({plan.get('price_basis', 'not applicable')})")
    print(f"Market Breadth     : {plan['breadth']:.1f}% ({plan['regime']})")
    print(f"Total Portfolio    : {plan['total_equity']:>15,.0f} KRW")
    print(f"Target Per Slot    : {plan.get('target_slot_budget', 0):>15,.0f} KRW (10% Allocation)")
    print("-" * 75)

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

    print("-" * 75)
    print(f"Projected Cash After Rebalance: {plan.get('projected_cash', 0):>15,.0f} KRW")
    print("=" * 75)


def main():
    parser = argparse.ArgumentParser(description="Mode 2 Pure Quant Portfolio Manager")
    parser.add_argument("command", choices=["init", "status", "check-stops", "rebalance"], help="Operation command")
    parser.add_argument("--cash", type=float, default=1_000_000.0, help="Initial cash for init")
    parser.add_argument("--date", type=str, default=None, help="Target evaluation date (YYYY-MM-DD)")
    parser.add_argument("--apply", action="store_true", help="Apply rebalance to state file (not dry-run)")
    parser.add_argument("--state", type=str, default=DEFAULT_STATE_FILE, help="Path to portfolio_state.json")
    parser.add_argument("--data", type=str, default=DEFAULT_DATA_DIR, help="Path to daily data directory")

    args = parser.parse_args()
    mgr = PortfolioManager(state_file=args.state, data_dir=args.data)

    if args.command == "init":
        mgr.initialize_portfolio(args.cash)
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
        dry_run = not args.apply
        plan = mgr.generate_rebalance_orders(as_of_date=args.date, dry_run=dry_run)
        print_rebalance_plan(plan)
        if dry_run:
            print("\n* This was a DRY-RUN. Use '--apply' flag to persist changes into portfolio_state.json.")
        else:
            print("\n* State file successfully updated with new portfolio positions!")


if __name__ == "__main__":
    main()
