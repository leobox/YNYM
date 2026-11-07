"""E1 manual single-check forward journal schema, band validator, and append-only ledger (§§1, 6, 7).

Strictly separates:
- E0 historical open approximation vs E1 human manual check records.
- Reference 11:00 KST research comparison vs user's actual observation/order/fill timestamps.
- Virtual observation without order (`VIRTUAL_OBSERVATION_NO_ORDER`) vs actual filled trades.
- First 12-week operational/collection consistency check vs >=24m & >=60-episode profitability claims.
No broker API or order execution capability exists in this module.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from . import SPEC_VERSION, canonical_json_bytes, sha256_bytes
from .execution_e0 import round_down_to_krx_tick
from .models import TradingCalendar

KST_TZ = "Asia/Seoul"

ENTRY_DISPOSITIONS = frozenset(
    {
        "FILLED",
        "UNFILLED_ORDER",
        "VIRTUAL_OBSERVATION_NO_ORDER",
        "CANCELLED_OUT_OF_BAND",
        "CANCELLED_MISSED_CHECK",
    }
)
EXIT_DISPOSITIONS = frozenset(
    {
        "EXIT_FILLED",
        "EXIT_UNFILLED_OR_HALTED",
        "EXIT_VIRTUAL_OBSERVATION_NO_ORDER",
    }
)


def compute_e1_entry_check_band(
    signal_sma20: float,
    signal_close: float,
    signal_atr20: float,
    instrument_kind: str = "EQUITY_ETF",
) -> dict[str, float | bool]:
    """Compute E1 manual entry price band (§6 line 96):

    `전일 SMA20 < 확인 가격 <= 전일 종가 + 0.5 * ATR20`
    """
    vals = [signal_sma20, signal_close, signal_atr20]
    if not np.isfinite(vals).all() or signal_sma20 <= 0.0 or signal_close <= 0.0 or signal_atr20 <= 0.0:
        raise ValueError("INVALID_E1_BAND_INPUTS")
    lower_excl = float(signal_sma20)
    upper_incl = float(signal_close + 0.5 * signal_atr20)
    tick_ref = round_down_to_krx_tick(upper_incl, instrument_kind=instrument_kind)
    return {
        "lower_exclusive_sma20": lower_excl,
        "upper_inclusive_close_plus_half_atr": upper_incl,
        "upper_krx_tick_reference": tick_ref,
        "band_non_empty": bool(lower_excl < upper_incl),
    }


def is_e1_entry_price_in_band(
    observed_price: float,
    signal_sma20: float,
    signal_close: float,
    signal_atr20: float,
) -> bool:
    """Return True iff `signal_sma20 < observed_price <= signal_close + 0.5 * signal_atr20`."""
    if not np.isfinite([observed_price, signal_sma20, signal_close, signal_atr20]).all():
        return False
    if observed_price <= 0.0 or signal_sma20 <= 0.0 or signal_close <= 0.0 or signal_atr20 <= 0.0:
        return False
    return bool(signal_sma20 < observed_price <= (signal_close + 0.5 * signal_atr20))


def _is_strict_nonneg_int(val: object) -> bool:
    return (
        isinstance(val, (int, np.integer))
        and not isinstance(val, (bool, np.bool_))
        and int(val) >= 0
    )


def normalize_e1_date(val: str | pd.Timestamp) -> str:
    """Normalize any calendar date representation to canonical `YYYY-MM-DD` in KST."""
    if val is None or (isinstance(val, str) and not val.strip()):
        raise ValueError("E1_EMPTY_DATE_STRING")
    ts = pd.Timestamp(val)
    if pd.isna(ts):
        raise ValueError(f"E1_INVALID_DATE:{val}")
    if ts.tzinfo is not None:
        ts = ts.tz_convert(KST_TZ)
    return ts.normalize().strftime("%Y-%m-%d")


def normalize_e1_kst_timestamp(val: str | pd.Timestamp) -> pd.Timestamp:
    """Parse timestamp and normalize to timezone-aware `Asia/Seoul` (KST)."""
    if val is None or (isinstance(val, str) and not val.strip()):
        raise ValueError("E1_EMPTY_TIMESTAMP")
    ts = pd.Timestamp(val)
    if pd.isna(ts):
        raise ValueError(f"E1_INVALID_TIMESTAMP:{val}")
    if ts.tzinfo is None:
        return ts.tz_localize(KST_TZ)
    return ts.tz_convert(KST_TZ)


def normalize_e1_code(code: str) -> str:
    cleaned = str(code).strip().upper()
    if not cleaned:
        raise ValueError("E1_EMPTY_CODE")
    return cleaned


_UNVERIFIED_COMPLIANCE_REASONS = frozenset(
    {
        "CALENDAR_REQUIRED_FOR_NEXT_SESSION_COMPLIANCE",
        "CALENDAR_REQUIRED_FOR_NEXT_SESSION_REVERIFICATION",
    }
)


def _journal_calendar_sidecar_path(journal_path: Path) -> Path:
    p = Path(journal_path)
    return p.with_name(p.name + ".calendar.json")


@dataclass(frozen=True)
class E1JournalRecord:
    record_id: str
    action_type: str  # ENTRY | EXIT
    signal_date: str  # YYYY-MM-DD (completed bar date)
    check_session_date: str  # YYYY-MM-DD (next trading session)
    code: str
    region: str
    sector: str
    signal_close: float
    signal_sma20: float
    signal_atr20: float
    disposition: str
    check_index_for_signal: int = 1
    observed_at: str | None = None
    observed_price: float | None = None
    ordered_at: str | None = None
    order_qty: int = 0
    order_limit_or_type: str | None = None
    filled_at: str | None = None
    filled_qty: int = 0
    filled_price: float | None = None
    fee_tax_krw: float = 0.0
    is_research_1100_comparison_only: bool = False
    episode_id: str | None = None
    notes: str = ""
    recorded_compliance_status: str | None = None
    recorded_protocol_violations: tuple[str, ...] = ()
    recorded_calendar_id: str | None = None
    recorded_calendar_sha256: str | None = None
    recorded_expected_next_session_date: str | None = None
    _restored_calendar: TradingCalendar | None = field(
        default=None, repr=False, compare=False
    )

    def canonical_entry_key(self) -> tuple[str, str]:
        return (normalize_e1_date(self.signal_date), normalize_e1_code(self.code))

    def validate(self, calendar: TradingCalendar | None = None) -> None:
        if (
            not str(self.record_id).strip()
            or not str(self.code).strip()
            or not str(self.region).strip()
            or not str(self.sector).strip()
        ):
            raise ValueError("E1_RECORD_MISSING_IDENTIFIERS")

        sig_str = normalize_e1_date(self.signal_date)
        chk_str = normalize_e1_date(self.check_session_date)
        sig_dt = pd.Timestamp(sig_str)
        chk_dt = pd.Timestamp(chk_str)
        if chk_dt <= sig_dt:
            raise ValueError("E1_CHECK_DATE_MUST_BE_STRICTLY_AFTER_SIGNAL_DATE")

        if calendar is not None:
            if not calendar.is_session(sig_dt):
                raise ValueError(f"E1_SIGNAL_DATE_NOT_SCHEDULED_SESSION:{sig_str}")
            if not calendar.is_session(chk_dt):
                raise ValueError(f"E1_CHECK_DATE_NOT_SCHEDULED_SESSION:{chk_str}")
            if self.action_type == "ENTRY":
                expected_chk = calendar.next_session_after(sig_dt, offset=1)
                if expected_chk is None or chk_dt < expected_chk:
                    raise ValueError(
                        f"E1_CHECK_DATE_BEFORE_NEXT_SESSION:{chk_str}<{expected_chk}"
                    )

        if not np.isfinite([self.signal_close, self.signal_sma20, self.signal_atr20]).all():
            raise ValueError("E1_NONFINITE_SIGNAL_METRICS")
        if self.signal_close <= 0.0 or self.signal_sma20 <= 0.0 or self.signal_atr20 <= 0.0:
            raise ValueError("E1_NONPOSITIVE_SIGNAL_METRICS")
        if not np.isfinite(self.fee_tax_krw) or self.fee_tax_krw < 0.0:
            raise ValueError("E1_INVALID_FEE_TAX")

        if not _is_strict_nonneg_int(self.check_index_for_signal):
            raise ValueError("E1_INVALID_CHECK_INDEX_INTEGER")
        if not _is_strict_nonneg_int(self.order_qty) or not _is_strict_nonneg_int(self.filled_qty):
            raise ValueError("E1_QUANTITIES_MUST_BE_NONNEGATIVE_INTEGERS")
        if int(self.filled_qty) > int(self.order_qty):
            raise ValueError("E1_FILLED_QTY_EXCEEDS_ORDER_QTY")

        if self.observed_price is not None and (
            isinstance(self.observed_price, bool)
            or not math.isfinite(float(self.observed_price))
            or float(self.observed_price) <= 0.0
        ):
            raise ValueError("E1_INVALID_OBSERVED_PRICE")
        if self.filled_price is not None and (
            isinstance(self.filled_price, bool)
            or not math.isfinite(float(self.filled_price))
            or float(self.filled_price) <= 0.0
        ):
            raise ValueError("E1_INVALID_FILLED_PRICE")

        obs_ts = normalize_e1_kst_timestamp(self.observed_at) if self.observed_at else None
        ord_ts = normalize_e1_kst_timestamp(self.ordered_at) if self.ordered_at else None
        fil_ts = normalize_e1_kst_timestamp(self.filled_at) if self.filled_at else None

        if ord_ts is not None and obs_ts is None:
            raise ValueError("E1_ORDER_WITHOUT_OBSERVATION_TIMESTAMP")
        if ord_ts is not None and obs_ts is not None and ord_ts < obs_ts:
            raise ValueError("E1_ORDER_PRECEDES_OBSERVATION")
        if fil_ts is not None and ord_ts is None:
            raise ValueError("E1_FILL_WITHOUT_ORDER_TIMESTAMP")
        if fil_ts is not None and ord_ts is not None and fil_ts < ord_ts:
            raise ValueError("E1_FILL_PRECEDES_ORDER")

        if obs_ts is not None:
            obs_day = pd.Timestamp(obs_ts.strftime("%Y-%m-%d"))
            if obs_day <= sig_dt:
                raise ValueError("E1_OBSERVATION_ON_OR_BEFORE_SIGNAL_DATE")
            if obs_day < chk_dt:
                raise ValueError("E1_OBSERVATION_BEFORE_SCHEDULED_CHECK_SESSION")
            if calendar is not None and not calendar.is_session(obs_day):
                raise ValueError(f"E1_OBSERVATION_ON_NON_TRADING_DATE:{obs_day.strftime('%Y-%m-%d')}")
        if ord_ts is not None and calendar is not None:
            ord_day = pd.Timestamp(ord_ts.strftime("%Y-%m-%d"))
            if not calendar.is_session(ord_day):
                raise ValueError(f"E1_ORDER_ON_NON_TRADING_DATE:{ord_day.strftime('%Y-%m-%d')}")
        if fil_ts is not None and calendar is not None:
            fil_day = pd.Timestamp(fil_ts.strftime("%Y-%m-%d"))
            if not calendar.is_session(fil_day):
                raise ValueError(f"E1_FILL_ON_NON_TRADING_DATE:{fil_day.strftime('%Y-%m-%d')}")

        if self.is_research_1100_comparison_only and self.disposition in {
            "FILLED",
            "EXIT_FILLED",
            "UNFILLED_ORDER",
        }:
            raise ValueError("RESEARCH_1100_REFERENCE_CANNOT_CLAIM_REAL_USER_ORDER_OR_FILL")

        if self.action_type == "ENTRY":
            if self.disposition not in ENTRY_DISPOSITIONS:
                raise ValueError(f"INVALID_E1_ENTRY_DISPOSITION:{self.disposition}")
            if int(self.check_index_for_signal) != 1:
                raise ValueError("RETROACTIVE_OR_MULTI_CHECK_ENTRY_PROHIBITED")

            if self.disposition == "CANCELLED_MISSED_CHECK":
                if self.observed_at is not None or self.observed_price is not None:
                    raise ValueError("MISSED_CHECK_MUST_NOT_HAVE_OBSERVATION")
                if self.ordered_at is not None or self.order_qty != 0 or self.filled_qty != 0:
                    raise ValueError("MISSED_CHECK_CANNOT_HAVE_ORDER_OR_FILL")
                return

            if self.observed_at is None or self.observed_price is None or self.observed_price <= 0.0:
                raise ValueError("E1_OBSERVED_RECORD_MISSING_TIME_OR_PRICE")

            in_band = is_e1_entry_price_in_band(
                observed_price=self.observed_price,
                signal_sma20=self.signal_sma20,
                signal_close=self.signal_close,
                signal_atr20=self.signal_atr20,
            )

            if self.disposition == "CANCELLED_OUT_OF_BAND":
                if in_band:
                    raise ValueError("IN_BAND_PRICE_MARKED_AS_OUT_OF_BAND")
                if self.ordered_at is not None or self.order_qty != 0 or self.filled_qty != 0:
                    raise ValueError("OUT_OF_BAND_ENTRY_CANNOT_ORDER_OR_FILL")
            elif self.disposition == "VIRTUAL_OBSERVATION_NO_ORDER":
                if (
                    self.ordered_at is not None
                    or self.filled_at is not None
                    or self.order_qty != 0
                    or self.filled_qty != 0
                ):
                    raise ValueError("VIRTUAL_OBSERVATION_CANNOT_CONTAIN_ORDER_OR_FILL")
            elif self.disposition == "UNFILLED_ORDER":
                if not in_band:
                    raise ValueError("OUT_OF_BAND_ENTRY_ORDER_PROHIBITED")
                if self.ordered_at is None or self.order_qty <= 0 or self.filled_qty != 0:
                    raise ValueError("INVALID_UNFILLED_ORDER_FIELDS")
            elif self.disposition == "FILLED":
                if not in_band:
                    raise ValueError("RETROACTIVE_OR_OUT_OF_BAND_ENTRY_FILL_PROHIBITED")
                if (
                    self.ordered_at is None
                    or self.filled_at is None
                    or self.order_qty <= 0
                    or self.filled_qty <= 0
                    or self.filled_qty > self.order_qty
                    or self.filled_price is None
                    or self.filled_price <= 0.0
                ):
                    raise ValueError("INVALID_FILLED_ENTRY_FIELDS")

        elif self.action_type == "EXIT":
            if self.disposition not in EXIT_DISPOSITIONS:
                raise ValueError(f"INVALID_E1_EXIT_DISPOSITION:{self.disposition}")
            if self.disposition == "EXIT_FILLED":
                if (
                    self.observed_at is None
                    or self.observed_price is None
                    or self.observed_price <= 0.0
                    or self.ordered_at is None
                    or self.filled_at is None
                    or self.order_qty <= 0
                    or self.filled_qty <= 0
                    or self.filled_qty > self.order_qty
                    or self.filled_price is None
                    or self.filled_price <= 0.0
                ):
                    raise ValueError("INVALID_EXIT_FILLED_FIELDS")
            elif self.disposition == "EXIT_VIRTUAL_OBSERVATION_NO_ORDER":
                if (
                    self.ordered_at is not None
                    or self.filled_at is not None
                    or self.order_qty != 0
                    or self.filled_qty != 0
                ):
                    raise ValueError("EXIT_VIRTUAL_OBSERVATION_CANNOT_FILL")
            elif self.disposition == "EXIT_UNFILLED_OR_HALTED":
                if self.filled_qty != 0 or self.filled_at is not None or self.filled_price is not None:
                    raise ValueError("EXIT_UNFILLED_CANNOT_HAVE_FILLED_QTY")
        else:
            raise ValueError(f"UNKNOWN_E1_ACTION_TYPE:{self.action_type}")

    def _timing_violations_without_calendar(self) -> list[str]:
        chk_dt = pd.Timestamp(normalize_e1_date(self.check_session_date))
        violations: list[str] = []
        if self.observed_at:
            obs_day = pd.Timestamp(
                normalize_e1_kst_timestamp(self.observed_at).strftime("%Y-%m-%d")
            )
            if obs_day > chk_dt:
                violations.append("LATE_OBSERVATION_AFTER_CHECK_SESSION")
        if self.ordered_at:
            ord_day = pd.Timestamp(
                normalize_e1_kst_timestamp(self.ordered_at).strftime("%Y-%m-%d")
            )
            if ord_day > chk_dt:
                violations.append("LATE_ORDER_AFTER_CHECK_SESSION")
        if self.filled_at:
            fil_day = pd.Timestamp(
                normalize_e1_kst_timestamp(self.filled_at).strftime("%Y-%m-%d")
            )
            if fil_day > chk_dt:
                violations.append("LATE_FILL_AFTER_CHECK_SESSION")
        return violations

    def audit_compliance(self, calendar: TradingCalendar | None = None) -> dict:
        """Return full audit dict separating recorded compliance from latest calendar re-evaluation (§6)."""
        eff_cal = calendar if calendar is not None else self._restored_calendar
        self.validate(calendar=eff_cal)
        sig_dt = pd.Timestamp(normalize_e1_date(self.signal_date))
        chk_dt = pd.Timestamp(normalize_e1_date(self.check_session_date))
        live_violations = self._timing_violations_without_calendar()

        expected_next_str: str | None = None
        if eff_cal is not None:
            if self.action_type == "ENTRY":
                expected_chk = eff_cal.next_session_after(sig_dt, offset=1)
                if expected_chk is not None:
                    expected_next_str = expected_chk.strftime("%Y-%m-%d")
                    if chk_dt > expected_chk:
                        live_violations.insert(
                            0, "ENTRY_CHECK_SESSION_LATER_THAN_NEXT_TRADING_SESSION"
                        )
            reeval_status = (
                "PROTOCOL_VIOLATION_PRESERVED" if live_violations else "COMPLIANT"
            )
            reeval_reasons = tuple(dict.fromkeys(live_violations))
            eff_status = reeval_status
            eff_reasons = reeval_reasons
        else:
            # No calendar provided or restored: NEVER promote an unverified or previously-violating record to COMPLIANT
            stored_viols = tuple(
                v
                for v in (self.recorded_protocol_violations or ())
                if v not in _UNVERIFIED_COMPLIANCE_REASONS
            )
            merged_viols = tuple(dict.fromkeys([*stored_viols, *live_violations]))
            if (
                self.recorded_compliance_status == "PROTOCOL_VIOLATION_PRESERVED"
                or len(merged_viols) > 0
            ):
                reeval_status = (
                    "PROTOCOL_VIOLATION_PRESERVED"
                    if live_violations
                    else "UNVERIFIED_NO_CALENDAR"
                )
                reeval_reasons = (
                    tuple(live_violations)
                    if live_violations
                    else ("CALENDAR_REQUIRED_FOR_NEXT_SESSION_REVERIFICATION",)
                )
                eff_status = "PROTOCOL_VIOLATION_PRESERVED"
                eff_reasons = merged_viols
            else:
                reeval_status = "UNVERIFIED_NO_CALENDAR"
                reeval_reasons = ("CALENDAR_REQUIRED_FOR_NEXT_SESSION_COMPLIANCE",)
                eff_status = "UNVERIFIED_NO_CALENDAR"
                eff_reasons = reeval_reasons

        cal_reclassified = bool(
            calendar is not None
            and self.recorded_compliance_status is not None
            and self.recorded_compliance_status != reeval_status
        )
        return {
            "record_id": self.record_id,
            "recorded_compliance_status": self.recorded_compliance_status,
            "recorded_protocol_violations": list(self.recorded_protocol_violations or ()),
            "recorded_calendar_id": self.recorded_calendar_id,
            "recorded_calendar_sha256": self.recorded_calendar_sha256,
            "recorded_expected_next_session_date": self.recorded_expected_next_session_date,
            "reevaluated_compliance_status": reeval_status,
            "reevaluated_protocol_violations": list(reeval_reasons),
            "reevaluated_calendar_id": eff_cal.calendar_id if eff_cal is not None else None,
            "reevaluated_calendar_sha256": eff_cal.sha256() if eff_cal is not None else None,
            "expected_next_session_date": expected_next_str,
            "calendar_reclassification_changed": cal_reclassified,
            "effective_compliance_status": eff_status,
            "effective_protocol_violations": list(eff_reasons),
        }

    def evaluate_compliance(
        self, calendar: TradingCalendar | None = None
    ) -> tuple[str, tuple[str, ...]]:
        """Evaluate whether a valid record complied with scheduled single-session timing (§6).

        - Late manual observations/orders/fills are preserved as `PROTOCOL_VIOLATION_PRESERVED`.
        - Missing calendar (`calendar=None` without restored linked calendar) preserves stored
          violations and marks unverified records as `UNVERIFIED_NO_CALENDAR`, never `COMPLIANT`.
        """
        audit = self.audit_compliance(calendar=calendar)
        return (
            str(audit["effective_compliance_status"]),
            tuple(audit["effective_protocol_violations"]),
        )

    def to_dict(self, calendar: TradingCalendar | None = None) -> dict:
        audit = self.audit_compliance(calendar=calendar)
        eff_cal = calendar if calendar is not None else self._restored_calendar
        payload = {
            "record_id": self.record_id,
            "action_type": self.action_type,
            "signal_date": normalize_e1_date(self.signal_date),
            "check_session_date": normalize_e1_date(self.check_session_date),
            "code": normalize_e1_code(self.code),
            "region": self.region,
            "sector": self.sector,
            "signal_close": float(self.signal_close),
            "signal_sma20": float(self.signal_sma20),
            "signal_atr20": float(self.signal_atr20),
            "disposition": self.disposition,
            "check_index_for_signal": int(self.check_index_for_signal),
            "observed_at": (
                normalize_e1_kst_timestamp(self.observed_at).isoformat()
                if self.observed_at
                else None
            ),
            "observed_price": self.observed_price,
            "ordered_at": (
                normalize_e1_kst_timestamp(self.ordered_at).isoformat()
                if self.ordered_at
                else None
            ),
            "order_qty": int(self.order_qty),
            "order_limit_or_type": self.order_limit_or_type,
            "filled_at": (
                normalize_e1_kst_timestamp(self.filled_at).isoformat()
                if self.filled_at
                else None
            ),
            "filled_qty": int(self.filled_qty),
            "filled_price": self.filled_price,
            "fee_tax_krw": float(self.fee_tax_krw),
            "is_research_1100_comparison_only": bool(self.is_research_1100_comparison_only),
            "episode_id": self.episode_id,
            "notes": self.notes,
            "compliance_status": audit["effective_compliance_status"],
            "protocol_violations": list(audit["effective_protocol_violations"]),
            "recorded_calendar_id": (
                eff_cal.calendar_id if eff_cal is not None else self.recorded_calendar_id
            ),
            "recorded_calendar_sha256": (
                eff_cal.sha256() if eff_cal is not None else self.recorded_calendar_sha256
            ),
            "recorded_expected_next_session_date": (
                audit["expected_next_session_date"]
                if audit["expected_next_session_date"] is not None
                else self.recorded_expected_next_session_date
            ),
            "spec_version": SPEC_VERSION,
        }
        return payload


def reconcile_e1_position_ledger(
    records: Sequence[E1JournalRecord],
    calendar: TradingCalendar | None = None,
) -> dict:
    """Reconcile E1 ENTRY/EXIT share balances and realized cash flows by episode (§6, §8, §9).

    - Partial exits reduce `remaining_qty` and do NOT complete an episode until `remaining_qty == 0`.
    - Orphan exits (no matching open entry), over-exits (`filled_qty > remaining_qty`), exit before
      entry fill, or duplicate concurrent entries while holding are flagged in `inconsistencies`.
    """
    open_by_ep: dict[str, dict] = {}
    open_ep_by_code: dict[str, str] = {}
    closed_episodes: list[dict] = []
    inconsistencies: list[str] = []

    for rec in records:
        eff_cal = calendar if calendar is not None else rec._restored_calendar
        rec.validate(calendar=eff_cal)
        comp_status, _ = rec.evaluate_compliance(calendar=calendar)
        is_compliant = comp_status == "COMPLIANT"
        code_norm = normalize_e1_code(rec.code)
        sig_norm = normalize_e1_date(rec.signal_date)

        if rec.action_type == "ENTRY" and rec.disposition == "FILLED":
            ep_id = (
                str(rec.episode_id).strip()
                if (rec.episode_id is not None and str(rec.episode_id).strip())
                else f"{code_norm}@{sig_norm}"
            )
            if code_norm in open_ep_by_code:
                inconsistencies.append(
                    f"DUPLICATE_CONCURRENT_ENTRY_WHILE_HOLDING:{rec.record_id}:{code_norm}"
                )
                continue
            if ep_id in open_by_ep or any(ep["episode_id"] == ep_id for ep in closed_episodes):
                inconsistencies.append(f"DUPLICATE_EPISODE_ID:{rec.record_id}:{ep_id}")
                continue

            fil_ts = normalize_e1_kst_timestamp(rec.filled_at)  # type: ignore[arg-type]
            entry_cost = float(rec.filled_qty) * float(rec.filled_price) + float(rec.fee_tax_krw)  # type: ignore[arg-type]
            open_by_ep[ep_id] = {
                "episode_id": ep_id,
                "code": code_norm,
                "region": rec.region,
                "sector": rec.sector,
                "entry_record_id": rec.record_id,
                "entry_signal_date": sig_norm,
                "entry_filled_at": fil_ts,
                "entry_qty": int(rec.filled_qty),
                "remaining_qty": int(rec.filled_qty),
                "entry_cost_krw": entry_cost,
                "exit_proceeds_net_krw": 0.0,
                "exit_fill_records": [],
                "all_compliant": is_compliant,
            }
            open_ep_by_code[code_norm] = ep_id

        elif rec.action_type == "EXIT" and rec.disposition == "EXIT_FILLED":
            explicit_ep = (
                str(rec.episode_id).strip()
                if (rec.episode_id is not None and str(rec.episode_id).strip())
                else None
            )
            target_ep_id = explicit_ep if explicit_ep is not None else open_ep_by_code.get(code_norm)
            if target_ep_id is None or target_ep_id not in open_by_ep:
                inconsistencies.append(
                    f"ORPHAN_EXIT_WITHOUT_OPEN_POSITION:{rec.record_id}:{code_norm}"
                )
                continue

            ep = open_by_ep[target_ep_id]
            if ep["code"] != code_norm:
                inconsistencies.append(
                    f"EXIT_CODE_MISMATCH_FOR_EPISODE:{rec.record_id}:{code_norm}!={ep['code']}"
                )
                continue

            obs_ts = normalize_e1_kst_timestamp(rec.observed_at)  # type: ignore[arg-type]
            fil_ts = normalize_e1_kst_timestamp(rec.filled_at)  # type: ignore[arg-type]
            if obs_ts < ep["entry_filled_at"] or fil_ts < ep["entry_filled_at"]:
                inconsistencies.append(
                    f"EXIT_TIMESTAMP_PRECEDES_ENTRY_FILL:{rec.record_id}:{target_ep_id}"
                )
                continue

            exit_qty = int(rec.filled_qty)
            if exit_qty > int(ep["remaining_qty"]):
                inconsistencies.append(
                    f"OVER_EXIT_EXCEEDS_OPEN_SHARES:{rec.record_id}:{exit_qty}>{ep['remaining_qty']}"
                )
                continue

            net_inflow = float(exit_qty) * float(rec.filled_price) - float(rec.fee_tax_krw)  # type: ignore[arg-type]
            ep["remaining_qty"] = int(ep["remaining_qty"]) - exit_qty
            ep["exit_proceeds_net_krw"] = float(ep["exit_proceeds_net_krw"]) + net_inflow
            ep["exit_fill_records"].append(rec.record_id)
            ep["all_compliant"] = bool(ep["all_compliant"] and is_compliant)

            if int(ep["remaining_qty"]) == 0:
                open_by_ep.pop(target_ep_id)
                open_ep_by_code.pop(code_norm, None)
                net_pnl = float(ep["exit_proceeds_net_krw"]) - float(ep["entry_cost_krw"])
                closed_episodes.append(
                    {
                        "episode_id": ep["episode_id"],
                        "code": ep["code"],
                        "region": ep["region"],
                        "sector": ep["sector"],
                        "entry_record_id": ep["entry_record_id"],
                        "exit_fill_record_ids": list(ep["exit_fill_records"]),
                        "entry_qty": ep["entry_qty"],
                        "entry_cost_krw": ep["entry_cost_krw"],
                        "exit_proceeds_net_krw": ep["exit_proceeds_net_krw"],
                        "net_pnl_krw": net_pnl,
                        "net_return_pct": (
                            net_pnl / float(ep["entry_cost_krw"])
                            if float(ep["entry_cost_krw"]) > 0.0
                            else 0.0
                        ),
                        "exit_filled_at": fil_ts.isoformat(),
                        "all_compliant": ep["all_compliant"],
                    }
                )

    open_episodes = [
        {
            "episode_id": ep["episode_id"],
            "code": ep["code"],
            "entry_qty": ep["entry_qty"],
            "remaining_qty": ep["remaining_qty"],
            "partial_exit_count": len(ep["exit_fill_records"]),
            "all_compliant": ep["all_compliant"],
        }
        for ep in open_by_ep.values()
    ]
    return {
        "ledger_consistent": len(inconsistencies) == 0,
        "inconsistencies": inconsistencies,
        "closed_episodes": closed_episodes,
        "open_episodes": open_episodes,
    }


def _load_sidecar_calendars(journal_path: Path) -> dict[str, TradingCalendar]:
    sidecar = _journal_calendar_sidecar_path(journal_path)
    if not sidecar.is_file():
        return {}
    doc = json.loads(sidecar.read_text(encoding="utf-8"))
    raw_map = doc.get("calendars_by_sha256")
    if not isinstance(raw_map, dict):
        return {}
    out: dict[str, TradingCalendar] = {}
    for expected_sha, cdoc in raw_map.items():
        if not isinstance(cdoc, dict) or "scheduled_sessions" not in cdoc:
            continue
        cal_obj = TradingCalendar(
            cdoc["scheduled_sessions"],
            calendar_id=str(cdoc.get("calendar_id", "UNSPECIFIED_CALENDAR")),
            source=str(cdoc.get("source", "unspecified")),
            covered_start=cdoc.get("covered_start"),
            covered_end=cdoc.get("covered_end"),
            is_exchange_verified=bool(cdoc.get("is_exchange_verified", False)),
        )
        if cal_obj.sha256() != expected_sha:
            raise ValueError(
                f"E1_JOURNAL_CALENDAR_SIDECAR_HASH_MISMATCH:{expected_sha}!={cal_obj.sha256()}"
            )
        out[expected_sha] = cal_obj
    return out


def _persist_sidecar_calendar(journal_path: Path, calendar: TradingCalendar) -> None:
    sidecar = _journal_calendar_sidecar_path(journal_path)
    cal_sha = calendar.sha256()
    if sidecar.is_file():
        doc = json.loads(sidecar.read_text(encoding="utf-8"))
        cal_map = dict(doc.get("calendars_by_sha256") or {})
    else:
        cal_map = {}
    cal_map[cal_sha] = calendar.to_serializable_dict()
    payload = {
        "spec_version": SPEC_VERSION,
        "latest_calendar_sha256": cal_sha,
        "calendars_by_sha256": cal_map,
    }
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_name(sidecar.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(sidecar)


def load_e1_journal(
    journal_path: Path,
    calendar: TradingCalendar | None = None,
) -> list[E1JournalRecord]:
    path = Path(journal_path)
    if not path.exists():
        return []
    sidecar_cals = _load_sidecar_calendars(path)
    records: list[E1JournalRecord] = []
    seen_ids: set[str] = set()
    seen_entry_signals: set[tuple[str, str]] = set()

    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        obj = json.loads(stripped)
        obj.pop("spec_version", None)
        saved_status = obj.pop("compliance_status", None)
        saved_violations = tuple(obj.pop("protocol_violations", None) or ())
        rec_cal_id = obj.pop("recorded_calendar_id", None)
        rec_cal_sha = obj.pop("recorded_calendar_sha256", None)
        rec_expected_next = obj.pop("recorded_expected_next_session_date", None)
        restored_cal = sidecar_cals.get(rec_cal_sha) if isinstance(rec_cal_sha, str) else None

        rec = E1JournalRecord(
            **obj,
            recorded_compliance_status=saved_status,
            recorded_protocol_violations=saved_violations,
            recorded_calendar_id=rec_cal_id,
            recorded_calendar_sha256=rec_cal_sha,
            recorded_expected_next_session_date=rec_expected_next,
            _restored_calendar=restored_cal,
        )
        eff_cal = calendar if calendar is not None else restored_cal
        rec.validate(calendar=eff_cal)
        if rec.record_id in seen_ids:
            raise ValueError(f"DUPLICATE_E1_RECORD_ID:line={line_no}:{rec.record_id}")
        seen_ids.add(rec.record_id)

        if rec.action_type == "ENTRY":
            sig_key = rec.canonical_entry_key()
            if sig_key in seen_entry_signals:
                raise ValueError(
                    f"DUPLICATE_ENTRY_CHECK_FOR_SAME_SIGNAL_PROHIBITED:line={line_no}:{sig_key}"
                )
            seen_entry_signals.add(sig_key)
        records.append(rec)

    ledger = reconcile_e1_position_ledger(records, calendar=calendar)
    if not ledger["ledger_consistent"]:
        raise ValueError(
            f"E1_POSITION_LEDGER_INCONSISTENT:{'|'.join(ledger['inconsistencies'])}"
        )
    return records


def append_e1_journal_record(
    journal_path: Path,
    record: E1JournalRecord,
    calendar: TradingCalendar | None = None,
) -> str:
    """Validate and append a single E1 record to an append-only JSONL journal."""
    record.validate(calendar=calendar)
    path = Path(journal_path)
    existing = load_e1_journal(path, calendar=calendar)
    if any(r.record_id == record.record_id for r in existing):
        raise ValueError(f"DUPLICATE_E1_RECORD_ID:{record.record_id}")
    if record.action_type == "ENTRY":
        cand_key = record.canonical_entry_key()
        if any(r.action_type == "ENTRY" and r.canonical_entry_key() == cand_key for r in existing):
            raise ValueError(
                f"SECOND_ENTRY_ATTEMPT_AFTER_SINGLE_CHECK_PROHIBITED:{cand_key[0]}:{cand_key[1]}"
            )

    ledger = reconcile_e1_position_ledger([*existing, record], calendar=calendar)
    if not ledger["ledger_consistent"]:
        raise ValueError(
            f"E1_POSITION_LEDGER_INCONSISTENT:{'|'.join(ledger['inconsistencies'])}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    if calendar is not None:
        _persist_sidecar_calendar(path, calendar)
    line_bytes = canonical_json_bytes(record.to_dict(calendar=calendar)) + b"\n"
    with path.open("ab") as fh:
        fh.write(line_bytes)
    return sha256_bytes(path.read_bytes())


def summarize_e1_journal(
    records: Sequence[E1JournalRecord],
    calendar: TradingCalendar | None = None,
) -> dict:
    """Summarize E1 forward journal with share-ledger reconciliation & strict anti-overclaim gate (§6, §9).

    `profitability_claim_allowed` is ALWAYS `False` in journal summaries: accumulating >=24 months and
    >=60 compliant closed episodes only makes the ledger eligible to begin formal benchmark/bootstrap/
    stress statistical evaluation (`statistical_evaluation_eligible`), never proving profitability by
    span and row count alone.
    """
    if not records:
        return {
            "record_count": 0,
            "span_weeks": 0.0,
            "span_months_approx": 0.0,
            "disposition_counts": {},
            "real_entry_fills": 0,
            "compliant_entry_fills": 0,
            "violation_entry_fills": 0,
            "unverified_entry_fills": 0,
            "real_exit_fills": 0,
            "closed_episode_count": 0,
            "compliant_closed_episode_count": 0,
            "open_episode_count": 0,
            "protocol_violation_record_count": 0,
            "unverified_record_count": 0,
            "prior_recorded_violation_record_count": 0,
            "compliance_audit_trail": [],
            "virtual_observations": 0,
            "ledger_consistent": True,
            "ledger_inconsistencies": [],
            "realized_net_pnl_krw_total": 0.0,
            "evaluation_stage": "EMPTY_JOURNAL",
            "statistical_evaluation_eligible": False,
            "profitability_claim_allowed": False,
        }

    disp_counts: dict[str, int] = {}
    compliant_entries = 0
    violation_entries = 0
    unverified_entries = 0
    violation_records = 0
    unverified_records = 0
    prior_recorded_violations = 0
    audit_trail: list[dict] = []

    for r in records:
        audit = r.audit_compliance(calendar=calendar)
        audit_trail.append(audit)
        comp_status = str(audit["effective_compliance_status"])
        disp_counts[r.disposition] = disp_counts.get(r.disposition, 0) + 1
        if (
            r.recorded_compliance_status == "PROTOCOL_VIOLATION_PRESERVED"
            or any(
                v not in _UNVERIFIED_COMPLIANCE_REASONS
                for v in (r.recorded_protocol_violations or ())
            )
        ):
            prior_recorded_violations += 1
        if comp_status == "PROTOCOL_VIOLATION_PRESERVED":
            violation_records += 1
        elif comp_status != "COMPLIANT":
            unverified_records += 1

        if r.action_type == "ENTRY" and r.disposition == "FILLED":
            if comp_status == "COMPLIANT":
                compliant_entries += 1
            elif comp_status == "PROTOCOL_VIOLATION_PRESERVED":
                violation_entries += 1
            else:
                unverified_entries += 1

    ledger = reconcile_e1_position_ledger(records, calendar=calendar)
    dates = [pd.Timestamp(normalize_e1_date(r.check_session_date)) for r in records]
    span_days = max(0, int((max(dates) - min(dates)).days))
    span_weeks = span_days / 7.0
    span_months = span_days / 30.4375

    real_entries = disp_counts.get("FILLED", 0)
    real_exits = disp_counts.get("EXIT_FILLED", 0)
    virtual_obs = disp_counts.get("VIRTUAL_OBSERVATION_NO_ORDER", 0) + disp_counts.get(
        "EXIT_VIRTUAL_OBSERVATION_NO_ORDER", 0
    )

    closed_eps = ledger["closed_episodes"]
    compliant_closed_eps = [ep for ep in closed_eps if ep["all_compliant"]]
    realized_net_pnl_total = float(sum(float(ep["net_pnl_krw"]) for ep in closed_eps))

    if not ledger["ledger_consistent"]:
        stage = "LEDGER_INCONSISTENT_INVALID_FOR_EVALUATION"
        stat_eligible = False
    elif span_months < 24.0 or len(compliant_closed_eps) < 60:
        stage = "CONSISTENCY_CHECK_ONLY_NO_PROFITABILITY_CLAIM"
        stat_eligible = False
    else:
        stage = "ELIGIBLE_FOR_E1_STATISTICAL_EVALUATION_NOT_PROFITABILITY_PROOF"
        stat_eligible = True

    return {
        "record_count": len(records),
        "span_weeks": round(span_weeks, 3),
        "span_months_approx": round(span_months, 3),
        "disposition_counts": disp_counts,
        "real_entry_fills": real_entries,
        "compliant_entry_fills": compliant_entries,
        "violation_entry_fills": violation_entries,
        "unverified_entry_fills": unverified_entries,
        "real_exit_fills": real_exits,
        "closed_episode_count": len(closed_eps),
        "compliant_closed_episode_count": len(compliant_closed_eps),
        "open_episode_count": len(ledger["open_episodes"]),
        "protocol_violation_record_count": violation_records,
        "unverified_record_count": unverified_records,
        "prior_recorded_violation_record_count": prior_recorded_violations,
        "compliance_audit_trail": audit_trail,
        "virtual_observations": virtual_obs,
        "ledger_consistent": bool(ledger["ledger_consistent"]),
        "ledger_inconsistencies": ledger["inconsistencies"],
        "realized_net_pnl_krw_total": realized_net_pnl_total,
        "evaluation_stage": stage,
        "statistical_evaluation_eligible": stat_eligible,
        "profitability_claim_allowed": False,
    }
