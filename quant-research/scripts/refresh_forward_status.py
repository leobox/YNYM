"""Publish an execution heartbeat without changing sealed daily decisions (stdlib only)."""

import argparse
from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfo

START = "<!-- DUAL_FORWARD:START -->"
END = "<!-- DUAL_FORWARD:END -->"
STATUS = {
    "CAPTURED": "정상 확인 · 새 완료 일봉 평가",
    "BACKFILLED": "정상 확인 · 누락 기록 보충",
    "REUSED": "정상 확인 · 같은 완료 일봉의 기존 판단 유지",
    "SEALED": "예약 실행 확인 · 해당 일봉은 이미 평가 완료 (시세 재조회 생략)",
    "SKIPPED": "⚠️ 데이터 검증 보류 · 아래는 마지막 정상 평가 결과이며 이번 실행의 무후보 판정이 아닙니다",
    "ERROR": "⚠️ 실행 실패 · 아래는 마지막 정상 평가 결과이며 이번 실행의 무후보 판정이 아닙니다",
}
REASONS = {
    "PRICE_SNAPSHOT_STALE_OR_FUTURE": "수신 일봉 날짜가 허용 범위를 벗어남 (오래된 날짜 또는 미래 날짜)",
    "SAME_DAY_CLOSE_UNVERIFIED": "당일 일봉 마감 확인 전",
}


def refresh(readme: Path, latest: Path, status: str, reason: str = "",
            now: datetime | None = None) -> None:
    now = now or datetime.now(ZoneInfo("Asia/Seoul"))
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    label = now.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
    source = readme.read_text(encoding="utf-8")
    if source.count(START) != 1 or source.count(END) != 1:
        raise ValueError("README_PANEL_MARKERS_MISSING_OR_DUPLICATED")
    start, end = source.index(START) + len(START), source.index(END)
    if start > end:
        raise ValueError("README_PANEL_MARKERS_REVERSED")
    panel = source[start:end].strip()
    panel = re.sub(r"^> 실행 상태:.*\n\n?", "", panel, flags=re.MULTILINE)
    # The first timestamp used to mean fetched_at. Preserve it separately forever;
    # runner re-rendering supplies the original timestamp again on REUSED.
    pattern = r"> 갱신: `([^`]+)` · 기준 완료 일봉: `([^`]+)`(?: · 기록 수신: `([^`]+)`)?"
    def stamp(match):
        return (f"> 갱신: `{label}` · 기준 완료 일봉: `{match[2]}`"
                f" · 기록 수신: `{match[3] or match[1]}`")
    panel, count = re.subn(pattern, stamp, panel)
    if count != 2:
        raise ValueError("EXPECTED_TWO_FORWARD_TIMESTAMPS")
    message = STATUS[status]
    if reason:
        message += " · " + REASONS.get(reason, "상세 사유는 Actions 실행 로그 참조")
    panel = f"> 실행 상태: {message}\n\n{panel}\n"
    readme.write_text(source[:start] + "\n" + panel + source[end:], encoding="utf-8")
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(panel, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--latest", type=Path, default=Path("quant-research/data/research/T-110/latest.md"))
    parser.add_argument("--status", choices=STATUS, required=True)
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    refresh(args.readme, args.latest, args.status, args.reason)


if __name__ == "__main__":
    main()
