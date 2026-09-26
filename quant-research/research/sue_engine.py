"""
T-094 exploratory year-over-year EPS change engine.

Uses a current zero-auth finance snapshot; this is not point-in-time SUE:
1. Expected EPS = Prior Year Same-Quarter EPS (Foster III Seasonal Model baseline)
2. Delta EPS = Actual EPS_q - EPS_(q-4)
3. Event Pool Lifecycle: UNOBSERVED -> ACTIVE -> EXPIRED (>60 trading days) / SUPERSEDED
4. Stored D+1/D+2 fields are calendar-date approximations, not execution dates.
5. Cohort Analysis: 0~20d, 21~40d, 41~60d elapsed trading days.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

DEFAULT_RAW_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "fundamental" / "quarterly_sue_raw.json"


@dataclass
class SueEvent:
    code: str
    period: str                 # e.g., '202606'
    prior_period: str           # e.g., '202506'
    actual_eps: float
    prior_eps: float
    delta_eps: float
    pct_change: float           # (actual - prior) / abs(prior) * 100
    is_turnaround: bool         # Turned positive from negative
    disclosure_date: str        # 'YYYY-MM-DD'
    signal_date: str            # D+1 calendar day 'YYYY-MM-DD'; not a fill rule
    entry_date: str             # D+2 calendar day 'YYYY-MM-DD'; not a fill rule
    is_surprise: bool           # delta_eps > 0; not standardized SUE
    raw_title: Optional[str] = None


class SueEngine:
    """Zero-auth EPS growth proxy and indicative event lifecycle tracker."""

    def __init__(self, raw_data_path: Path | str = DEFAULT_RAW_DATA_PATH):
        self.raw_data_path = Path(raw_data_path)
        self.raw_data: Dict[str, Any] = {}
        self.events_by_code: Dict[str, List[SueEvent]] = {}
        self.load_data()

    def load_data(self):
        if self.raw_data_path.exists():
            with open(self.raw_data_path, "r", encoding="utf-8") as f:
                self.raw_data = json.load(f)
            self._build_events()

    @staticmethod
    def _prior_year_period(period_str: str) -> Optional[str]:
        """Convert YYYYMM to (YYYY-1)MM."""
        try:
            val = int(period_str)
            year = val // 100
            month = val % 100
            return f"{year - 1:04d}{month:02d}"
        except Exception:
            return None

    @staticmethod
    def _default_disclosure_date(period_str: str) -> str:
        """Conservative statutory reporting deadline if disclosure not matched."""
        try:
            year = int(period_str[:4])
            month = period_str[4:6]
            if month == "03":
                return f"{year}-05-15"
            elif month == "06":
                return f"{year}-08-14"
            elif month == "09":
                return f"{year}-11-14"
            elif month == "12":
                return f"{year + 1}-03-31"
        except Exception:
            pass
        return "2099-12-31"

    def _find_disclosure_date(self, period_str: str, disclosures: List[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
        """Find an unverified first keyword disclosure in a broad quarter window."""
        try:
            year = int(period_str[:4])
            month = period_str[4:6]

            # Approximate reporting window: quarter end to +75 days
            if month == "03":
                min_dt, max_dt = f"{year}-04-01", f"{year}-05-20"
            elif month == "06":
                min_dt, max_dt = f"{year}-07-01", f"{year}-08-20"
            elif month == "09":
                min_dt, max_dt = f"{year}-10-01", f"{year}-11-20"
            elif month == "12":
                min_dt, max_dt = f"{year + 1}-01-01", f"{year + 1}-04-05"
            else:
                return self._default_disclosure_date(period_str), None

            matched = []
            for d in disclosures:
                dt_str = (d.get("datetime") or "")[:10]
                if min_dt <= dt_str <= max_dt:
                    matched.append((dt_str, d.get("title", "")))

            if matched:
                matched.sort(key=lambda x: x[0])
                return matched[0][0], matched[0][1]
        except Exception:
            pass

        return self._default_disclosure_date(period_str), None

    def _build_events(self):
        """Construct exploratory EPS change events from a current snapshot."""
        for code, stock_info in self.raw_data.items():
            quarters = stock_info.get("quarters", [])
            disclosures = stock_info.get("disclosures", [])

            # Map periods to actual quarters (ignore consensus rows)
            actual_quarters = {q["period"]: q for q in quarters if not q.get("is_consensus") and q.get("eps") is not None}

            events: List[SueEvent] = []
            for period, q in sorted(actual_quarters.items()):
                prior_p = self._prior_year_period(period)
                if not prior_p or prior_p not in actual_quarters:
                    continue

                prior_q = actual_quarters[prior_p]
                actual_eps = float(q["eps"])
                prior_eps = float(prior_q["eps"])
                delta_eps = actual_eps - prior_eps

                if prior_eps == 0:
                    pct_change = 100.0 if actual_eps > 0 else (-100.0 if actual_eps < 0 else 0.0)
                else:
                    pct_change = (delta_eps / abs(prior_eps)) * 100.0

                is_turnaround = (prior_eps <= 0 and actual_eps > 0)

                disc_date, disc_title = self._find_disclosure_date(period, disclosures)

                # Conservative timeline:
                # D: Disclosure
                # D+1: Signal confirmed at close (T+1 calendar day approx)
                # D+2: Fill at open (T+2 calendar day approx)
                disc_dt = pd.Timestamp(disc_date)
                signal_dt = disc_dt + pd.Timedelta(days=1)
                entry_dt = disc_dt + pd.Timedelta(days=2)

                event = SueEvent(
                    code=code,
                    period=period,
                    prior_period=prior_p,
                    actual_eps=actual_eps,
                    prior_eps=prior_eps,
                    delta_eps=delta_eps,
                    pct_change=round(pct_change, 2),
                    is_turnaround=is_turnaround,
                    disclosure_date=disc_date,
                    signal_date=signal_dt.strftime("%Y-%m-%d"),
                    entry_date=entry_dt.strftime("%Y-%m-%d"),
                    is_surprise=(delta_eps > 0 or is_turnaround),
                    raw_title=disc_title
                )
                events.append(event)

            self.events_by_code[code] = sorted(events, key=lambda e: e.disclosure_date)

    def get_events_for_stock(self, code: str) -> List[SueEvent]:
        return self.events_by_code.get(code, [])

    def get_active_event_on(
        self,
        code: str,
        eval_date: pd.Timestamp,
        calendar: Optional[pd.DatetimeIndex] = None,
        max_holding_bars: int = 60
    ) -> Dict[str, Any]:
        """
        Evaluate the stored event dates; caller must independently gate snapshot
        collection time and verify that EPS was actually disclosed then.

        Returns dict with keys:
          - status: 'UNOBSERVED' | 'ACTIVE' | 'EXPIRED' | 'SUPERSEDED'
          - event: Optional[SueEvent]
          - days_elapsed: int (trading days since disclosure)
          - cohort: '0_20d' | '21_40d' | '41_60d' | 'expired' | 'none'
          - is_surprise: bool
        """
        events = self.events_by_code.get(code, [])
        if not events:
            return {
                "status": "UNOBSERVED",
                "event": None,
                "days_elapsed": 0,
                "cohort": "none",
                "is_surprise": False,
            }

        eval_str = eval_date.strftime("%Y-%m-%d")

        # Disclosed on or before eval_date (Point-in-Time filter)
        known_events = [e for e in events if e.disclosure_date <= eval_str]
        if not known_events:
            return {
                "status": "UNOBSERVED",
                "event": None,
                "days_elapsed": 0,
                "cohort": "none",
                "is_surprise": False,
            }

        # Most recent disclosed event
        latest_event = known_events[-1]

        # Calculate trading days elapsed
        if calendar is not None:
            # Count trading days between disclosure_date and eval_date
            mask = (calendar >= pd.Timestamp(latest_event.disclosure_date)) & (calendar <= eval_date)
            days_elapsed = int(mask.sum()) - 1
            days_elapsed = max(days_elapsed, 0)
        else:
            # Approximate calendar days * (5/7)
            delta_days = (eval_date - pd.Timestamp(latest_event.disclosure_date)).days
            days_elapsed = max(int(delta_days * 5 / 7), 0)

        # Check status and cohort
        if days_elapsed > max_holding_bars:
            status = "EXPIRED"
            cohort = "expired"
        else:
            status = "ACTIVE"
            if days_elapsed <= 20:
                cohort = "0_20d"
            elif days_elapsed <= 40:
                cohort = "21_40d"
            else:
                cohort = "41_60d"

        return {
            "status": status,
            "event": latest_event,
            "days_elapsed": days_elapsed,
            "cohort": cohort,
            "is_surprise": latest_event.is_surprise,
            "pct_change": latest_event.pct_change,
            "delta_eps": latest_event.delta_eps,
        }

    def evaluate_universe_on(
        self,
        codes: List[str],
        eval_date: pd.Timestamp,
        calendar: Optional[pd.DatetimeIndex] = None
    ) -> pd.DataFrame:
        """Evaluate SUE event status for a universe of stocks on eval_date."""
        rows = []
        for code in codes:
            res = self.get_active_event_on(code, eval_date, calendar)
            ev = res["event"]
            rows.append({
                "code": code,
                "status": res["status"],
                "cohort": res["cohort"],
                "days_elapsed": res["days_elapsed"],
                "is_surprise": res["is_surprise"],
                "pct_change": res.get("pct_change", 0.0),
                "delta_eps": res.get("delta_eps", 0.0),
                "period": ev.period if ev else None,
                "disclosure_date": ev.disclosure_date if ev else None,
            })
        return pd.DataFrame(rows)
