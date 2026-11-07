"""Frozen input contracts, point-in-time mapping rules, calendar, and cost schedules (§§2, 5, 8, 10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from . import SPEC_VERSION, canonical_json_bytes, sha256_bytes

EXCLUDED_ASSET_CLASSES = frozenset(
    {"LEVERAGED", "INVERSE", "GOLD", "COMMODITY", "BOND", "PARKING"}
)
MIN_HISTORY_BARS = 120
MIN_MEDIAN_TURNOVER_20D_KRW = 1_000_000_000.0
MIN_LOCK_CALENDAR_DAYS = 180  # ~6 months lock (§2: 최초 6개월 대표와 분류 고정)
MIN_REGIONAL_REPRESENTATIVES = 5
MIN_REGIONAL_COVERAGE_RATIO = 0.90


@dataclass(frozen=True)
class RegionalBenchmarkSpec:
    region: str
    code: str
    name: str
    currency: str
    fx_hedged: bool
    asset_class: str = "BROAD_EQUITY"

    def validate(self) -> None:
        if not self.region or self.region.upper() in {"MIXED", "UNKNOWN", "UNCLEAR"}:
            raise ValueError(f"INVALID_BENCHMARK_REGION:{self.region}")
        if not self.code or not self.currency:
            raise ValueError(f"INVALID_BENCHMARK_META:{self.code}")
        if self.asset_class != "BROAD_EQUITY":
            raise ValueError(f"BENCHMARK_NOT_BROAD_EQUITY:{self.code}:{self.asset_class}")


@dataclass(frozen=True)
class SectorETFSpec:
    code: str
    name: str
    region: str
    sector: str
    underlying_index: str
    currency: str
    fx_hedged: bool
    asset_class: str = "EQUITY_SECTOR"
    mapping_reviewed: bool = False
    listed_date: str | None = None
    delisted_date: str | None = None
    eligible_status: str = "ACTIVE"  # ACTIVE | INELIGIBLE | LIQUIDATING

    def is_active_on(self, day: pd.Timestamp) -> bool:
        ts = pd.Timestamp(day).normalize()
        if self.eligible_status != "ACTIVE":
            return False
        if self.listed_date and ts < pd.Timestamp(self.listed_date).normalize():
            return False
        if self.delisted_date and ts >= pd.Timestamp(self.delisted_date).normalize():
            return False
        return True


@dataclass(frozen=True)
class ConstituentItem:
    code: str
    name: str
    market: str  # KOSPI | KOSDAQ | OVERSEAS
    listed_date: str | None = None
    delisted_date: str | None = None
    is_common_stock: bool = True

    def is_active_on(self, day: pd.Timestamp) -> bool:
        if not self.is_common_stock:
            return False
        ts = pd.Timestamp(day).normalize()
        if self.listed_date and ts < pd.Timestamp(self.listed_date).normalize():
            return False
        if self.delisted_date and ts >= pd.Timestamp(self.delisted_date).normalize():
            return False
        return True


@dataclass(frozen=True)
class PITConstituentTable:
    """Point-in-time sector constituent snapshots (§2, §4).

    Never substitutes current constituents backward unless explicitly marked diagnostic.
    """

    snapshots: Mapping[tuple[str, str], Sequence[tuple[pd.Timestamp, tuple[ConstituentItem, ...]]]]
    is_pit_verified: bool = False

    def constituents_as_of(
        self, region: str, sector: str, day: pd.Timestamp
    ) -> tuple[ConstituentItem, ...] | None:
        key = (region, sector)
        entries = self.snapshots.get(key)
        if not entries:
            return None
        ts = pd.Timestamp(day).normalize()
        valid = [items for eff_date, items in entries if pd.Timestamp(eff_date).normalize() <= ts]
        if not valid:
            return None
        return tuple(item for item in valid[-1] if item.is_active_on(ts))

    def covered_sectors_by_region(
        self, as_of: pd.Timestamp | str | None = None
    ) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        ts = pd.Timestamp(as_of).normalize() if as_of is not None else None
        for (region, sector), entries in sorted(self.snapshots.items()):
            if not entries:
                continue
            if ts is not None:
                if self.constituents_as_of(region, sector, ts) is None:
                    continue
            out.setdefault(region, set()).add(sector)
        return out

    def to_serializable_dict(self) -> dict:
        entries_out: list[dict] = []
        for (region, sector), entries in sorted(self.snapshots.items()):
            for eff_date, items in sorted(entries, key=lambda p: pd.Timestamp(p[0]).normalize()):
                entries_out.append(
                    {
                        "region": region,
                        "sector": sector,
                        "effective_date": pd.Timestamp(eff_date).strftime("%Y-%m-%d"),
                        "members": [
                            m.__dict__
                            for m in sorted(items, key=lambda item: (item.code, item.market))
                        ],
                    }
                )
        return {
            "is_pit_verified": bool(self.is_pit_verified),
            "entries": entries_out,
        }

    def sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_serializable_dict()))


class TradingCalendar:
    """Pre-sealed scheduled session calendar (§3, §10).

    Determining whether session `t` is the final scheduled trading session of its week
    uses ONLY the sealed calendar (scheduled holidays/weekends), never peeking at future
    price bars (`df.index`), guaranteeing strict prefix invariance on mid-week truncations.
    """

    def __init__(
        self,
        scheduled_sessions: Sequence[pd.Timestamp | str],
        *,
        calendar_id: str = "UNSPECIFIED_CALENDAR",
        source: str = "unspecified",
        covered_start: pd.Timestamp | str | None = None,
        covered_end: pd.Timestamp | str | None = None,
        is_exchange_verified: bool = False,
    ):
        normalized = sorted({pd.Timestamp(s).normalize() for s in scheduled_sessions})
        if not normalized:
            raise ValueError("TRADING_CALENDAR_EMPTY")
        self._sessions: tuple[pd.Timestamp, ...] = tuple(normalized)
        self._session_set = frozenset(self._sessions)
        self._index_by_day = {d: i for i, d in enumerate(self._sessions)}
        self.calendar_id = str(calendar_id).strip() or "UNSPECIFIED_CALENDAR"
        self.source = str(source).strip() or "unspecified"
        self.covered_start = (
            pd.Timestamp(covered_start).normalize() if covered_start is not None else None
        )
        self.covered_end = (
            pd.Timestamp(covered_end).normalize() if covered_end is not None else None
        )
        self.is_exchange_verified = bool(is_exchange_verified)
        if (
            self.covered_start is not None
            and self.covered_end is not None
            and self.covered_end < self.covered_start
        ):
            raise ValueError("CALENDAR_COVERED_END_BEFORE_START")

        last_by_iso_week: dict[tuple[int, int], pd.Timestamp] = {}
        for d in self._sessions:
            iso = d.isocalendar()
            key = (int(iso.year), int(iso.week))
            if key not in last_by_iso_week or d > last_by_iso_week[key]:
                last_by_iso_week[key] = d
        self._week_end_set = frozenset(last_by_iso_week.values())

    @property
    def sessions(self) -> tuple[pd.Timestamp, ...]:
        return self._sessions

    def is_session(self, day: pd.Timestamp) -> bool:
        return pd.Timestamp(day).normalize() in self._session_set

    def is_week_end_session(self, day: pd.Timestamp) -> bool:
        return pd.Timestamp(day).normalize() in self._week_end_set

    def next_session_after(self, day: pd.Timestamp, offset: int = 1) -> pd.Timestamp | None:
        if offset < 1:
            raise ValueError("offset must be >= 1")
        ts = pd.Timestamp(day).normalize()
        idx = self._index_by_day.get(ts)
        if idx is None:
            # Find first scheduled session strictly after ts
            pos = int(np.searchsorted(self._sessions, ts, side="right"))
            target = pos + offset - 1
        else:
            target = idx + offset
        if 0 <= target < len(self._sessions):
            return self._sessions[target]
        return None

    def sessions_in_range(
        self, start: pd.Timestamp | str | None = None, end: pd.Timestamp | str | None = None
    ) -> tuple[pd.Timestamp, ...]:
        s_ts = pd.Timestamp(start).normalize() if start is not None else self._sessions[0]
        e_ts = pd.Timestamp(end).normalize() if end is not None else self._sessions[-1]
        return tuple(d for d in self._sessions if s_ts <= d <= e_ts)

    def audit_window_coverage(
        self,
        eval_start: pd.Timestamp | str,
        eval_end: pd.Timestamp | str,
        bars_by_code: Mapping[str, pd.DataFrame] | None = None,
        benchmarks: Mapping[str, RegionalBenchmarkSpec] | None = None,
        min_uninspected_months: int = 24,
        max_edge_gap_days: int = 7,
        max_internal_gap_days: int = 14,
        min_sessions_per_full_month: int = 10,
    ) -> dict:
        """Audit sealed calendar coverage bidirectionally against `[eval_start, eval_end]` and price bars (§9, §10).

        - Distinguishes (1) missing price bars on scheduled sessions (`missing_or_halted_sessions`)
          from (2) sparse/incomplete calendar omitting observed trading sessions (`unscheduled_*_bar_sessions`).
        - Monthly/gap heuristics are anomaly detectors only and NEVER override omitted trading sessions
          or declared coverage bounds (`covered_start` / `covered_end`).
        - Approved holidays/weekends with neither scheduled sessions nor completed bars remain valid.
        """
        req_start = pd.Timestamp(eval_start).normalize()
        req_end = pd.Timestamp(eval_end).normalize()
        if req_end < req_start:
            raise ValueError(
                f"EVAL_END_BEFORE_EVAL_START:{req_start.strftime('%Y-%m-%d')}>{req_end.strftime('%Y-%m-%d')}"
            )
        sessions = self.sessions_in_range(req_start, req_end)
        if not sessions:
            raise ValueError("EVALUATION_WINDOW_HAS_NO_SCHEDULED_SESSIONS")

        actual_start = sessions[0]
        actual_end = sessions[-1]
        leading_gap_days = int((actual_start - req_start).days)
        trailing_gap_days = int((req_end - actual_end).days)
        internal_gaps = [
            int((sessions[idx] - sessions[idx - 1]).days) for idx in range(1, len(sessions))
        ]
        max_gap_days = max(internal_gaps) if internal_gaps else 0

        # Anomaly detector: check every full interior calendar month between req_start and req_end
        month_counts: dict[tuple[int, int], int] = {}
        for d in sessions:
            ym = (int(d.year), int(d.month))
            month_counts[ym] = month_counts.get(ym, 0) + 1

        sparse_full_months: list[str] = []
        cursor = pd.Timestamp(year=req_start.year, month=req_start.month, day=1)
        end_month = pd.Timestamp(year=req_end.year, month=req_end.month, day=1)
        while cursor <= end_month:
            ym = (int(cursor.year), int(cursor.month))
            month_end = (cursor + pd.offsets.MonthEnd(1)).normalize()
            is_full_requested_month = req_start <= cursor and req_end >= month_end
            if is_full_requested_month and month_counts.get(ym, 0) < min_sessions_per_full_month:
                sparse_full_months.append(f"{ym[0]:04d}-{ym[1]:02d}:{month_counts.get(ym, 0)}")
            cursor = (cursor + pd.offsets.MonthBegin(1)).normalize()

        calendar_issues: list[str] = []
        if self.covered_start is not None and req_start < self.covered_start:
            calendar_issues.append("REQUESTED_START_BEFORE_CALENDAR_DECLARED_COVERAGE")
        if self.covered_end is not None and req_end > self.covered_end:
            calendar_issues.append("REQUESTED_END_AFTER_CALENDAR_DECLARED_COVERAGE")
        if leading_gap_days > max_edge_gap_days:
            calendar_issues.append("LEADING_EDGE_CALENDAR_GAP_ANOMALY")
        if trailing_gap_days > max_edge_gap_days:
            calendar_issues.append("TRAILING_EDGE_CALENDAR_GAP_ANOMALY")
        if max_gap_days > max_internal_gap_days:
            calendar_issues.append("INTERNAL_CALENDAR_GAP_ANOMALY")
        if sparse_full_months:
            calendar_issues.append("SPARSE_FULL_MONTH_ANOMALY")

        session_set_in_window = frozenset(sessions)
        benchmark_codes: set[str] = set()
        benchmark_coverage: dict[str, dict[str, object]] = {}
        max_benchmark_missing_ratio = 0.0
        unscheduled_benchmark_dates: set[pd.Timestamp] = set()
        unscheduled_instrument_dates: set[pd.Timestamp] = set()

        if bars_by_code is not None and benchmarks is not None:
            for region, bspec in sorted(benchmarks.items()):
                benchmark_codes.add(bspec.code)
                bdf = bars_by_code.get(bspec.code)
                received = 0
                omitted_from_calendar: list[str] = []
                if bdf is not None:
                    for d in sessions:
                        if d in bdf.index and (
                            "Halted" not in bdf.columns or not bool(bdf.loc[d, "Halted"])
                        ):
                            received += 1
                    sub = bdf.loc[req_start:req_end]
                    if not sub.empty:
                        completed_sub = (
                            sub.loc[~sub["Halted"].astype(bool)]
                            if "Halted" in sub.columns
                            else sub
                        )
                        for raw_d in completed_sub.index:
                            nd = pd.Timestamp(raw_d).normalize()
                            if nd not in session_set_in_window:
                                unscheduled_benchmark_dates.add(nd)
                                omitted_from_calendar.append(nd.strftime("%Y-%m-%d"))

                missing = len(sessions) - received
                miss_ratio = float(missing / len(sessions)) if sessions else 1.0
                max_benchmark_missing_ratio = max(max_benchmark_missing_ratio, miss_ratio)
                benchmark_coverage[region] = {
                    "benchmark_code": bspec.code,
                    "scheduled_sessions": len(sessions),
                    "completed_sessions": received,
                    "missing_or_halted_sessions": missing,
                    "missing_ratio": round(miss_ratio, 6),
                    "unscheduled_completed_bar_sessions": len(omitted_from_calendar),
                    "unscheduled_completed_bar_sample": omitted_from_calendar[:10],
                }

        if bars_by_code is not None:
            for code, df in sorted(bars_by_code.items()):
                if code in benchmark_codes or df is None or df.empty:
                    continue
                sub = df.loc[req_start:req_end]
                if sub.empty:
                    continue
                completed_sub = (
                    sub.loc[~sub["Halted"].astype(bool)] if "Halted" in sub.columns else sub
                )
                for raw_d in completed_sub.index:
                    nd = pd.Timestamp(raw_d).normalize()
                    if nd not in session_set_in_window:
                        unscheduled_instrument_dates.add(nd)

        all_unscheduled_bar_dates = sorted(
            unscheduled_benchmark_dates | unscheduled_instrument_dates
        )
        if unscheduled_benchmark_dates:
            calendar_issues.append("CALENDAR_OMITS_OBSERVED_BENCHMARK_SESSIONS")
        if unscheduled_instrument_dates:
            calendar_issues.append("CALENDAR_OMITS_OBSERVED_INSTRUMENT_SESSIONS")

        price_data_issues: list[str] = []
        if max_benchmark_missing_ratio > 0.0:
            price_data_issues.append("BENCHMARK_PRICE_MISSING_OR_HALTED_ON_SCHEDULED_SESSIONS")

        calendar_complete = len(calendar_issues) == 0

        actual_cal_days = max(1, int((actual_end - actual_start).days) + 1)
        requested_cal_days = max(1, int((req_end - req_start).days) + 1)
        actual_years = actual_cal_days / 365.25
        actual_months_approx = actual_cal_days / 30.4375
        requested_months_approx = requested_cal_days / 30.4375

        actual_span_meets_24m = bool(
            actual_end
            >= (
                actual_start
                + pd.DateOffset(months=min_uninspected_months)
                - pd.Timedelta(days=1)
            )
        )
        meets_24m = bool(
            calendar_complete
            and actual_span_meets_24m
            and max_benchmark_missing_ratio <= (1.0 - MIN_REGIONAL_COVERAGE_RATIO)
        )

        return {
            "calendar_id": self.calendar_id,
            "calendar_source": self.source,
            "calendar_is_exchange_verified": self.is_exchange_verified,
            "requested_eval_start": req_start.strftime("%Y-%m-%d"),
            "requested_eval_end": req_end.strftime("%Y-%m-%d"),
            "actual_eval_start": actual_start.strftime("%Y-%m-%d"),
            "actual_eval_end": actual_end.strftime("%Y-%m-%d"),
            "calendar_first_session": self._sessions[0].strftime("%Y-%m-%d"),
            "calendar_last_session": self._sessions[-1].strftime("%Y-%m-%d"),
            "scheduled_sessions": len(sessions),
            "leading_gap_days": leading_gap_days,
            "trailing_gap_days": trailing_gap_days,
            "max_internal_gap_days": max_gap_days,
            "sparse_full_months": sparse_full_months,
            "unscheduled_benchmark_bar_sessions": len(unscheduled_benchmark_dates),
            "unscheduled_instrument_bar_sessions": len(unscheduled_instrument_dates),
            "unscheduled_bar_sessions_total": len(all_unscheduled_bar_dates),
            "unscheduled_bar_dates_sample": [
                d.strftime("%Y-%m-%d") for d in all_unscheduled_bar_dates[:15]
            ],
            "calendar_issues": calendar_issues,
            "price_data_issues": price_data_issues,
            "calendar_complete_for_requested_window": calendar_complete,
            "benchmark_session_coverage": benchmark_coverage,
            "max_benchmark_missing_ratio": round(max_benchmark_missing_ratio, 6),
            "requested_months_approx": round(requested_months_approx, 3),
            "actual_years": actual_years,
            "months_approx": round(actual_months_approx, 3),
            "actual_span_meets_24_months": actual_span_meets_24m,
            "meets_24_months": meets_24m,
        }

    def to_serializable_dict(self) -> dict:
        return {
            "calendar_id": self.calendar_id,
            "source": self.source,
            "covered_start": (
                self.covered_start.strftime("%Y-%m-%d") if self.covered_start is not None else None
            ),
            "covered_end": (
                self.covered_end.strftime("%Y-%m-%d") if self.covered_end is not None else None
            ),
            "is_exchange_verified": self.is_exchange_verified,
            "scheduled_sessions": [d.strftime("%Y-%m-%d") for d in self._sessions],
        }

    def sha256(self) -> str:
        return sha256_bytes(
            canonical_json_bytes([d.strftime("%Y-%m-%d") for d in self._sessions])
        )


@dataclass(frozen=True)
class TaxRuleEntry:
    effective_from: str  # YYYY-MM-DD inclusive
    effective_to: str    # YYYY-MM-DD inclusive
    instrument_kind: str  # EQUITY_ETF | KOSPI_STOCK | KOSDAQ_STOCK
    sell_tax_rate: float


@dataclass(frozen=True)
class CostSchedule:
    """Sealed transaction cost and statutory tax model (§8).

    - Never copies stock transaction tax onto ETFs.
    - Never fixes 0.18% across all historical years when statutory rates changed.
    - Prohibits double-deducting ETF management fee (TER) already embedded in NAV/Close.
    """

    schedule_id: str
    account_type: str  # GENERAL_RESEARCH_SCENARIO | PENSION_RESEARCH_SCENARIO
    buy_fee_rate: float
    sell_fee_rate: float
    entry_slippage_rate: float
    exit_slippage_rate: float
    tax_rules: tuple[TaxRuleEntry, ...]
    etf_ter_extra_deduction_rate: float = 0.0
    is_exact_personal_after_tax: bool = False

    def validate(self) -> None:
        if self.etf_ter_extra_deduction_rate != 0.0:
            raise ValueError("ETF_MANAGEMENT_FEE_DOUBLE_DEDUCTION_PROHIBITED")
        if self.is_exact_personal_after_tax:
            raise ValueError("RESEARCH_COST_SCENARIO_CANNOT_CLAIM_EXACT_PERSONAL_AFTER_TAX")
        for rate in (
            self.buy_fee_rate,
            self.sell_fee_rate,
            self.entry_slippage_rate,
            self.exit_slippage_rate,
        ):
            if not np.isfinite(rate) or rate < 0.0 or rate >= 0.10:
                raise ValueError(f"INVALID_COST_RATE:{rate}")
        for rule in self.tax_rules:
            if not np.isfinite(rule.sell_tax_rate) or rule.sell_tax_rate < 0.0 or rule.sell_tax_rate >= 0.05:
                raise ValueError(f"INVALID_TAX_RATE:{rule}")
            if rule.instrument_kind == "EQUITY_ETF" and rule.sell_tax_rate != 0.0:
                raise ValueError("EQUITY_ETF_MUST_NOT_COPY_STOCK_SELL_TRANSACTION_TAX")

    def sell_tax_rate_for(self, instrument_kind: str, trade_date: pd.Timestamp) -> float:
        if self.account_type == "PENSION_RESEARCH_SCENARIO" and instrument_kind == "EQUITY_ETF":
            return 0.0
        ds = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
        matched = [
            r.sell_tax_rate
            for r in self.tax_rules
            if r.instrument_kind == instrument_kind and r.effective_from <= ds <= r.effective_to
        ]
        if not matched:
            raise KeyError(f"MISSING_SEALED_TAX_RULE:{instrument_kind}:{ds}")
        return float(matched[-1])

    def compute_buy_cost(
        self,
        raw_price: float,
        qty: int,
        cost_multiplier: float = 1.0,
    ) -> dict[str, float]:
        if raw_price <= 0 or qty < 0 or cost_multiplier <= 0:
            raise ValueError("INVALID_BUY_COST_INPUT")
        eff_slip = self.entry_slippage_rate * cost_multiplier
        eff_fee = self.buy_fee_rate * cost_multiplier
        exec_price = float(raw_price) * (1.0 + eff_slip)
        gross = exec_price * qty
        fee = gross * eff_fee
        total_outflow = gross + fee
        return {
            "exec_price": exec_price,
            "gross_amount": gross,
            "fee": fee,
            "tax": 0.0,
            "total_cash_outflow": total_outflow,
        }

    def compute_sell_proceeds(
        self,
        raw_price: float,
        qty: int,
        instrument_kind: str,
        trade_date: pd.Timestamp,
        cost_multiplier: float = 1.0,
    ) -> dict[str, float]:
        if raw_price <= 0 or qty < 0 or cost_multiplier <= 0:
            raise ValueError("INVALID_SELL_COST_INPUT")
        eff_slip = self.exit_slippage_rate * cost_multiplier
        eff_fee = self.sell_fee_rate * cost_multiplier
        eff_tax = self.sell_tax_rate_for(instrument_kind, trade_date) * cost_multiplier
        if eff_slip >= 1.0 or (eff_fee + eff_tax) >= 1.0:
            raise ValueError("COST_MULTIPLIER_EXCEEDS_100_PCT")
        exec_price = float(raw_price) * (1.0 - eff_slip)
        gross = exec_price * qty
        fee = gross * eff_fee
        tax = gross * eff_tax
        net_inflow = gross - fee - tax
        return {
            "exec_price": exec_price,
            "gross_amount": gross,
            "fee": fee,
            "tax": tax,
            "net_cash_inflow": net_inflow,
        }

    def sha256(self) -> str:
        self.validate()
        payload = {
            "schedule_id": self.schedule_id,
            "account_type": self.account_type,
            "buy_fee_rate": self.buy_fee_rate,
            "sell_fee_rate": self.sell_fee_rate,
            "entry_slippage_rate": self.entry_slippage_rate,
            "exit_slippage_rate": self.exit_slippage_rate,
            "etf_ter_extra_deduction_rate": self.etf_ter_extra_deduction_rate,
            "tax_rules": [r.__dict__ for r in self.tax_rules],
        }
        return sha256_bytes(canonical_json_bytes(payload))


def default_krx_research_cost_schedule(
    account_type: str = "GENERAL_RESEARCH_SCENARIO",
) -> CostSchedule:
    """Default pre-sealed statutory KRX research cost table (2015-2027)."""
    rules = (
        TaxRuleEntry("2015-01-01", "2020-12-31", "EQUITY_ETF", 0.0),
        TaxRuleEntry("2021-01-01", "2027-12-31", "EQUITY_ETF", 0.0),
        # KRX statutory securities transaction + agricultural/fishery special tax on KOSPI/KOSDAQ
        TaxRuleEntry("2015-01-01", "2019-06-02", "KOSPI_STOCK", 0.0030),
        TaxRuleEntry("2015-01-01", "2019-06-02", "KOSDAQ_STOCK", 0.0030),
        TaxRuleEntry("2019-06-03", "2020-12-31", "KOSPI_STOCK", 0.0025),
        TaxRuleEntry("2019-06-03", "2020-12-31", "KOSDAQ_STOCK", 0.0025),
        TaxRuleEntry("2021-01-01", "2022-12-31", "KOSPI_STOCK", 0.0023),
        TaxRuleEntry("2021-01-01", "2022-12-31", "KOSDAQ_STOCK", 0.0023),
        TaxRuleEntry("2023-01-01", "2023-12-31", "KOSPI_STOCK", 0.0020),
        TaxRuleEntry("2023-01-01", "2023-12-31", "KOSDAQ_STOCK", 0.0020),
        TaxRuleEntry("2024-01-01", "2024-12-31", "KOSPI_STOCK", 0.0018),
        TaxRuleEntry("2024-01-01", "2024-12-31", "KOSDAQ_STOCK", 0.0018),
        TaxRuleEntry("2025-01-01", "2025-12-31", "KOSPI_STOCK", 0.0015),
        TaxRuleEntry("2025-01-01", "2025-12-31", "KOSDAQ_STOCK", 0.0015),
        TaxRuleEntry("2026-01-01", "2027-12-31", "KOSPI_STOCK", 0.0020),
        TaxRuleEntry("2026-01-01", "2027-12-31", "KOSDAQ_STOCK", 0.0020),
    )
    sched = CostSchedule(
        schedule_id="krx_statutory_v1_2015_2027",
        account_type=account_type,
        buy_fee_rate=0.00015,
        sell_fee_rate=0.00015,
        entry_slippage_rate=0.0005,
        exit_slippage_rate=0.0005,
        tax_rules=rules,
        etf_ter_extra_deduction_rate=0.0,
        is_exact_personal_after_tax=False,
    )
    sched.validate()
    return sched


@dataclass(frozen=True)
class FrozenSectorMapping:
    """Frozen (region, sector) -> representative ETF mapping version (§2)."""

    mapping_version: str
    freeze_date: pd.Timestamp
    valid_from: pd.Timestamp
    valid_until: pd.Timestamp
    benchmarks: Mapping[str, RegionalBenchmarkSpec]
    representatives: Mapping[tuple[str, str], SectorETFSpec]
    pending_mixed_codes: tuple[str, ...] = ()
    excluded_non_equity_codes: tuple[str, ...] = ()
    is_pit_verified: bool = False

    def validate_lock_period(self, require_six_month_lock: bool = True) -> None:
        for bench in self.benchmarks.values():
            bench.validate()
        f_dt = pd.Timestamp(self.freeze_date).normalize()
        v_from = pd.Timestamp(self.valid_from).normalize()
        v_until = pd.Timestamp(self.valid_until).normalize()
        if v_from < f_dt or v_until < v_from:
            raise ValueError("INVALID_MAPPING_VALIDITY_WINDOW")
        if require_six_month_lock and (v_until - v_from).days < MIN_LOCK_CALENDAR_DAYS:
            raise ValueError("MAPPING_LOCK_SHORTER_THAN_6_MONTHS")

        seen_codes: set[str] = set()
        for (region, sector), spec in self.representatives.items():
            if not spec.mapping_reviewed:
                raise ValueError(f"UNREVIEWED_SECTOR_MAPPING_PROHIBITED:{spec.code}")
            if spec.asset_class in EXCLUDED_ASSET_CLASSES or spec.asset_class != "EQUITY_SECTOR":
                raise ValueError(f"EXCLUDED_ASSET_CLASS_IN_SECTOR_EXPERIMENT:{spec.code}:{spec.asset_class}")
            if not spec.underlying_index.strip():
                raise ValueError(f"MISSING_UNDERLYING_INDEX:{spec.code}")
            if region != spec.region or sector != spec.sector:
                raise ValueError(f"MAPPING_KEY_MISMATCH:{region}:{sector}:{spec.code}")
            if region not in self.benchmarks:
                raise ValueError(f"MISSING_REGIONAL_BENCHMARK:{region}")
            bench = self.benchmarks[region]
            if spec.currency != bench.currency or spec.fx_hedged != bench.fx_hedged:
                raise ValueError(f"BENCHMARK_CURRENCY_OR_HEDGE_MISMATCH:{spec.code}:{bench.code}")
            if spec.code in seen_codes:
                raise ValueError(f"DUPLICATE_REPRESENTATIVE_ACROSS_SECTORS:{spec.code}")
            seen_codes.add(spec.code)

    def sha256(self) -> str:
        payload = {
            "mapping_version": self.mapping_version,
            "freeze_date": pd.Timestamp(self.freeze_date).strftime("%Y-%m-%d"),
            "valid_from": pd.Timestamp(self.valid_from).strftime("%Y-%m-%d"),
            "valid_until": pd.Timestamp(self.valid_until).strftime("%Y-%m-%d"),
            "is_pit_verified": self.is_pit_verified,
            "benchmarks": {k: v.__dict__ for k, v in sorted(self.benchmarks.items())},
            "representatives": {
                f"{r}::{s}": spec.__dict__
                for (r, s), spec in sorted(self.representatives.items())
            },
            "pending_mixed_codes": list(self.pending_mixed_codes),
            "excluded_non_equity_codes": list(self.excluded_non_equity_codes),
        }
        return sha256_bytes(canonical_json_bytes(payload))


def clean_and_validate_bars(
    df: pd.DataFrame,
    code: str,
    explicit_column_overrides: Mapping[str, bool] | None = None,
) -> pd.DataFrame:
    """Validate daily bar schema, reject duplicates/invalid OHLC/negative volume (§2, rules.md).

    Expected columns:
    - Open, High, Low, Close, Volume (raw trading price & volume on that date)
    - Optional: TotalReturnClose (distribution-reinvested index; defaults to Close only if absent)
    - Optional: SplitFactor (cumulative share multiplier S(t), default 1.0; e.g., 2.0 on 2-for-1 split)
    - Optional: CashDividend (per-share cash distribution ex-date amount in currency, default 0.0)
    - Optional: Halted (boolean trading halt indicator, default False)

    Records `out.attrs["explicit_columns"]` so defaulted `0/1` corporate-action columns are never
    silently promoted to verified corporate-action / total-return provenance.
    """
    if df.empty:
        raise ValueError(f"EMPTY_BARS:{code}")

    prev_explicit = (
        dict(df.attrs["explicit_columns"])
        if isinstance(df.attrs.get("explicit_columns"), Mapping)
        else None
    )

    def _was_explicit(col_name: str, non_default_check: bool = False) -> bool:
        if explicit_column_overrides is not None and col_name in explicit_column_overrides:
            return bool(explicit_column_overrides[col_name])
        if col_name not in df.columns:
            return False
        if prev_explicit is None or col_name not in prev_explicit:
            return True
        if bool(prev_explicit[col_name]):
            return True
        return bool(non_default_check)

    has_nondefault_sf = (
        bool((df["SplitFactor"].astype(float) != 1.0).any())
        if "SplitFactor" in df.columns
        else False
    )
    has_nondefault_cd = (
        bool((df["CashDividend"].astype(float) != 0.0).any())
        if "CashDividend" in df.columns
        else False
    )
    has_nondefault_halt = (
        bool(df["Halted"].astype(bool).any()) if "Halted" in df.columns else False
    )
    explicit_cols = {
        "SplitFactor": _was_explicit("SplitFactor", has_nondefault_sf),
        "CashDividend": _was_explicit("CashDividend", has_nondefault_cd),
        "Halted": _was_explicit("Halted", has_nondefault_halt),
        "TotalReturnClose": _was_explicit("TotalReturnClose", False),
    }

    out = df.copy()
    out.index = pd.to_datetime(out.index).normalize()
    out = out.sort_index()
    if out.index.has_duplicates:
        raise ValueError(f"DUPLICATE_BAR_DATES:{code}")

    req_cols = ["Open", "High", "Low", "Close", "Volume"]
    for col in req_cols:
        if col not in out.columns:
            raise ValueError(f"MISSING_COLUMN:{code}:{col}")

    vals = out[req_cols].astype(float)
    finite_mask = np.isfinite(vals).all(axis=1)
    pos_price = (vals[["Open", "High", "Low", "Close"]] > 0.0).all(axis=1)
    nonneg_vol = vals["Volume"] >= 0.0
    ohlc_ok = (vals["High"] >= vals[["Open", "Low", "Close"]].max(axis=1)) & (
        vals["Low"] <= vals[["Open", "High", "Close"]].min(axis=1)
    )
    valid_mask = finite_mask & pos_price & nonneg_vol & ohlc_ok
    if not valid_mask.all():
        bad_dates = [d.strftime("%Y-%m-%d") for d in out.index[~valid_mask][:5]]
        raise ValueError(f"INVALID_OHLCV_ROWS:{code}:{bad_dates}")

    out["Open"] = vals["Open"]
    out["High"] = vals["High"]
    out["Low"] = vals["Low"]
    out["Close"] = vals["Close"]
    out["Volume"] = vals["Volume"]

    if "SplitFactor" in out.columns:
        sf = out["SplitFactor"].astype(float)
        if not (np.isfinite(sf).all() and (sf > 0.0).all()):
            raise ValueError(f"INVALID_SPLIT_FACTOR:{code}")
        out["SplitFactor"] = sf
    else:
        out["SplitFactor"] = 1.0

    if "CashDividend" in out.columns:
        cd = out["CashDividend"].astype(float)
        if not (np.isfinite(cd).all() and (cd >= 0.0).all()):
            raise ValueError(f"INVALID_CASH_DIVIDEND:{code}")
        out["CashDividend"] = cd
    else:
        out["CashDividend"] = 0.0

    if "Halted" in out.columns:
        out["Halted"] = out["Halted"].astype(bool)
    else:
        out["Halted"] = False

    if "TotalReturnClose" in out.columns:
        tr_col = out["TotalReturnClose"].astype(float)
        if not (np.isfinite(tr_col).all() and (tr_col > 0.0).all()):
            raise ValueError(f"INVALID_TOTAL_RETURN_CLOSE:{code}")
        out["TotalReturnClose"] = tr_col
    else:
        # Forward-compounded total-return index (100% prefix-invariant, zero future leakage):
        # G_t = (SplitFactor_t / SplitFactor_{t-1}) * (Close_t + CashDividend_t) / Close_{t-1}
        c_arr = out["Close"].to_numpy(dtype=float)
        sf_arr = out["SplitFactor"].to_numpy(dtype=float)
        cd_arr = out["CashDividend"].to_numpy(dtype=float)
        tr_arr = np.empty(len(out), dtype=float)
        tr_arr[0] = c_arr[0]
        if len(out) > 1:
            split_step = sf_arr[1:] / sf_arr[:-1]
            gross_ret = split_step * (c_arr[1:] + cd_arr[1:]) / c_arr[:-1]
            tr_arr[1:] = c_arr[0] * np.cumprod(gross_ret)
        if not (np.isfinite(tr_arr).all() and (tr_arr > 0.0).all()):
            raise ValueError(f"COMPUTED_TOTAL_RETURN_INVALID:{code}")
        out["TotalReturnClose"] = tr_arr

    out.attrs["explicit_columns"] = explicit_cols
    return out


def bar_dataframe_sha256(df: pd.DataFrame) -> str:
    """Compute canonical SHA256 digest of a validated bar DataFrame and its explicit-column flags."""
    explicit_cols = (
        {str(k): bool(v) for k, v in sorted(df.attrs["explicit_columns"].items())}
        if isinstance(df.attrs.get("explicit_columns"), Mapping)
        else {}
    )
    cols = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "SplitFactor",
        "CashDividend",
        "Halted",
        "TotalReturnClose",
    ]
    rows = [
        [
            idx.strftime("%Y-%m-%d"),
            float(row["Open"]),
            float(row["High"]),
            float(row["Low"]),
            float(row["Close"]),
            float(row["Volume"]),
            float(row["SplitFactor"]),
            float(row["CashDividend"]),
            bool(row["Halted"]),
            float(row["TotalReturnClose"]),
        ]
        for idx, row in df[cols].iterrows()
    ]
    return sha256_bytes(
        canonical_json_bytes({"explicit_columns": explicit_cols, "rows": rows})
    )


def verify_total_return_reconciliation(df: pd.DataFrame, rtol: float = 1e-6) -> bool:
    """Verify whether `TotalReturnClose` one-step return ratios reconcile with `(Close, SplitFactor, CashDividend)` (§2 line 29).

    Catches vendor `AdjClose` series that omit distributions, double-adjust splits, or subtract dividends additively.
    """
    if len(df) < 2:
        return True
    for req in ("Close", "SplitFactor", "CashDividend", "TotalReturnClose"):
        if req not in df.columns:
            return False
    c_arr = df["Close"].to_numpy(dtype=float)
    sf_arr = df["SplitFactor"].to_numpy(dtype=float)
    cd_arr = df["CashDividend"].to_numpy(dtype=float)
    tr_arr = df["TotalReturnClose"].to_numpy(dtype=float)
    if not (
        np.isfinite(c_arr).all()
        and np.isfinite(sf_arr).all()
        and np.isfinite(cd_arr).all()
        and np.isfinite(tr_arr).all()
        and (c_arr > 0.0).all()
        and (sf_arr > 0.0).all()
        and (tr_arr > 0.0).all()
    ):
        return False
    expected_step = (sf_arr[1:] / sf_arr[:-1]) * (c_arr[1:] + cd_arr[1:]) / c_arr[:-1]
    actual_step = tr_arr[1:] / tr_arr[:-1]
    return bool(np.allclose(actual_step, expected_step, rtol=rtol, atol=1e-9))


def select_frozen_representatives(
    candidates: Sequence[SectorETFSpec],
    bars_by_code: Mapping[str, pd.DataFrame],
    benchmarks: Mapping[str, RegionalBenchmarkSpec],
    freeze_date: pd.Timestamp | str,
    valid_from: pd.Timestamp | str,
    valid_until: pd.Timestamp | str,
    mapping_version: str = "v1.0-initial",
    is_pit_verified: bool = False,
    require_six_month_lock: bool = True,
) -> FrozenSectorMapping:
    """Deterministically select 1 representative ETF per (region, sector) on freeze_date (§2).

    - Strictly slices bars `<= freeze_date` (zero lookahead).
    - Excludes leveraged/inverse/gold/commodity/bond/parking ETFs.
    - Separates unclear/mixed region ETFs into `pending_mixed_codes`.
    - Rejects unreviewed mappings (`mapping_reviewed == False`).
    - Applies `completed_bars >= 120` and `20d median turnover >= 1,000,000,000 KRW`.
    - Picks highest 20d median turnover, tie-broken by `code` ascending.
    """
    f_ts = pd.Timestamp(freeze_date).normalize()
    for b in benchmarks.values():
        b.validate()

    pending_mixed: list[str] = []
    excluded_non_eq: list[str] = []
    grouped: dict[tuple[str, str], list[tuple[float, str, SectorETFSpec]]] = {}

    for cand in candidates:
        if cand.asset_class in EXCLUDED_ASSET_CLASSES or cand.asset_class != "EQUITY_SECTOR":
            excluded_non_eq.append(cand.code)
            continue
        if not cand.region or cand.region.upper() in {"MIXED", "UNKNOWN", "UNCLEAR"} or cand.region not in benchmarks:
            pending_mixed.append(cand.code)
            continue
        if not cand.mapping_reviewed:
            raise ValueError(f"UNREVIEWED_SECTOR_MAPPING_PROHIBITED:{cand.code}")
        if not cand.underlying_index.strip():
            raise ValueError(f"MISSING_UNDERLYING_INDEX:{cand.code}")

        bench = benchmarks[cand.region]
        if cand.currency != bench.currency or cand.fx_hedged != bench.fx_hedged:
            raise ValueError(f"BENCHMARK_CURRENCY_OR_HEDGE_MISMATCH:{cand.code}:{bench.code}")

        if not cand.is_active_on(f_ts):
            continue
        df = bars_by_code.get(cand.code)
        if df is None:
            continue
        hist = df.loc[:f_ts]
        # Completed traded bars (non-halted) up to freeze_date
        completed = hist.loc[~hist["Halted"]] if "Halted" in hist.columns else hist
        if len(completed) < MIN_HISTORY_BARS:
            continue
        # Must be current at freeze_date (match benchmark's latest session <= freeze_date)
        bench_df = bars_by_code.get(bench.code)
        if bench_df is None or bench_df.loc[:f_ts].empty:
            raise ValueError(f"BENCHMARK_DATA_MISSING_ON_FREEZE:{bench.code}")
        expected_last = bench_df.loc[:f_ts].index[-1]
        if completed.index[-1] != expected_last:
            continue

        recent20 = completed.iloc[-20:]
        turnover20 = (recent20["Close"].astype(float) * recent20["Volume"].astype(float)).median()
        if not np.isfinite(turnover20) or float(turnover20) < MIN_MEDIAN_TURNOVER_20D_KRW:
            continue

        key = (cand.region, cand.sector)
        grouped.setdefault(key, []).append((float(turnover20), cand.code, cand))

    chosen: dict[tuple[str, str], SectorETFSpec] = {}
    for key, pool in sorted(grouped.items()):
        pool.sort(key=lambda item: (-item[0], item[1]))
        chosen[key] = pool[0][2]

    mapping = FrozenSectorMapping(
        mapping_version=mapping_version,
        freeze_date=f_ts,
        valid_from=pd.Timestamp(valid_from).normalize(),
        valid_until=pd.Timestamp(valid_until).normalize(),
        benchmarks=dict(benchmarks),
        representatives=chosen,
        pending_mixed_codes=tuple(sorted(set(pending_mixed))),
        excluded_non_equity_codes=tuple(sorted(set(excluded_non_eq))),
        is_pit_verified=is_pit_verified,
    )
    mapping.validate_lock_period(require_six_month_lock=require_six_month_lock)
    return mapping


@dataclass(frozen=True)
class DataProvenanceManifest:
    """Sealed provenance manifest for input price/mapping/cost datasets (§2, §8, §10)."""

    manifest_id: str
    fetched_at: str
    provider: str
    total_return_verified: bool
    corporate_actions_verified: bool
    mapping_pit_verified: bool
    constituents_pit_verified_regions: tuple[str, ...]
    file_hashes: Mapping[str, str] = field(default_factory=dict)
    notes: str = ""

    def sha256(self) -> str:
        payload = {
            "spec_version": SPEC_VERSION,
            "manifest_id": self.manifest_id,
            "fetched_at": self.fetched_at,
            "provider": self.provider,
            "total_return_verified": self.total_return_verified,
            "corporate_actions_verified": self.corporate_actions_verified,
            "mapping_pit_verified": self.mapping_pit_verified,
            "constituents_pit_verified_regions": list(self.constituents_pit_verified_regions),
            "file_hashes": dict(sorted(self.file_hashes.items())),
            "notes": self.notes,
        }
        return sha256_bytes(canonical_json_bytes(payload))


@dataclass(frozen=True)
class ValidatedResearchInputs:
    """Boundary-validated research input bundle shared by CLI and Python API (§§2, 8, 9, 10).

    Separates `declared` provenance claims from `mechanical` checks (`verify_total_return_reconciliation`,
    explicit corporate-action column presence, PIT flag consistency) and derives `effective` verification
    status so corrupted TR series, defaulted `0/1` corporate-action columns, or conflicting PIT flags
    can never silently pass into adoption review.
    """

    calendar: TradingCalendar
    mapping: FrozenSectorMapping
    bars_by_code: Mapping[str, pd.DataFrame]
    cost_schedule: CostSchedule
    provenance: DataProvenanceManifest
    pit_constituents: PITConstituentTable | None
    bar_hashes: Mapping[str, str]
    explicit_columns_by_code: Mapping[str, Mapping[str, bool]]
    input_audit: Mapping[str, object]


def validate_research_inputs(
    *,
    calendar: TradingCalendar,
    mapping: FrozenSectorMapping,
    bars_by_code: Mapping[str, pd.DataFrame],
    cost_schedule: CostSchedule,
    provenance: DataProvenanceManifest,
    pit_constituents: PITConstituentTable | None = None,
    strict: bool = True,
) -> ValidatedResearchInputs:
    """Validate and audit all research inputs at the CLI / Python API boundary."""
    mapping.validate_lock_period(require_six_month_lock=True)
    cost_schedule.validate()

    cleaned_bars: dict[str, pd.DataFrame] = {}
    bar_hashes: dict[str, str] = {}
    explicit_by_code: dict[str, dict[str, bool]] = {}
    tr_failed_codes: list[str] = []
    defaulted_ca_codes: list[str] = []

    core_codes = {b.code for b in mapping.benchmarks.values()} | {
        s.code for s in mapping.representatives.values()
    }
    pit_member_codes: set[str] = set()
    if pit_constituents is not None:
        for entries in pit_constituents.snapshots.values():
            for _, members in entries:
                for item in members:
                    pit_member_codes.add(item.code)

    for code, raw_df in sorted(bars_by_code.items()):
        cdf = clean_and_validate_bars(raw_df, code=code)
        cleaned_bars[code] = cdf
        bar_hashes[code] = bar_dataframe_sha256(cdf)
        exp_map = dict(cdf.attrs.get("explicit_columns", {}))
        explicit_by_code[code] = exp_map
        if not verify_total_return_reconciliation(cdf):
            tr_failed_codes.append(code)
        if code in (core_codes | pit_member_codes):
            if not (exp_map.get("SplitFactor", False) and exp_map.get("CashDividend", False)):
                defaulted_ca_codes.append(code)

    missing_core_bar_codes = sorted(c for c in core_codes if c not in cleaned_bars)

    conflicts: list[str] = []
    if bool(mapping.is_pit_verified) != bool(provenance.mapping_pit_verified):
        conflicts.append("CONFLICTING_MAPPING_PIT_VERIFICATION_FLAGS")

    declared_pit_regions = tuple(sorted(set(provenance.constituents_pit_verified_regions)))
    covered_by_pit_table = (
        pit_constituents.covered_sectors_by_region() if pit_constituents is not None else {}
    )
    if declared_pit_regions:
        if pit_constituents is None or not bool(pit_constituents.is_pit_verified):
            conflicts.append("CONFLICTING_CONSTITUENTS_PIT_VERIFICATION_FLAGS")
        else:
            missing_declared_regs = [r for r in declared_pit_regions if r not in covered_by_pit_table]
            if missing_declared_regs:
                conflicts.append(
                    f"DECLARED_PIT_REGIONS_MISSING_IN_TABLE:{','.join(missing_declared_regs)}"
                )

    if strict:
        if tr_failed_codes:
            raise ValueError(
                f"TOTAL_RETURN_RECONCILIATION_FAILED:{','.join(sorted(tr_failed_codes))}"
            )
        if conflicts:
            raise ValueError(f"INPUT_PROVENANCE_CONFLICT:{'|'.join(conflicts)}")

    all_tr_reconciled = len(tr_failed_codes) == 0 and len(missing_core_bar_codes) == 0
    all_explicit_ca = len(defaulted_ca_codes) == 0 and len(missing_core_bar_codes) == 0

    eff_ca_verified = bool(
        provenance.corporate_actions_verified and all_explicit_ca and not conflicts
    )
    eff_tr_verified = bool(
        provenance.total_return_verified
        and all_tr_reconciled
        and eff_ca_verified
    )
    eff_mapping_pit = bool(
        mapping.is_pit_verified
        and provenance.mapping_pit_verified
        and "CONFLICTING_MAPPING_PIT_VERIFICATION_FLAGS" not in conflicts
    )
    if (
        pit_constituents is not None
        and bool(pit_constituents.is_pit_verified)
        and "CONFLICTING_CONSTITUENTS_PIT_VERIFICATION_FLAGS" not in conflicts
    ):
        eff_pit_regions = tuple(r for r in declared_pit_regions if r in covered_by_pit_table)
    else:
        eff_pit_regions = ()

    audit_prereqs_missing: list[str] = []
    if tr_failed_codes:
        audit_prereqs_missing.append("TOTAL_RETURN_RECONCILIATION_FAILED")
    if missing_core_bar_codes:
        audit_prereqs_missing.append("CORE_BENCHMARK_OR_REPRESENTATIVE_BARS_MISSING")
    if provenance.corporate_actions_verified and not all_explicit_ca:
        audit_prereqs_missing.append("CORPORATE_ACTIONS_DEFAULTED_COLUMNS_NOT_VERIFIED")
    if provenance.total_return_verified and not eff_tr_verified:
        audit_prereqs_missing.append("DECLARED_TOTAL_RETURN_UNVERIFIED_MECHANICALLY")
    for c_err in conflicts:
        audit_prereqs_missing.append(c_err)
    if not eff_tr_verified:
        audit_prereqs_missing.append("TOTAL_RETURN_NOT_VERIFIED")
    if not eff_ca_verified:
        audit_prereqs_missing.append("CORPORATE_ACTIONS_NOT_VERIFIED")
    if not eff_mapping_pit:
        audit_prereqs_missing.append("SECTOR_MAPPING_NOT_PIT_VERIFIED")

    input_audit = {
        "declared": {
            "total_return_verified": bool(provenance.total_return_verified),
            "corporate_actions_verified": bool(provenance.corporate_actions_verified),
            "mapping_is_pit_verified": bool(mapping.is_pit_verified),
            "provenance_mapping_pit_verified": bool(provenance.mapping_pit_verified),
            "pit_table_is_pit_verified": (
                bool(pit_constituents.is_pit_verified) if pit_constituents is not None else None
            ),
            "constituents_pit_verified_regions": list(declared_pit_regions),
        },
        "mechanical": {
            "all_total_return_reconciled": all_tr_reconciled,
            "total_return_failed_codes": sorted(tr_failed_codes),
            "all_explicit_corporate_action_columns": all_explicit_ca,
            "defaulted_corporate_action_codes": sorted(defaulted_ca_codes),
            "missing_core_bar_codes": missing_core_bar_codes,
            "conflicts": conflicts,
        },
        "effective": {
            "total_return_verified": eff_tr_verified,
            "corporate_actions_verified": eff_ca_verified,
            "mapping_pit_verified": eff_mapping_pit,
            "constituents_pit_verified_regions": list(eff_pit_regions),
        },
        "missing_prerequisites": audit_prereqs_missing,
    }

    return ValidatedResearchInputs(
        calendar=calendar,
        mapping=mapping,
        bars_by_code=cleaned_bars,
        cost_schedule=cost_schedule,
        provenance=provenance,
        pit_constituents=pit_constituents,
        bar_hashes=bar_hashes,
        explicit_columns_by_code=explicit_by_code,
        input_audit=input_audit,
    )
