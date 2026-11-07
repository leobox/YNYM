"""Fetch public KRX ETF daily bars, seal daily T-145 v1.0 RS20 observations, and update README.md.

Safety invariants:
- Read-only public market data queries (`etfItemList.nhn`, `fchart.stock.naver.com/sise.nhn`) ONLY.
- Zero order/trading/account APIs.
- Explicit finite timeout `(5, 15)` and bounded 3-attempt exponential backoff on every HTTP request.
- Idempotent daily sealing: once `runs/<as_of>.json` is sealed with `status == "OK"`, re-running on
  the same completed bar date preserves the sealed JSON untouched while refreshing `latest.md` / `README.md`.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "quant-research") not in sys.path:
    sys.path.insert(0, str(ROOT / "quant-research"))

from research.etf_observer import parse_daily  # noqa: E402
from research.sector_v1 import SPEC_TASK_ID, SPEC_VERSION  # noqa: E402
from research.sector_v1.public_observer import (  # noqa: E402
    KST,
    OBSERVATION_SCHEMA_VERSION,
    PUBLIC_MAPPING_VERSION,
    build_frozen_public_sector_mapping,
    evaluate_public_sector_snapshot,
    is_eligible_kr_common_constituent,
    render_sector_v1_panel,
    update_sector_v1_readme,
)

LIST_URL = "https://finance.naver.com/api/sise/etfItemList.nhn"
CHART_URL = "https://fchart.stock.naver.com/sise.nhn"
CONSTITUENT_URL_TMPL = "https://m.stock.naver.com/api/stock/{code}/etfAnalysis"


def parse_constituent_payload(payload: bytes, excluded_etf_codes: set[str]) -> list[dict[str, str]]:
    """Parse public ETF top constituent JSON into eligible KRX common-stock `{code, name}` rows."""
    doc = json.loads(payload.decode("utf-8"))
    items = doc.get("etfTop10MajorConstituentAssets") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise ValueError("CONSTITUENT_SCHEMA_MISSING")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("itemCode") or "").strip()
        name = str(item.get("itemName") or "").strip()
        if code in seen or not is_eligible_kr_common_constituent(code, name, excluded_etf_codes):
            continue
        seen.add(code)
        out.append({"code": code, "name": name})
    return out


def fetch_public_bytes(url: str, params: dict | None = None) -> bytes:
    """Fetch public market payload with explicit finite timeout `(5, 15)` and 3x exponential backoff."""
    last_err = "UNKNOWN"
    for attempt in range(3):
        try:
            resp = requests.get(
                url,
                params=params,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=(5, 15),
            )
            resp.raise_for_status()
            if len(resp.content) > 8_000_000:
                raise ValueError("RESPONSE_TOO_LARGE")
            return resp.content
        except (requests.RequestException, ValueError) as exc:
            last_err = type(exc).__name__
            if attempt < 2:
                time.sleep(1.0 * (2**attempt))
    raise RuntimeError(f"PUBLIC_DATA_FETCH_FAILED:{last_err}")


def atomic_dump_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def classify_symbol_coverage(
    frames: dict[str, pd.DataFrame],
    requested_count: int,
    now_kst: datetime,
) -> tuple[str, str | None, list[str]]:
    if requested_count <= 0:
        return "ERROR", None, []
    dates = {code: str(df.index[-1].date()) for code, df in frames.items() if len(df) > 0}
    if not dates:
        return "ERROR", None, []
    counts = pd.Series(list(dates.values())).value_counts()
    as_of = max(counts.index, key=lambda d: (int(counts[d]), d))
    fresh = sorted(code for code, dt in dates.items() if dt == as_of)
    if (now_kst.astimezone(KST).date() - pd.Timestamp(as_of).date()).days > 7:
        return "STALE", as_of, fresh
    if (len(fresh) / requested_count) < 0.90:
        return "INSUFFICIENT_COVERAGE", as_of, fresh
    return ("OK" if len(fresh) == requested_count else "PARTIAL"), as_of, fresh


def collect_sector_v1_observation(
    output_dir: Path,
    raw_archive_dir: Path | None = None,
    bar_count: int = 260,
) -> dict:
    started = datetime.now(KST)
    run_id = started.strftime("%Y%m%dT%H%M%S%f")
    output_dir = Path(output_dir)
    runs_dir = output_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    eff_raw_root = Path(raw_archive_dir) if raw_archive_dir is not None else None
    if eff_raw_root is not None:
        raw_run_dir = eff_raw_root / run_id
        raw_run_dir.mkdir(parents=True, exist_ok=True)
    else:
        raw_run_dir = None

    mapping = build_frozen_public_sector_mapping()
    symbols: list[dict[str, str]] = []
    for region, bspec in sorted(mapping.benchmarks.items()):
        symbols.append(
            {"code": bspec.code, "name": bspec.name, "role": f"BENCHMARK:{region}"}
        )
    for (region, sector), spec in sorted(mapping.representatives.items()):
        symbols.append(
            {"code": spec.code, "name": spec.name, "role": f"SECTOR:{region}:{sector}"}
        )

    inputs_meta: list[dict] = []
    errors: list[dict] = []
    frames: dict[str, pd.DataFrame] = {}
    quality: dict[str, dict] = {}
    constituent_quality: dict[str, dict] = {}

    def record_payload_digest(name: str, payload: bytes, extra: dict) -> dict:
        digest = {
            "name": name,
            "raw_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            **extra,
        }
        if raw_run_dir is not None:
            compressed = gzip.compress(payload, mtime=0)
            target = raw_run_dir / f"{name}.gz"
            target.write_bytes(compressed)
            digest["archived_gzip_sha256"] = hashlib.sha256(compressed).hexdigest()
        return digest

    record: dict = {
        "spec_task_id": SPEC_TASK_ID,
        "spec_version": SPEC_VERSION,
        "mapping_version": PUBLIC_MAPPING_VERSION,
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "run_id": run_id,
        "fetched_at": started.isoformat(timespec="seconds"),
        "requested_symbol_count": len(symbols),
        "fresh_symbol_count": 0,
        "as_of": None,
        "status": "ERROR",
    }

    try:
        # Optional ETF list verification so delisted/renamed codes are flagged in provenance
        list_payload = fetch_public_bytes(LIST_URL)
        inputs_meta.append(
            record_payload_digest(
                "etf_list.json",
                list_payload,
                {"url": LIST_URL, "received_at": datetime.now(KST).isoformat(timespec="seconds")},
            )
        )
        list_doc = json.loads(list_payload.decode("euc-kr"))
        listed_codes = {
            str(item.get("itemcode", "")).strip()
            for item in (list_doc.get("result", {}).get("etfItemList") or [])
        }
        unlisted_frozen = sorted(s["code"] for s in symbols if s["code"] not in listed_codes)

        def fetch_one(sym: dict[str, str]):
            code = sym["code"]
            params = {"symbol": code, "timeframe": "day", "count": bar_count, "requestType": 0}
            data = fetch_public_bytes(CHART_URL, params=params)
            meta = record_payload_digest(
                f"{code}.xml",
                data,
                {
                    "url": CHART_URL,
                    "symbol": code,
                    "count": bar_count,
                    "received_at": datetime.now(KST).isoformat(timespec="seconds"),
                },
            )
            try:
                frame, q = parse_daily(data, code, started)
                return code, frame, q, meta, None
            except Exception as exc:
                return code, None, None, meta, f"{type(exc).__name__}:{str(exc)[:120]}"

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fetch_one, s): s["code"] for s in symbols}
            for fut in as_completed(futures):
                code = futures[fut]
                try:
                    c_out, frame, q, meta, err = fut.result()
                    inputs_meta.append(meta)
                    if err:
                        errors.append({"code": c_out, "error": err})
                    elif frame is not None and q is not None:
                        frames[c_out] = frame
                        quality[c_out] = q
                except Exception as exc:
                    errors.append({"code": code, "error": f"{type(exc).__name__}:{str(exc)[:120]}"})

        status, as_of, fresh = classify_symbol_coverage(frames, len(symbols), started)
        record.update(
            status=status,
            as_of=as_of,
            fresh_symbol_count=len(fresh),
            unlisted_frozen_codes=unlisted_frozen,
        )
        if status in ("OK", "PARTIAL") and as_of is not None:
            # KR sector constituents (`C`): contemporaneous public ETF top holdings snapshot.
            # Failures degrade per sector (never fabricated) and never block the ETF panel.
            excluded_etf_codes = set(listed_codes) | {s["code"] for s in symbols}
            sector_constituents: dict[tuple[str, str], list[dict[str, str]]] = {}
            for (region, sector), spec in sorted(mapping.representatives.items()):
                if region != "KR":
                    continue
                try:
                    c_payload = fetch_public_bytes(CONSTITUENT_URL_TMPL.format(code=spec.code))
                    inputs_meta.append(
                        record_payload_digest(
                            f"constituents_{spec.code}.json",
                            c_payload,
                            {
                                "url": CONSTITUENT_URL_TMPL.format(code=spec.code),
                                "sector": sector,
                                "received_at": datetime.now(KST).isoformat(timespec="seconds"),
                            },
                        )
                    )
                    sector_constituents[(region, sector)] = parse_constituent_payload(
                        c_payload, excluded_etf_codes
                    )
                except Exception as exc:
                    errors.append(
                        {
                            "code": f"constituents:{spec.code}",
                            "error": f"{type(exc).__name__}:{str(exc)[:120]}",
                        }
                    )

            stock_codes = sorted(
                {m["code"] for members in sector_constituents.values() for m in members}
            )
            stock_frames: dict[str, pd.DataFrame] = {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(fetch_one, {"code": c, "name": c, "role": "KR_CONSTITUENT"}): c
                    for c in stock_codes
                }
                for fut in as_completed(futures):
                    code = futures[fut]
                    try:
                        c_out, frame, q, meta, err = fut.result()
                        inputs_meta.append(meta)
                        if err:
                            errors.append({"code": c_out, "error": err})
                        elif frame is not None and q is not None and len(frame) > 0:
                            # Zero-volume flat bars are non-traded sessions, not completed trades.
                            frame = frame.copy()
                            frame["Halted"] = frame["Volume"].astype(float) <= 0.0
                            stock_frames[c_out] = frame
                            constituent_quality[c_out] = q
                    except Exception as exc:
                        errors.append({"code": code, "error": f"{type(exc).__name__}:{str(exc)[:120]}"})

            record.update(
                requested_constituent_stock_count=len(stock_codes),
                fetched_constituent_stock_count=len(stock_frames),
                constituent_snapshot={
                    f"{reg}:{sec}": members for (reg, sec), members in sorted(sector_constituents.items())
                },
            )
            fresh_frames = {code: frames[code] for code in fresh}
            fresh_frames.update(
                {c: f for c, f in stock_frames.items() if c not in fresh_frames}
            )
            record["result"] = evaluate_public_sector_snapshot(
                raw_frames=fresh_frames,
                as_of=as_of,
                fetched_at=started,
                mapping=mapping,
                sector_constituents=sector_constituents,
                extra_excluded_etf_codes=excluded_etf_codes,
            )
    except Exception as exc:
        errors.append({"code": None, "error": f"{type(exc).__name__}:{str(exc)[:120]}"})
        record["status"] = "ERROR"

    code_paths = [
        Path(__file__).resolve(),
        ROOT / "quant-research/research/sector_v1/public_observer.py",
        ROOT / "quant-research/research/sector_v1/signals.py",
        ROOT / "quant-research/research/sector_v1/models.py",
        ROOT / "quant-research/research/sector_v1/execution_e0.py",
        ROOT / "quant-research/research/sector_v1/journal_e1.py",
    ]
    record.update(
        fetched_at=datetime.now(KST).isoformat(timespec="seconds"),
        completion_cutoff_clock=started.isoformat(timespec="seconds"),
        errors=sorted(errors, key=lambda e: e.get("code") or ""),
        data_quality=quality,
        constituent_data_quality=constituent_quality,
        inputs=sorted(inputs_meta, key=lambda i: i["name"]),
        provenance={
            "list_source": LIST_URL,
            "daily_source": CHART_URL,
            "constituent_source": CONSTITUENT_URL_TMPL,
            "price_unit": "KRW",
            "volume_unit": "shares",
            "total_return_verified": False,
            "corporate_actions_verified": False,
            "mapping_pit_verified": False,
            "constituents_pit_verified": False,
            "constituents_scope": "KR only; contemporaneous public ETF top-10 holdings snapshot on fetch date (not historical survivorship-free PIT). US individual stocks not implemented (§4).",
            "note": "Public KRX daily raw price bars for forward observation & E1 check band only; not total-return backtest proof.",
        },
        environment={
            "python": sys.version,
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "requests": requests.__version__,
        },
        code_sha256={
            p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in code_paths
        },
    )
    try:
        record["git_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        record["git_commit"] = None

    # Idempotent daily seal: preserve already-sealed `OK` snapshot for `as_of` without overwriting!
    as_of_val = record.get("as_of")
    if isinstance(as_of_val, str) and record["status"] in ("OK", "PARTIAL"):
        daily_seal_path = runs_dir / f"{as_of_val}.json"
        if daily_seal_path.is_file():
            existing = json.loads(daily_seal_path.read_text(encoding="utf-8"))
            # Reuse only an identical-schema `OK` seal; an older schema `OK` seal is upgraded only by
            # a fully `OK` new run so a degraded `PARTIAL` run never replaces a complete seal.
            schema_match = existing.get("observation_schema_version") == OBSERVATION_SCHEMA_VERSION
            if existing.get("status") == "OK" and (schema_match or record["status"] != "OK"):
                existing["idempotent_reused"] = True
                (output_dir / "latest.md").write_text(
                    render_sector_v1_panel(existing), encoding="utf-8"
                )
                return existing
        atomic_dump_json(daily_seal_path, record)

    (output_dir / "latest.md").write_text(render_sector_v1_panel(record), encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        "--out-dir",
        dest="output",
        type=Path,
        default=ROOT / "quant-research/data/research/T-150",
    )
    parser.add_argument("--raw-dir", type=Path, default=None)
    parser.add_argument("--readme", type=Path, default=None)
    parser.add_argument("--bar-count", type=int, default=260)
    parser.add_argument(
        "--render-only",
        type=Path,
        default=None,
        help="Render a previously captured JSON snapshot without network access.",
    )
    args = parser.parse_args(argv)
    if not 130 <= args.bar_count <= 600:
        parser.error("--bar-count must be in 130..600")

    if args.render_only is not None:
        record = json.loads(args.render_only.read_text(encoding="utf-8"))
    else:
        record = collect_sector_v1_observation(
            output_dir=args.output,
            raw_archive_dir=args.raw_dir,
            bar_count=args.bar_count,
        )

    panel_md = render_sector_v1_panel(record)
    if args.readme is not None:
        update_sector_v1_readme(args.readme, panel_md)

    print(
        json.dumps(
            {
                "run_id": record.get("run_id"),
                "status": record.get("status"),
                "as_of": record.get("as_of"),
                "requested_symbol_count": record.get("requested_symbol_count"),
                "fresh_symbol_count": record.get("fresh_symbol_count"),
                "idempotent_reused": bool(record.get("idempotent_reused", False)),
            },
            ensure_ascii=True,
        )
    )
    return 0 if record.get("status") in ("OK", "PARTIAL") else 1


if __name__ == "__main__":
    raise SystemExit(main())
