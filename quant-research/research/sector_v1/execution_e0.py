"""Deterministic E0 open-approximation portfolio & accounting engine (§§5, 6, 7, 8, 9, 10).

Research simulation only — strictly ZERO broker/account/order execution APIs.

Sealed E0 implementation invariants (§6, §10):
1. Candidate ranking, integer target shares `q_target`, and cash reservation budgets are fixed
   at signal session `t` Close using ONLY settled cash and prices `<= t` (never peeking at `Open_{t+1}`).
2. Slot capacity strictly enforces `len(open_positions) + len(pending_entry_orders) <= max_slots` (3).
   A position queued for exit still occupies its slot until its exit fill completes, preventing
   both >3 simultaneous holdings during exit trading halts and pre-use of pending exit proceeds.
3. Same-bar re-entry is prohibited: any `(region, sector)` or `code` liquidated at session `t` Open
   is excluded from candidate selection at session `t` Close (`청산한 봉의 재진입 금지`).
4. Pre-committed cash budget rule (`사전 현금 예산 배분`):
   - In priority order (`rank 1, 2, ...`), `q_i` is sized by `min(25% NAV, 0.5% NAV / 2*ATR20)`
     and shrunk at signal close if needed so `sum(base_cost_i) <= available_cash_t`.
   - Up to KRX +30% daily limit buffer (`1.30 * base_cost_i`) is reserved per order when settled
     cash permits; when `available_cash_t < sum(1.30 * base_cost_i)`, surplus cash above
     `sum(base_cost_i)` is partitioned pro-rata to `base_cost_i` so `sum(reserved_budget_i) <= available_cash_t`.
   - This guarantees zero margin borrowing (`cash >= 0` always) while reflecting genuine upward
     and downward opening gaps in `E0` without mimicking an unverified opening limit order (`M1`).
   - If `Open_{exec}` buy outflow exceeds `reserved_budget_i`, the order is rejected as
     `BUDGET_EXCEEDED_AT_OPEN` with no retroactive `q` resizing and no backup stock substitution.
5. Trading halts / missing bars / zero volume on scheduled execution open:
   - Entry order: cancelled on scheduled execution date as `UNFILLED_HALTED_OR_NO_TRADE_AT_OPEN`
     (never silently carried to a later session's price).
   - Exit order: kept in `PENDING_EXIT_HALTED`, logging each delayed session, and filled at the
     first resumed tradable session Open.
6. Period-end open positions remain open and marked-to-market in `daily_ledger`; they are never
   force-closed at `eval_end` to inflate `closed_episodes`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from . import sha256_file
from .models import (
    CostSchedule,
    FrozenSectorMapping,
    PITConstituentTable,
    TradingCalendar,
)
from .signals import (
    DailySignalSnapshot,
    evaluate_daily_snapshot,
    pit_split_adjusted_ohlc,
)

KRX_DAILY_LIMIT_BUFFER = 1.30
ACCOUNTING_TOLERANCE_KRW = 1e-5


def krx_tick_size(price: float, instrument_kind: str) -> float:
    """Sealed KRX tick-size rule (§6 line 90: 호가 단위 봉인).

    - Domestic-listed Equity ETF (`EQUITY_ETF`): 1 KRW below 2,000 KRW; 5 KRW at/above 2,000 KRW.
    - KRX Stocks (`KOSPI_STOCK`, `KOSDAQ_STOCK`, 2023+ unified schedule):
      <2,000: 1 | <5,000: 5 | <20,000: 10 | <50,000: 50 | <200,000: 100 | <500,000: 500 | >=500,000: 1,000.
    """
    if price <= 0.0 or not np.isfinite(price):
        raise ValueError(f"INVALID_PRICE_FOR_TICK:{price}")
    if instrument_kind == "EQUITY_ETF":
        return 1.0 if price < 2_000.0 else 5.0
    if price < 2_000.0:
        return 1.0
    if price < 5_000.0:
        return 5.0
    if price < 20_000.0:
        return 10.0
    if price < 50_000.0:
        return 50.0
    if price < 200_000.0:
        return 100.0
    if price < 500_000.0:
        return 500.0
    return 1_000.0


def round_down_to_krx_tick(price: float, instrument_kind: str) -> float:
    """Round a positive KRW price down to the nearest valid KRX tick."""
    tick = krx_tick_size(price, instrument_kind)
    return float(math.floor((price + 1e-9) / tick) * tick)


@dataclass
class OpenPosition:
    episode_id: int
    code: str
    name: str
    region: str
    sector: str
    instrument_kind: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    qty: int
    entry_raw_price_orig: float
    entry_exec_price_orig: float
    entry_raw_price_adj: float
    entry_exec_price_adj: float
    entry_signal_atr20_adj: float
    initial_cost_basis: float
    remaining_cost_basis: float
    entry_fee: float
    last_split_factor: float
    last_close: float
    last_mark_date: pd.Timestamp
    last_mark_status: str = "ENTRY_OPEN_PROVISIONAL"
    mark_is_stale: bool = False
    stale_sessions: int = 0
    total_missing_bar_sessions: int = 0
    total_halted_sessions: int = 0
    delisted_unsettled_sessions: int = 0
    below_sma20_streak: int = 0
    dividends_received: float = 0.0
    fractional_cash_received: float = 0.0
    fractional_realized_pnl: float = 0.0
    exit_delay_halts: int = 0


@dataclass
class PendingEntryOrder:
    order_id: int
    priority_rank: int
    signal_date: pd.Timestamp
    scheduled_exec_date: pd.Timestamp
    code: str
    name: str
    region: str
    sector: str
    instrument_kind: str
    qty: int
    signal_close: float
    signal_sma20: float
    signal_atr20: float
    signal_split_factor: float
    base_estimated_cost: float
    reserved_budget: float
    candidate_meta: dict = field(default_factory=dict)


@dataclass
class PendingExitOrder:
    order_id: int
    episode_id: int
    code: str
    signal_date: pd.Timestamp
    scheduled_exec_date: pd.Timestamp
    reason: str
    status: str = "QUEUED"  # QUEUED | PENDING_EXIT_HALTED


@dataclass(frozen=True)
class ClosedEpisode:
    episode_id: int
    code: str
    name: str
    region: str
    sector: str
    instrument_kind: str
    signal_date: str
    entry_date: str
    exit_signal_date: str
    exit_date: str
    holding_sessions: int
    exit_delay_halts: int
    exit_reason: str
    entry_qty_post_adjust: int
    entry_raw_open_adj: float
    exit_raw_open: float
    initial_cost_basis: float
    net_exit_proceeds: float
    dividends_received: float
    fractional_cash_received: float
    entry_fee: float
    exit_fee: float
    exit_tax: float
    net_pnl_krw: float
    net_return_pct: float


@dataclass
class E0PortfolioResult:
    policy_name: str
    initial_capital: float
    cost_multiplier: float
    execution_delay_sessions: int
    daily_ledger: pd.DataFrame
    closed_episodes: list[ClosedEpisode]
    open_positions_at_end: list[dict]
    order_events: list[dict]
    observation_summary: dict
    total_buy_turnover_krw: float
    total_sell_turnover_krw: float


def compute_target_qty(
    nav_t: float,
    signal_close: float,
    signal_atr20: float,
    max_slot_weight: float = 0.25,
    slot_risk_budget: float = 0.005,
) -> int:
    """Compute integer share count `q_target` (§5):

    `q_target = floor(min(NAV_t * 0.25 / signal_close, NAV_t * 0.005 / (2 * signal_atr20)))`
    """
    if not np.isfinite([nav_t, signal_close, signal_atr20]).all():
        return 0
    if nav_t <= 0.0 or signal_close <= 0.0 or signal_atr20 <= 0.0:
        return 0

    weight_budget = nav_t * max_slot_weight
    risk_budget = nav_t * slot_risk_budget
    two_atr = 2.0 * signal_atr20

    raw_q = min(weight_budget / signal_close, risk_budget / two_atr)
    if not np.isfinite(raw_q) or raw_q <= 0.0:
        return 0

    q = max(0, int(math.floor(raw_q)))
    if (
        (q + 1) * signal_close <= weight_budget
        and (q + 1) * two_atr <= risk_budget
    ):
        q += 1
    while q > 0 and (q * signal_close > weight_budget + 1e-9 or q * two_atr > risk_budget + 1e-9):
        q -= 1
    return q


def _max_affordable_qty_at_close(
    desired_qty: int,
    signal_close: float,
    available_cash: float,
    cost_schedule: CostSchedule,
    cost_multiplier: float,
) -> int:
    """Deterministically shrink `q` at signal night `t` if `available_cash` is below estimated close cost."""
    if desired_qty <= 0 or available_cash <= 0.0:
        return 0
    est = cost_schedule.compute_buy_cost(signal_close, desired_qty, cost_multiplier)["total_cash_outflow"]
    if est <= available_cash:
        return desired_qty

    unit_cost = cost_schedule.compute_buy_cost(signal_close, 1, cost_multiplier)["total_cash_outflow"]
    if unit_cost <= 0.0 or available_cash < unit_cost:
        return 0

    q = min(desired_qty, int(math.floor(available_cash / unit_cost)))
    while q > 0 and (
        cost_schedule.compute_buy_cost(signal_close, q, cost_multiplier)["total_cash_outflow"]
        > available_cash
    ):
        q -= 1
    return q


def allocate_precommitted_entry_budgets(
    sized_items: Sequence[dict],
    available_cash: float,
    limit_buffer: float = KRX_DAILY_LIMIT_BUFFER,
) -> list[float]:
    """Deterministically partition `available_cash` into per-order `reserved_budget` on night `t`.

    Requires `sum(item["base_cost"]) <= available_cash`.
    Guarantees `base_cost_i <= reserved_budget_i <= limit_buffer * base_cost_i` and
    `sum(reserved_budget_i) <= available_cash`.
    """
    if not sized_items:
        return []
    base_costs = [float(it["base_cost"]) for it in sized_items]
    total_base = sum(base_costs)
    if total_base > available_cash + 1e-9:
        raise ValueError("BASE_ENTRY_COSTS_EXCEED_AVAILABLE_CASH")

    max_caps = [bc * limit_buffer for bc in base_costs]
    if available_cash >= sum(max_caps):
        return max_caps

    surplus = max(0.0, available_cash - total_base)
    budgets: list[float] = []
    allocated = 0.0
    for idx, bc in enumerate(base_costs):
        if idx == len(base_costs) - 1:
            rem = max(bc, available_cash - allocated)
            budgets.append(min(max_caps[idx], rem))
        else:
            share = surplus * (bc / total_base) if total_base > 0.0 else 0.0
            b_i = min(max_caps[idx], bc + share)
            budgets.append(b_i)
            allocated += b_i
    return budgets


def verify_accounting_conservation(daily_ledger: pd.DataFrame, initial_capital: float) -> None:
    """Raise ValueError if any daily row violates cash + MTM == initial + realized + div + unrealized."""
    if daily_ledger.empty:
        return
    lhs = daily_ledger["cash"] + daily_ledger["market_value"]
    rhs = (
        float(initial_capital)
        + daily_ledger["realized_price_pnl_cum"]
        + daily_ledger["dividends_cum"]
        + daily_ledger["unrealized_pnl"]
    )
    max_err = float(np.max(np.abs(lhs - rhs)))
    if not np.isfinite(max_err) or max_err > ACCOUNTING_TOLERANCE_KRW:
        raise ValueError(f"ACCOUNTING_CONSERVATION_VIOLATION:max_err={max_err}")
    if (daily_ledger["cash"] < -ACCOUNTING_TOLERANCE_KRW).any():
        raise ValueError("NEGATIVE_CASH_BORROWING_VIOLATION")


CandidateSelectorFn = Callable[
    [
        pd.Timestamp,
        DailySignalSnapshot,
        int,
        set[tuple[str, str]],
        set[str],
    ],
    tuple[list[dict], dict[str, str]],
]


def build_precomputed_close_sma20(
    sessions: Sequence[pd.Timestamp],
    codes: Sequence[str],
    bars_by_code: Mapping[str, pd.DataFrame],
) -> dict[str, dict[pd.Timestamp, tuple[float, float]]]:
    """Precompute PIT-exact `(close_now, sma20_now)` per `(code, session)` for batch runs (e.g. Random-1000)."""
    table: dict[str, dict[pd.Timestamp, tuple[float, float]]] = {}
    for code in sorted(set(codes)):
        df_asset = bars_by_code.get(code)
        if df_asset is None:
            continue
        per_day: dict[pd.Timestamp, tuple[float, float]] = {}
        for t in sessions:
            sliced = df_asset.loc[:t]
            if sliced.empty or sliced.index[-1] != t or bool(sliced["Halted"].iloc[-1]):
                continue
            completed = sliced.loc[~sliced["Halted"]] if "Halted" in sliced.columns else sliced
            adj = pit_split_adjusted_ohlc(completed)
            close_now = float(adj["Close"].iloc[-1])
            sma20_now = float(adj["Close"].iloc[-20:].mean()) if len(adj) >= 20 else float("nan")
            per_day[t] = (close_now, sma20_now)
        table[code] = per_day
    return table


def run_e0_simulation(
    policy_name: str,
    calendar: TradingCalendar,
    mapping: FrozenSectorMapping,
    bars_by_code: Mapping[str, pd.DataFrame],
    cost_schedule: CostSchedule,
    eval_start: pd.Timestamp | str,
    eval_end: pd.Timestamp | str,
    candidate_selector: CandidateSelectorFn,
    initial_capital: float = 30_000_000.0,
    max_slots: int = 3,
    max_slot_weight: float = 0.25,
    slot_risk_budget: float = 0.005,
    cost_multiplier: float = 1.0,
    execution_delay_sessions: int = 0,
    precomputed_snapshots: Mapping[pd.Timestamp, DailySignalSnapshot] | None = None,
    precomputed_close_sma20: Mapping[str, Mapping[pd.Timestamp, tuple[float, float]]] | None = None,
    collect_observation_summary: bool = True,
) -> E0PortfolioResult:
    """Run deterministic E0 daily close-to-next-open simulation over `[eval_start, eval_end]`."""
    cost_schedule.validate()
    if initial_capital <= 0.0:
        raise ValueError("INITIAL_CAPITAL_MUST_BE_POSITIVE")
    if max_slots < 1:
        raise ValueError("MAX_SLOTS_MUST_BE_POSITIVE")
    if execution_delay_sessions < 0:
        raise ValueError("EXECUTION_DELAY_MUST_BE_NONNEGATIVE")

    sessions = calendar.sessions_in_range(eval_start, eval_end)
    if not sessions:
        raise ValueError("EVALUATION_WINDOW_HAS_NO_SCHEDULED_SESSIONS")
    session_index_map = {d: i for i, d in enumerate(sessions)}
    final_eval_ts = sessions[-1]

    cash = float(initial_capital)
    realized_price_pnl_cum = 0.0
    dividends_cum = 0.0
    total_buy_turnover = 0.0
    total_sell_turnover = 0.0

    open_positions: list[OpenPosition] = []
    pending_entries: list[PendingEntryOrder] = []
    pending_exits: dict[int, PendingExitOrder] = {}  # episode_id -> PendingExitOrder
    closed_episodes: list[ClosedEpisode] = []
    order_events: list[dict] = []
    ledger_rows: list[dict] = []

    next_episode_id = 1
    next_order_id = 1
    prev_nav = float(initial_capital)

    obs_total_rows = 0
    obs_bear_regime_rows = 0
    transition_counts: dict[str, int] = {}
    region_halt_counts: dict[str, int] = {}
    selector_region_notes: dict[str, str] = {}
    spec_by_code = {spec.code: spec for spec in mapping.representatives.values()}

    for t in sessions:
        t_str = t.strftime("%Y-%m-%d")
        exited_today_sectors: set[tuple[str, str]] = set()
        exited_today_codes: set[str] = set()

        # ------------------------------------------------------------------
        # 1A. Morning Corporate Actions (splits & ex-date cash dividends)
        # ------------------------------------------------------------------
        surviving_positions: list[OpenPosition] = []
        for pos in open_positions:
            df_code = bars_by_code[pos.code]
            has_row_today = t in df_code.index
            sf_today = float(df_code.loc[t, "SplitFactor"]) if has_row_today else pos.last_split_factor

            if abs(sf_today - pos.last_split_factor) > 1e-12:
                ratio = sf_today / pos.last_split_factor
                exact_shares = pos.qty * ratio
                new_qty = int(math.floor(exact_shares + 1e-12))
                frac = max(0.0, exact_shares - new_qty)

                pos.entry_raw_price_adj /= ratio
                pos.entry_exec_price_adj /= ratio
                pos.entry_signal_atr20_adj /= ratio
                pos.last_close /= ratio
                pos.last_split_factor = sf_today

                if frac > 1e-9 and exact_shares > 0.0:
                    ref_px = (
                        float(df_code.loc[t, "Open"])
                        if (has_row_today and not bool(df_code.loc[t, "Halted"]))
                        else pos.last_close
                    )
                    cil_cash = frac * ref_px
                    basis_share = pos.remaining_cost_basis * (frac / exact_shares)
                    pos.remaining_cost_basis -= basis_share
                    realized_delta = cil_cash - basis_share

                    cash += cil_cash
                    realized_price_pnl_cum += realized_delta
                    pos.fractional_cash_received += cil_cash
                    pos.fractional_realized_pnl += realized_delta

                    order_events.append(
                        {
                            "date": t_str,
                            "event": "CORPORATE_ACTION_FRACTIONAL_CASH_IN_LIEU",
                            "episode_id": pos.episode_id,
                            "code": pos.code,
                            "split_ratio": ratio,
                            "new_qty": new_qty,
                            "cash_in_lieu": cil_cash,
                        }
                    )

                pos.qty = new_qty
                order_events.append(
                    {
                        "date": t_str,
                        "event": "CORPORATE_ACTION_SPLIT_ADJUST",
                        "episode_id": pos.episode_id,
                        "code": pos.code,
                        "split_ratio": ratio,
                        "adjusted_qty": pos.qty,
                        "entry_raw_price_adj": pos.entry_raw_price_adj,
                        "entry_signal_atr20_adj": pos.entry_signal_atr20_adj,
                    }
                )

                if pos.qty <= 0:
                    pending_exits.pop(pos.episode_id, None)
                    ep_net = (
                        pos.dividends_received
                        + pos.fractional_cash_received
                        - pos.initial_cost_basis
                    )
                    hold_days = max(
                        1, session_index_map[t] - session_index_map.get(pos.entry_date, 0)
                    )
                    closed_episodes.append(
                        ClosedEpisode(
                            episode_id=pos.episode_id,
                            code=pos.code,
                            name=pos.name,
                            region=pos.region,
                            sector=pos.sector,
                            instrument_kind=pos.instrument_kind,
                            signal_date=pos.signal_date.strftime("%Y-%m-%d"),
                            entry_date=pos.entry_date.strftime("%Y-%m-%d"),
                            exit_signal_date=t_str,
                            exit_date=t_str,
                            holding_sessions=hold_days,
                            exit_delay_halts=pos.exit_delay_halts,
                            exit_reason="REVERSE_SPLIT_ZERO_SHARES",
                            entry_qty_post_adjust=0,
                            entry_raw_open_adj=pos.entry_raw_price_adj,
                            exit_raw_open=pos.last_close,
                            initial_cost_basis=pos.initial_cost_basis,
                            net_exit_proceeds=0.0,
                            dividends_received=pos.dividends_received,
                            fractional_cash_received=pos.fractional_cash_received,
                            entry_fee=pos.entry_fee,
                            exit_fee=0.0,
                            exit_tax=0.0,
                            net_pnl_krw=ep_net,
                            net_return_pct=(ep_net / pos.initial_cost_basis)
                            if pos.initial_cost_basis > 0
                            else 0.0,
                        )
                    )
                    exited_today_sectors.add((pos.region, pos.sector))
                    exited_today_codes.add(pos.code)
                    continue

            if has_row_today:
                div_ps = float(df_code.loc[t, "CashDividend"])
                if div_ps > 0.0 and pos.qty > 0:
                    div_amt = div_ps * pos.qty
                    cash += div_amt
                    dividends_cum += div_amt
                    pos.dividends_received += div_amt
                    order_events.append(
                        {
                            "date": t_str,
                            "event": "DIVIDEND_CREDIT",
                            "episode_id": pos.episode_id,
                            "code": pos.code,
                            "qty": pos.qty,
                            "dividend_per_share": div_ps,
                            "dividend_cash": div_amt,
                        }
                    )

            surviving_positions.append(pos)
        open_positions = surviving_positions

        # ------------------------------------------------------------------
        # 1B. Scheduled Exit Executions at Session `t` Open
        #     Note: `same_morning_exit_inflow` is held in escrow until Step 1C finishes
        #     so same-timestamp buy orders NEVER use morning `t` exit proceeds (§6 line 90).
        # ------------------------------------------------------------------
        same_morning_exit_inflow = 0.0
        still_open: list[OpenPosition] = []
        for pos in open_positions:
            p_exit = pending_exits.get(pos.episode_id)
            if p_exit is None or p_exit.scheduled_exec_date > t:
                still_open.append(pos)
                continue

            df_code = bars_by_code[pos.code]
            tradable_open = (
                t in df_code.index
                and not bool(df_code.loc[t, "Halted"])
                and float(df_code.loc[t, "Volume"]) > 0.0
            )
            if not tradable_open:
                p_exit.status = "PENDING_EXIT_HALTED"
                pos.exit_delay_halts += 1
                still_open.append(pos)
                order_events.append(
                    {
                        "date": t_str,
                        "event": "EXIT_DELAYED_HALT_OR_NO_TRADE",
                        "order_id": p_exit.order_id,
                        "episode_id": pos.episode_id,
                        "code": pos.code,
                        "reason": p_exit.reason,
                        "exit_delay_halts": pos.exit_delay_halts,
                    }
                )
                continue

            raw_open = float(df_code.loc[t, "Open"])
            sell_info = cost_schedule.compute_sell_proceeds(
                raw_price=raw_open,
                qty=pos.qty,
                instrument_kind=pos.instrument_kind,
                trade_date=t,
                cost_multiplier=cost_multiplier,
            )
            net_inflow = sell_info["net_cash_inflow"]
            same_morning_exit_inflow += net_inflow
            total_sell_turnover += sell_info["gross_amount"]

            price_pnl_delta = net_inflow - pos.remaining_cost_basis
            realized_price_pnl_cum += price_pnl_delta

            total_ep_net_pnl = (
                net_inflow
                + pos.dividends_received
                + pos.fractional_cash_received
                - pos.initial_cost_basis
            )
            hold_sessions = max(
                1, session_index_map[t] - session_index_map.get(pos.entry_date, 0)
            )
            closed_episodes.append(
                ClosedEpisode(
                    episode_id=pos.episode_id,
                    code=pos.code,
                    name=pos.name,
                    region=pos.region,
                    sector=pos.sector,
                    instrument_kind=pos.instrument_kind,
                    signal_date=pos.signal_date.strftime("%Y-%m-%d"),
                    entry_date=pos.entry_date.strftime("%Y-%m-%d"),
                    exit_signal_date=p_exit.signal_date.strftime("%Y-%m-%d"),
                    exit_date=t_str,
                    holding_sessions=hold_sessions,
                    exit_delay_halts=pos.exit_delay_halts,
                    exit_reason=p_exit.reason,
                    entry_qty_post_adjust=pos.qty,
                    entry_raw_open_adj=pos.entry_raw_price_adj,
                    exit_raw_open=raw_open,
                    initial_cost_basis=pos.initial_cost_basis,
                    net_exit_proceeds=net_inflow,
                    dividends_received=pos.dividends_received,
                    fractional_cash_received=pos.fractional_cash_received,
                    entry_fee=pos.entry_fee,
                    exit_fee=sell_info["fee"],
                    exit_tax=sell_info["tax"],
                    net_pnl_krw=total_ep_net_pnl,
                    net_return_pct=(total_ep_net_pnl / pos.initial_cost_basis)
                    if pos.initial_cost_basis > 0.0
                    else 0.0,
                )
            )
            pending_exits.pop(pos.episode_id, None)
            exited_today_sectors.add((pos.region, pos.sector))
            exited_today_codes.add(pos.code)
            order_events.append(
                {
                    "date": t_str,
                    "event": "EXIT_FILLED",
                    "order_id": p_exit.order_id,
                    "episode_id": pos.episode_id,
                    "code": pos.code,
                    "qty": pos.qty,
                    "raw_open": raw_open,
                    "exec_price": sell_info["exec_price"],
                    "fee": sell_info["fee"],
                    "tax": sell_info["tax"],
                    "net_cash_inflow": net_inflow,
                    "exit_reason": p_exit.reason,
                    "exit_delay_halts": pos.exit_delay_halts,
                }
            )
        open_positions = still_open

        # ------------------------------------------------------------------
        # 1C. Scheduled Entry Executions at Session `t` Open
        # ------------------------------------------------------------------
        due_entries = [o for o in pending_entries if o.scheduled_exec_date == t]
        future_entries = [o for o in pending_entries if o.scheduled_exec_date > t]
        due_entries.sort(key=lambda o: o.priority_rank)

        for order in due_entries:
            df_code = bars_by_code.get(order.code)
            tradable_open = (
                df_code is not None
                and t in df_code.index
                and not bool(df_code.loc[t, "Halted"])
                and float(df_code.loc[t, "Volume"]) > 0.0
            )
            if not tradable_open:
                order_events.append(
                    {
                        "date": t_str,
                        "event": "UNFILLED_HALTED_OR_NO_TRADE_AT_OPEN",
                        "order_id": order.order_id,
                        "signal_date": order.signal_date.strftime("%Y-%m-%d"),
                        "code": order.code,
                        "region": order.region,
                        "sector": order.sector,
                        "qty": order.qty,
                        "reserved_budget": order.reserved_budget,
                    }
                )
                continue

            if len(open_positions) >= max_slots:
                order_events.append(
                    {
                        "date": t_str,
                        "event": "UNFILLED_NO_FREE_SLOT_AT_OPEN",
                        "order_id": order.order_id,
                        "code": order.code,
                    }
                )
                continue

            held_sectors_now = {(p.region, p.sector) for p in open_positions}
            held_codes_now = {p.code for p in open_positions}
            if (
                (order.region, order.sector) in held_sectors_now
                or order.code in held_codes_now
                or (order.region, order.sector) in exited_today_sectors
                or order.code in exited_today_codes
            ):
                order_events.append(
                    {
                        "date": t_str,
                        "event": "UNFILLED_DUPLICATE_OR_SAME_BAR_EXIT_CONFLICT",
                        "order_id": order.order_id,
                        "code": order.code,
                    }
                )
                continue

            sf_exec = float(df_code.loc[t, "SplitFactor"])
            split_ratio = sf_exec / order.signal_split_factor
            exec_qty = int(math.floor(order.qty * split_ratio + 1e-12))
            exec_atr20 = order.signal_atr20 / split_ratio
            if exec_qty <= 0:
                order_events.append(
                    {
                        "date": t_str,
                        "event": "UNFILLED_POST_SPLIT_ZERO_QTY",
                        "order_id": order.order_id,
                        "code": order.code,
                    }
                )
                continue

            raw_open = float(df_code.loc[t, "Open"])
            buy_info = cost_schedule.compute_buy_cost(
                raw_price=raw_open,
                qty=exec_qty,
                cost_multiplier=cost_multiplier,
            )
            total_outflow = buy_info["total_cash_outflow"]

            # Enforce pre-committed cash budget (never borrow, never use same-morning sell proceeds)
            if total_outflow > order.reserved_budget + 1e-9 or total_outflow > cash + 1e-9:
                order_events.append(
                    {
                        "date": t_str,
                        "event": "BUDGET_EXCEEDED_AT_OPEN",
                        "order_id": order.order_id,
                        "signal_date": order.signal_date.strftime("%Y-%m-%d"),
                        "code": order.code,
                        "region": order.region,
                        "sector": order.sector,
                        "qty": exec_qty,
                        "raw_open": raw_open,
                        "required_outflow": total_outflow,
                        "reserved_budget": order.reserved_budget,
                    }
                )
                continue

            cash -= total_outflow
            total_buy_turnover += buy_info["gross_amount"]
            ep_id = next_episode_id
            next_episode_id += 1

            open_positions.append(
                OpenPosition(
                    episode_id=ep_id,
                    code=order.code,
                    name=order.name,
                    region=order.region,
                    sector=order.sector,
                    instrument_kind=order.instrument_kind,
                    signal_date=order.signal_date,
                    entry_date=t,
                    qty=exec_qty,
                    entry_raw_price_orig=raw_open,
                    entry_exec_price_orig=buy_info["exec_price"],
                    entry_raw_price_adj=raw_open,
                    entry_exec_price_adj=buy_info["exec_price"],
                    entry_signal_atr20_adj=exec_atr20,
                    initial_cost_basis=total_outflow,
                    remaining_cost_basis=total_outflow,
                    entry_fee=buy_info["fee"],
                    last_split_factor=sf_exec,
                    last_close=raw_open,
                    last_mark_date=t,
                )
            )
            order_events.append(
                {
                    "date": t_str,
                    "event": "ENTRY_FILLED",
                    "order_id": order.order_id,
                    "episode_id": ep_id,
                    "signal_date": order.signal_date.strftime("%Y-%m-%d"),
                    "code": order.code,
                    "region": order.region,
                    "sector": order.sector,
                    "qty": exec_qty,
                    "raw_open": raw_open,
                    "exec_price": buy_info["exec_price"],
                    "fee": buy_info["fee"],
                    "total_cash_outflow": total_outflow,
                    "reserved_budget": order.reserved_budget,
                }
            )
        pending_entries = future_entries
        cash += same_morning_exit_inflow

        # ------------------------------------------------------------------
        # 2A. End-of-Day Mark-to-Market & Accounting Conservation Check
        # ------------------------------------------------------------------
        market_value = 0.0
        unrealized_pnl = 0.0
        pos_values: list[float] = []
        stale_mark_count = 0
        missing_bar_count = 0
        halted_mark_count = 0
        delisted_unsettled_count = 0

        for pos in open_positions:
            df_code = bars_by_code[pos.code]
            spec_meta = spec_by_code.get(pos.code)
            is_delisted_or_liquidating = bool(
                spec_meta is not None
                and (
                    spec_meta.eligible_status == "LIQUIDATING"
                    or (
                        spec_meta.delisted_date
                        and t >= pd.Timestamp(spec_meta.delisted_date).normalize()
                    )
                )
            )
            if t in df_code.index and not bool(df_code.loc[t, "Halted"]):
                pos.last_close = float(df_code.loc[t, "Close"])
                pos.last_mark_date = t
                pos.last_mark_status = "OBSERVED_CLOSE"
                pos.mark_is_stale = False
                pos.stale_sessions = 0
            else:
                pos.mark_is_stale = True
                pos.stale_sessions += 1
                stale_mark_count += 1
                if is_delisted_or_liquidating:
                    pos.last_mark_status = "CARRIED_DELISTED_UNSETTLED"
                    pos.delisted_unsettled_sessions += 1
                    delisted_unsettled_count += 1
                    ev_type = "HELD_MARK_DELISTED_UNSETTLED_NO_PROCEEDS_DATA"
                    cause = "DELISTED_OR_LIQUIDATING_WITHOUT_SETTLEMENT_BAR"
                elif t in df_code.index and bool(df_code.loc[t, "Halted"]):
                    pos.last_mark_status = "CARRIED_HALTED"
                    pos.total_halted_sessions += 1
                    halted_mark_count += 1
                    ev_type = "HELD_MARK_STALE_HALTED"
                    cause = "EXCHANGE_TRADING_HALT"
                else:
                    pos.last_mark_status = "CARRIED_MISSING_BAR"
                    pos.total_missing_bar_sessions += 1
                    missing_bar_count += 1
                    ev_type = "HELD_MARK_MISSING_BAR"
                    cause = "DATA_PROVIDER_BAR_MISSING"

                order_events.append(
                    {
                        "date": t_str,
                        "event": ev_type,
                        "episode_id": pos.episode_id,
                        "code": pos.code,
                        "region": pos.region,
                        "sector": pos.sector,
                        "carried_mark_close": pos.last_close,
                        "last_mark_date": pos.last_mark_date.strftime("%Y-%m-%d"),
                        "stale_sessions": pos.stale_sessions,
                        "mark_status": pos.last_mark_status,
                        "cause": cause,
                        "exit_signal_evaluation_skipped": bool(pos.episode_id not in pending_exits),
                    }
                )

            pv = pos.qty * pos.last_close
            pos_values.append(pv)
            market_value += pv
            unrealized_pnl += pv - pos.remaining_cost_basis

        nav_t = cash + market_value
        daily_ret = (nav_t / prev_nav) - 1.0 if prev_nav > 0.0 else 0.0
        prev_nav = nav_t

        max_single_wt = (max(pos_values) / nav_t) if (pos_values and nav_t > 0.0) else 0.0
        residual = nav_t - (
            float(initial_capital) + realized_price_pnl_cum + dividends_cum + unrealized_pnl
        )

        ledger_rows.append(
            {
                "date": t,
                "cash": cash,
                "market_value": market_value,
                "nav": nav_t,
                "daily_net_return": daily_ret,
                "gross_exposure_ratio": (market_value / nav_t) if nav_t > 0.0 else 0.0,
                "cash_ratio": (cash / nav_t) if nav_t > 0.0 else 1.0,
                "open_slots": len(open_positions),
                "max_single_weight": max_single_wt,
                "realized_price_pnl_cum": realized_price_pnl_cum,
                "dividends_cum": dividends_cum,
                "unrealized_pnl": unrealized_pnl,
                "conservation_residual": residual,
                "stale_mark_positions": stale_mark_count,
                "missing_bar_positions": missing_bar_count,
                "halted_mark_positions": halted_mark_count,
                "delisted_unsettled_positions": delisted_unsettled_count,
                "nav_all_marks_observed": bool(stale_mark_count == 0),
            }
        )

        # ------------------------------------------------------------------
        # 2B. Daily Signal Snapshot & Exit Condition Evaluation (§3, §7)
        # ------------------------------------------------------------------
        snapshot = (
            precomputed_snapshots[t]
            if (precomputed_snapshots is not None and t in precomputed_snapshots)
            else evaluate_daily_snapshot(t, mapping, bars_by_code)
        )
        if collect_observation_summary:
            for reg, st in snapshot.region_status.items():
                if st != "ACTIVE":
                    key = f"{reg}:{st}"
                    region_halt_counts[key] = region_halt_counts.get(key, 0) + 1
            for obs in snapshot.observations:
                obs_total_rows += 1
                if not obs.regime_bullish:
                    obs_bear_regime_rows += 1
                transition_counts[obs.rs_transition_label] = (
                    transition_counts.get(obs.rs_transition_label, 0) + 1
                )

        for pos in open_positions:
            if pos.episode_id in pending_exits:
                continue

            reasons: list[str] = []
            fast_pair = (
                precomputed_close_sma20.get(pos.code, {}).get(t)
                if precomputed_close_sma20 is not None
                else None
            )
            if fast_pair is not None:
                close_now, sma20_now = fast_pair
                if np.isfinite(sma20_now):
                    if close_now < sma20_now:
                        pos.below_sma20_streak += 1
                    else:
                        pos.below_sma20_streak = 0
                if pos.below_sma20_streak >= 2:
                    reasons.append("BELOW_SMA20_2D")
                stop_line = pos.entry_raw_price_adj - (2.0 * pos.entry_signal_atr20_adj)
                if close_now < stop_line:
                    reasons.append("BELOW_ENTRY_MINUS_2ATR")
            elif precomputed_close_sma20 is None:
                df_asset = bars_by_code[pos.code]
                sliced = df_asset.loc[:t]
                if (
                    not sliced.empty
                    and sliced.index[-1] == t
                    and not bool(sliced["Halted"].iloc[-1])
                ):
                    completed = sliced.loc[~sliced["Halted"]] if "Halted" in sliced.columns else sliced
                    adj = pit_split_adjusted_ohlc(completed)
                    close_now = float(adj["Close"].iloc[-1])
                    if len(adj) >= 20:
                        sma20_now = float(adj["Close"].iloc[-20:].mean())
                        if np.isfinite(sma20_now) and close_now < sma20_now:
                            pos.below_sma20_streak += 1
                        else:
                            pos.below_sma20_streak = 0
                    if pos.below_sma20_streak >= 2:
                        reasons.append("BELOW_SMA20_2D")

                    stop_line = pos.entry_raw_price_adj - (2.0 * pos.entry_signal_atr20_adj)
                    if close_now < stop_line:
                        reasons.append("BELOW_ENTRY_MINUS_2ATR")

            bench_meta = snapshot.benchmark_metrics.get(pos.region)
            if bench_meta is not None and not bool(bench_meta["regime_bullish"]):
                reasons.append("REGIME_BELOW_SMA120")

            if reasons:
                reason_str = "|".join(reasons)
                exec_dt = calendar.next_session_after(t, offset=1 + execution_delay_sessions)
                oid = next_order_id
                next_order_id += 1
                if exec_dt is None or exec_dt > final_eval_ts:
                    order_events.append(
                        {
                            "date": t_str,
                            "event": "EXIT_SIGNAL_PENDING_AFTER_WINDOW_END",
                            "order_id": oid,
                            "episode_id": pos.episode_id,
                            "code": pos.code,
                            "reason": reason_str,
                        }
                    )
                else:
                    pending_exits[pos.episode_id] = PendingExitOrder(
                        order_id=oid,
                        episode_id=pos.episode_id,
                        code=pos.code,
                        signal_date=t,
                        scheduled_exec_date=exec_dt,
                        reason=reason_str,
                    )
                    order_events.append(
                        {
                            "date": t_str,
                            "event": "EXIT_QUEUED",
                            "order_id": oid,
                            "episode_id": pos.episode_id,
                            "code": pos.code,
                            "scheduled_exec_date": exec_dt.strftime("%Y-%m-%d"),
                            "reason": reason_str,
                        }
                    )

        # ------------------------------------------------------------------
        # 2C. Weekly New-Entry Selection & Pre-Committed Cash Allocation (§3, §5, §6)
        # ------------------------------------------------------------------
        if calendar.is_week_end_session(t):
            free_slots = max(0, max_slots - len(open_positions) - len(pending_entries))
            excluded_sectors = (
                {(p.region, p.sector) for p in open_positions}
                | {(o.region, o.sector) for o in pending_entries}
                | exited_today_sectors
            )
            excluded_codes = (
                {p.code for p in open_positions}
                | {o.code for o in pending_entries}
                | exited_today_codes
            )

            candidates, reg_notes = candidate_selector(
                t, snapshot, free_slots, excluded_sectors, excluded_codes
            )
            for rk, rv in reg_notes.items():
                selector_region_notes[rk] = rv

            if not candidates:
                order_events.append(
                    {
                        "date": t_str,
                        "event": "WEEKLY_NO_CANDIDATE",
                        "free_slots": free_slots,
                        "region_status": dict(snapshot.region_status),
                    }
                )
            else:
                already_reserved = sum(o.reserved_budget for o in pending_entries)
                unreserved_cash = max(0.0, cash - already_reserved)
                rem_cash = unreserved_cash

                sized_queue: list[dict] = []
                for rank_idx, cand in enumerate(candidates, start=1):
                    s_close = float(cand["signal_close"])
                    s_atr = float(cand["signal_atr20"])
                    q_raw = compute_target_qty(
                        nav_t=nav_t,
                        signal_close=s_close,
                        signal_atr20=s_atr,
                        max_slot_weight=max_slot_weight,
                        slot_risk_budget=slot_risk_budget,
                    )
                    if q_raw <= 0:
                        order_events.append(
                            {
                                "date": t_str,
                                "event": "ZERO_QTY_DROPPED",
                                "priority_rank": rank_idx,
                                "code": cand["code"],
                                "region": cand["region"],
                                "sector": cand["sector"],
                                "nav": nav_t,
                                "signal_close": s_close,
                                "signal_atr20": s_atr,
                            }
                        )
                        continue

                    q_adj = _max_affordable_qty_at_close(
                        desired_qty=q_raw,
                        signal_close=s_close,
                        available_cash=rem_cash,
                        cost_schedule=cost_schedule,
                        cost_multiplier=cost_multiplier,
                    )
                    if q_adj <= 0:
                        order_events.append(
                            {
                                "date": t_str,
                                "event": "INSUFFICIENT_CASH_AT_SIGNAL",
                                "priority_rank": rank_idx,
                                "code": cand["code"],
                                "desired_qty": q_raw,
                                "available_cash": rem_cash,
                            }
                        )
                        continue

                    base_cost = cost_schedule.compute_buy_cost(
                        raw_price=s_close,
                        qty=q_adj,
                        cost_multiplier=cost_multiplier,
                    )["total_cash_outflow"]
                    rem_cash = max(0.0, rem_cash - base_cost)
                    sized_queue.append(
                        {
                            "priority_rank": rank_idx,
                            "cand": cand,
                            "qty": q_adj,
                            "base_cost": base_cost,
                        }
                    )

                if sized_queue:
                    budgets = allocate_precommitted_entry_budgets(
                        sized_items=sized_queue,
                        available_cash=unreserved_cash,
                    )
                    exec_dt = calendar.next_session_after(t, offset=1 + execution_delay_sessions)
                    for item, budget in zip(sized_queue, budgets):
                        cand = item["cand"]
                        oid = next_order_id
                        next_order_id += 1
                        df_cand = bars_by_code[cand["code"]]
                        sf_sig = float(df_cand.loc[:t, "SplitFactor"].iloc[-1])

                        if exec_dt is None or exec_dt > final_eval_ts:
                            order_events.append(
                                {
                                    "date": t_str,
                                    "event": "PENDING_AFTER_WINDOW_END",
                                    "order_id": oid,
                                    "priority_rank": item["priority_rank"],
                                    "code": cand["code"],
                                    "region": cand["region"],
                                    "sector": cand["sector"],
                                    "qty": item["qty"],
                                    "reserved_budget": budget,
                                }
                            )
                        else:
                            pending_entries.append(
                                PendingEntryOrder(
                                    order_id=oid,
                                    priority_rank=item["priority_rank"],
                                    signal_date=t,
                                    scheduled_exec_date=exec_dt,
                                    code=cand["code"],
                                    name=cand["name"],
                                    region=cand["region"],
                                    sector=cand["sector"],
                                    instrument_kind=cand["instrument_kind"],
                                    qty=item["qty"],
                                    signal_close=float(cand["signal_close"]),
                                    signal_sma20=float(cand["signal_sma20"]),
                                    signal_atr20=float(cand["signal_atr20"]),
                                    signal_split_factor=sf_sig,
                                    base_estimated_cost=item["base_cost"],
                                    reserved_budget=budget,
                                    candidate_meta=dict(cand),
                                )
                            )
                            order_events.append(
                                {
                                    "date": t_str,
                                    "event": "ENTRY_QUEUED",
                                    "order_id": oid,
                                    "priority_rank": item["priority_rank"],
                                    "code": cand["code"],
                                    "region": cand["region"],
                                    "sector": cand["sector"],
                                    "qty": item["qty"],
                                    "scheduled_exec_date": exec_dt.strftime("%Y-%m-%d"),
                                    "base_estimated_cost": item["base_cost"],
                                    "reserved_budget": budget,
                                }
                            )

    ledger_df = pd.DataFrame(ledger_rows).set_index("date")
    ledger_df.index = pd.DatetimeIndex(ledger_df.index)
    verify_accounting_conservation(ledger_df, initial_capital=initial_capital)

    open_end_summary = [
        {
            "episode_id": p.episode_id,
            "code": p.code,
            "name": p.name,
            "region": p.region,
            "sector": p.sector,
            "entry_date": p.entry_date.strftime("%Y-%m-%d"),
            "qty": p.qty,
            "entry_raw_price_adj": p.entry_raw_price_adj,
            "mark_close": p.last_close,
            "last_mark_date": p.last_mark_date.strftime("%Y-%m-%d"),
            "mark_is_stale": bool(p.mark_is_stale),
            "stale_sessions": int(p.stale_sessions),
            "mark_status": p.last_mark_status,
            "total_missing_bar_sessions": int(p.total_missing_bar_sessions),
            "total_halted_sessions": int(p.total_halted_sessions),
            "delisted_unsettled_sessions": int(p.delisted_unsettled_sessions),
            "unsettled_liquidation_note": (
                "MISSING_DELISTING_SETTLEMENT_DATA"
                if p.delisted_unsettled_sessions > 0
                else None
            ),
            "market_value": p.qty * p.last_close,
            "remaining_cost_basis": p.remaining_cost_basis,
            "unrealized_pnl": (p.qty * p.last_close) - p.remaining_cost_basis,
            "dividends_received": p.dividends_received,
        }
        for p in open_positions
    ]

    obs_summary = {
        "total_sector_observations": obs_total_rows,
        "bear_regime_sector_observations": obs_bear_regime_rows,
        "rs_transition_counts": transition_counts,
        "region_non_active_session_counts": region_halt_counts,
        "selector_region_notes": selector_region_notes,
    }

    return E0PortfolioResult(
        policy_name=policy_name,
        initial_capital=float(initial_capital),
        cost_multiplier=float(cost_multiplier),
        execution_delay_sessions=int(execution_delay_sessions),
        daily_ledger=ledger_df,
        closed_episodes=closed_episodes,
        open_positions_at_end=open_end_summary,
        order_events=order_events,
        observation_summary=obs_summary,
        total_buy_turnover_krw=total_buy_turnover,
        total_sell_turnover_krw=total_sell_turnover,
    )
