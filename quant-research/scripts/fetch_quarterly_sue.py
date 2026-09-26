"""
Fetch quarterly financial statements and disclosure timestamps without any API key.
Source: Naver Finance Mobile Endpoints (Zero Auth, Publicly Accessible)
Target: 145/150 KRX stocks in daily_3y universe.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DAILY_3Y_DIR = DATA_DIR / "daily_3y"
OUTPUT_DIR = DATA_DIR / "fundamental"


def clean_num(val_str: Optional[str]) -> Optional[float]:
    if not val_str or val_str in ("-", "N/A", "NaN", ""):
        return None
    try:
        return float(val_str.replace(",", "").strip())
    except ValueError:
        return None


def fetch_stock_financials(code: str, session: requests.Session) -> Dict[str, Any]:
    """Fetch quarterly financials and earnings disclosures for one ticker."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    # 1. Quarterly financial statements
    fin_url = f"https://m.stock.naver.com/api/stock/{code}/finance/quarter"
    quarters_data: List[Dict[str, Any]] = []

    try:
        r = session.get(fin_url, headers=headers, timeout=5)
        if r.status_code == 200:
            res = r.json()
            info = res.get("financeInfo", {})
            title_list = info.get("trTitleList", [])
            row_list = info.get("rowList", [])

            # Map row titles to data
            rows_by_title = {}
            for row in row_list:
                t = row.get("title", "")
                cols = row.get("columns", {})
                rows_by_title[t] = cols

            eps_cols = rows_by_title.get("EPS", {})
            rev_cols = rows_by_title.get("매출액", {})
            op_cols = rows_by_title.get("영업이익", {})
            net_cols = rows_by_title.get("당기순이익", {})

            for item in title_list:
                key = item.get("key")
                is_cons = item.get("isConsensus") == "Y"
                eps_val = clean_num(eps_cols.get(key, {}).get("value"))
                rev_val = clean_num(rev_cols.get(key, {}).get("value"))
                op_val = clean_num(op_cols.get(key, {}).get("value"))
                net_val = clean_num(net_cols.get(key, {}).get("value"))

                quarters_data.append({
                    "period": key,
                    "title": item.get("title"),
                    "is_consensus": is_cons,
                    "eps": eps_val,
                    "revenue": rev_val,
                    "operating_profit": op_val,
                    "net_profit": net_val,
                })
    except Exception as e:
        print(f"[{code}] Error fetching financials: {e}", file=sys.stderr)

    # 2. Earnings-related disclosures
    disc_url = f"https://m.stock.naver.com/api/stock/{code}/disclosure?pageSize=100&page=1"
    disclosures_data: List[Dict[str, Any]] = []

    try:
        r_disc = session.get(disc_url, headers=headers, timeout=5)
        if r_disc.status_code == 200:
            disc_items = r_disc.json()
            keywords = ["보고서", "영업실적", "잠정", "매출액", "기재정정"]
            for item in disc_items:
                title = item.get("title", "")
                if any(k in title for k in keywords):
                    disclosures_data.append({
                        "datetime": item.get("datetime"),
                        "title": title,
                        "disclosure_id": item.get("disclosureId"),
                    })
    except Exception as e:
        print(f"[{code}] Error fetching disclosures: {e}", file=sys.stderr)

    return {
        "code": code,
        "quarters": quarters_data,
        "disclosures": disclosures_data,
        "fetched_at": datetime.now().isoformat(),
    }


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    csv_files = sorted(glob.glob(str(DAILY_3Y_DIR / "*.csv")))
    codes = [os.path.splitext(os.path.basename(f))[0] for f in csv_files]
    print(f"Found {len(codes)} stocks in daily_3y universe.")

    results: Dict[str, Any] = {}
    session = requests.Session()

    success_count = 0
    fail_count = 0

    start_time = time.time()
    for idx, code in enumerate(codes, 1):
        try:
            data = fetch_stock_financials(code, session)
            if data["quarters"]:
                results[code] = data
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            print(f"Failed {code}: {e}")
            fail_count += 1

        if idx % 25 == 0 or idx == len(codes):
            print(f"Progress: {idx}/{len(codes)} ({idx/len(codes)*100:.1f}%) - Success: {success_count}, Fail: {fail_count}")
        time.sleep(0.05)  # respectful delay

    elapsed = round(time.time() - start_time, 2)
    print(f"Completed in {elapsed}s. Success: {success_count}, Fail: {fail_count}")

    # Save JSON data
    output_file = OUTPUT_DIR / "quarterly_sue_raw.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # Manifest
    raw_bytes = json.dumps(results, sort_keys=True).encode("utf-8")
    manifest = {
        "source": "naver_finance_mobile_public",
        "auth_required": False,
        "universe_size": len(codes),
        "success_count": success_count,
        "fail_count": fail_count,
        "coverage_pct": round(success_count / len(codes) * 100, 2),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "created_at": datetime.now().isoformat(),
        "elapsed_sec": elapsed,
    }

    manifest_file = OUTPUT_DIR / "manifest.json"
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"Saved to {output_file} and {manifest_file}")


if __name__ == "__main__":
    main()
