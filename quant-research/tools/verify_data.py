"""Offline integrity report for the imported hourly-bar snapshot."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "imported" / "hourly_pattern"


def verify_data(base_dir: Path) -> tuple[dict, list[str]]:
    files = sorted(base_dir.glob("bars_*.csv"))
    summary = {"files": len(files), "rows": 0, "earliest": None, "latest": None, "timezone": None}
    issues = []
    minimums, maximums, timezones = [], [], set()
    required = ("open", "high", "low", "close", "volume")
    for path in files:
        try:
            frame = pd.read_csv(path)
            columns = {str(column).lower(): column for column in frame.columns}
            missing = [name for name in required if name not in columns]
            if missing:
                issues.append(f"{path.name}: missing columns {missing}")
                continue
            datetime_column = next((columns[name] for name in ("datetime", "date", "timestamp", "unnamed: 0") if name in columns), None)
            if datetime_column is None:
                issues.append(f"{path.name}: missing datetime column")
                continue
            timestamps = pd.to_datetime(frame[datetime_column], errors="coerce")
            values = frame[[columns[name] for name in required]].apply(pd.to_numeric, errors="coerce")
            summary["rows"] += len(frame)
            if timestamps.isna().any():
                issues.append(f"{path.name}: {int(timestamps.isna().sum())} invalid timestamps")
            if values.isna().any(axis=1).any():
                issues.append(f"{path.name}: {int(values.isna().any(axis=1).sum())} invalid OHLCV rows")
            if timestamps.duplicated().any():
                issues.append(f"{path.name}: {int(timestamps.duplicated().sum())} duplicate timestamps")
            if not timestamps.is_monotonic_increasing:
                issues.append(f"{path.name}: timestamps are not increasing")
            if (values[columns["volume"]] < 0).any():
                issues.append(f"{path.name}: negative volume")
            prices = values[[columns[name] for name in required[:4]]]
            high, low = values[columns["high"]], values[columns["low"]]
            bad = ((prices <= 0).any(axis=1)
                   | (high < values[[columns["open"], columns["close"]]].max(axis=1))
                   | (low > values[[columns["open"], columns["close"]]].min(axis=1)))
            if bad.any():
                issues.append(f"{path.name}: {int(bad.sum())} invalid OHLC rows")
            valid_times = timestamps.dropna()
            if not valid_times.empty:
                minimums.append(valid_times.min())
                maximums.append(valid_times.max())
                timezones.add(str(valid_times.dt.tz))
        except (OSError, ValueError, TypeError) as exc:
            issues.append(f"{path.name}: {exc}")
    if not files:
        issues.append(f"no bars_*.csv files in {base_dir}")
    if minimums:
        summary["earliest"] = min(minimums).isoformat()
        summary["latest"] = max(maximums).isoformat()
    summary["timezone"] = sorted(timezones)
    if timezones != {"UTC+09:00"}:
        issues.append(f"expected fixed +09:00 timestamps, found {sorted(timezones)}")
    try:
        parsed = json.loads((base_dir / "summary.json").read_text(encoding="utf-8"))
        summary["summary_keys"] = sorted(parsed)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        issues.append(f"summary.json: {exc}")
    return summary, issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_DATA)
    args = parser.parse_args()
    summary, issues = verify_data(args.path.resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if issues:
        print("\n".join(f"ERROR: {issue}" for issue in issues), file=sys.stderr)
        return 1
    print("PASS: imported hourly bars satisfy structural integrity checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
