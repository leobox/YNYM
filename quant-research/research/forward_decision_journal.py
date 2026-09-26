"""T-096: seal a recent Mode 2 explorer decision with its exact input hashes.

This is a read-only observation journal. It neither books virtual fills nor
turns the T-094 EPS growth proxy into a validated SUE factor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.dynamic_sue_rag_explorer import DEFAULT_EPS_PATH, explore  # noqa: E402
from scripts.pure_quant_portfolio_manager import (  # noqa: E402
    DEFAULT_DATA_DIR, DEFAULT_STATE_FILE, PortfolioManager,
)

DEFAULT_RUN_DIR = ROOT / "data/research/T-096/runs"
MAX_PRICE_LAG_DAYS = 3  # Friday close remains eligible through Monday.


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def inspect(manager: PortfolioManager, eps_path: Path | str = DEFAULT_EPS_PATH,
            today: date | None = None) -> dict:
    """Inspect latest close and provenance without storing an observation."""
    today = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    price_dir = Path(manager.data_dir)
    price_files = sorted(price_dir.glob("*.csv")) if price_dir.is_dir() else []
    if not price_files:
        return {"ready": False, "reasons": ["PRICE_SOURCE_FILES_MISSING"],
                "as_of": None, "today_kst": today.isoformat(),
                "price_lag_calendar_days": None, "run_id": None,
                "source_hashes": {}, "decision": None}

    code_files = [Path(__file__),
                  ROOT / "research/dynamic_sue_rag_explorer.py",
                  ROOT / "research/sue_engine.py",
                  ROOT / "scripts/pure_quant_portfolio_manager.py",
                  ROOT / "scripts/rag_thesis_engine.py"]

    def fingerprint() -> dict:
        return {
            "prices": {p.name: sha256_file(p) for p in price_files},
            "eps_snapshot": sha256_file(Path(eps_path)) if Path(eps_path).is_file() else None,
            "portfolio_state": sha256_file(manager.state_file) if manager.state_file.is_file() else None,
            "rag_profile": sha256_file(manager.rag.kb_path) if manager.rag.kb_path.is_file() else None,
            "code": {str(p.relative_to(ROOT)).replace("\\", "/"): sha256_file(p)
                     for p in code_files},
        }

    before = fingerprint()
    # Always reload the very files whose hashes are recorded, even if the
    # caller has an old universe cached in memory.
    fresh_manager = PortfolioManager(state_file=str(manager.state_file), data_dir=str(price_dir),
                                     kb_path=manager.rag.kb_path)
    decision = explore(fresh_manager, eps_path=eps_path)
    source_hashes = fingerprint()
    as_of = date.fromisoformat(decision["as_of"])
    lag_days = (today - as_of).days
    reasons = []
    if before != source_hashes:
        reasons.append("SOURCE_CHANGED_DURING_CAPTURE")
    if lag_days < 0:
        reasons.append("PRICE_DATE_IN_FUTURE")
    if lag_days == 0:
        reasons.append("SAME_DAY_CLOSE_UNVERIFIED")
    if lag_days > MAX_PRICE_LAG_DAYS:
        reasons.append("PRICE_SNAPSHOT_STALE")
    if decision["plan_status"] == "DATA_INCOMPLETE":
        reasons.append("HELD_PRICE_INCOMPLETE")
    # The run id changes if a factor, decision, EPS cache, state, or source
    # implementation changes. It is independent of wall-clock retry time.
    run_id = hashlib.sha256(_canonical({"as_of": decision["as_of"],
                                        "source_hashes": source_hashes,
                                        "decision": decision})).hexdigest()[:20]
    return {"ready": not reasons, "reasons": reasons,
            "as_of": decision["as_of"], "today_kst": today.isoformat(),
            "price_lag_calendar_days": lag_days,
            "run_id": run_id, "source_hashes": source_hashes,
            "decision": decision}


def capture(manager: PortfolioManager, eps_path: Path | str = DEFAULT_EPS_PATH,
            output_dir: Path | str = DEFAULT_RUN_DIR, today: date | None = None) -> dict:
    """Create one immutable JSON record, or return the prior identical run."""
    finding = inspect(manager, eps_path=eps_path, today=today)
    if not finding["ready"]:
        raise ValueError("Cannot seal forward observation: " + ", ".join(finding["reasons"]))
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{finding['run_id']}.json"
    if target.exists():
        prior = json.loads(target.read_text(encoding="utf-8"))
        if prior.get("run_id") != finding["run_id"] or prior.get("source_hashes") != finding["source_hashes"]:
            raise ValueError("Existing run id conflicts with current sources")
        return {"run_id": finding["run_id"], "path": str(target), "reused": True}

    record = {"schema_version": 1, "run_id": finding["run_id"],
              "captured_at_utc": datetime.now(timezone.utc).isoformat(),
              "as_of": finding["as_of"], "price_lag_calendar_days": finding["price_lag_calendar_days"],
              "source_hashes": finding["source_hashes"],
              "decision": finding["decision"],
              "note": "EPS growth proxy is not standardized SUE; no earnings score was applied."}
    payload = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=out_dir, prefix=".t096-", delete=False) as stream:
            temp_name = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link installs the completed file only if the name does not
        # exist. A concurrent identical run can then safely reuse its record.
        try:
            os.link(temp_name, target)
        except FileExistsError:
            prior = json.loads(target.read_text(encoding="utf-8"))
            if prior.get("run_id") != finding["run_id"] or prior.get("source_hashes") != finding["source_hashes"]:
                raise ValueError("Concurrent run id conflicts with current sources")
            return {"run_id": finding["run_id"], "path": str(target), "reused": True}
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
    return {"run_id": finding["run_id"], "path": str(target), "reused": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Mode 2 forward decision journal")
    parser.add_argument("--capture", action="store_true", help="Seal the latest eligible completed close")
    parser.add_argument("--data", default=DEFAULT_DATA_DIR)
    parser.add_argument("--state", default=DEFAULT_STATE_FILE)
    parser.add_argument("--eps-data", default=str(DEFAULT_EPS_PATH))
    parser.add_argument("--out", default=str(DEFAULT_RUN_DIR))
    args = parser.parse_args()
    manager = PortfolioManager(state_file=args.state, data_dir=args.data)
    try:
        if args.capture:
            result = capture(manager, args.eps_data, args.out)
        else:
            finding = inspect(manager, args.eps_data)
            result = {key: finding[key] for key in
                      ("ready", "reasons", "as_of", "today_kst", "price_lag_calendar_days", "run_id")}
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
