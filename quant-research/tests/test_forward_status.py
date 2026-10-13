"""The dashboard must distinguish an empty decision from a failed execution."""
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.refresh_forward_status import refresh


@pytest.fixture
def dashboard(tmp_path):
    readme, latest = tmp_path / "README.md", tmp_path / "latest.md"
    panel = "### 市場\n\n"
    for title in ("상승초입 3일 이내", "상승 초입"):
        panel += (f"### {title}\n\n"
                  "> 갱신: `2026-10-09 03:48 KST` · 기준 완료 일봉: `2026-10-08`\n\n"
                  "| - | 조건 충족 없음 | - |\n\n")
    readme.write_text("header\n<!-- DUAL_FORWARD:START -->\n" + panel
                      + "<!-- DUAL_FORWARD:END -->\ncollector untouched\n", encoding="utf-8")
    return readme, latest


@pytest.mark.parametrize("status", ["CAPTURED", "REUSED", "BACKFILLED", "SEALED"])
def test_empty_decisions_refresh_both_timestamps_without_advancing_bar(dashboard, status):
    readme, latest = dashboard
    now = datetime(2026, 10, 13, 18, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    refresh(readme, latest, status, now=now)
    content = readme.read_text(encoding="utf-8")
    assert content.count("갱신: `2026-10-13 18:00 KST`") == 2
    assert content.count("기준 완료 일봉: `2026-10-08`") == 2
    assert content.count("기록 수신: `2026-10-09 03:48 KST`") == 2
    assert content.count("조건 충족 없음") == 2
    assert content.startswith("header\n<!-- DUAL_FORWARD:START -->")
    assert content.endswith("<!-- DUAL_FORWARD:END -->\ncollector untouched\n")
    assert latest.read_text(encoding="utf-8") in content
    refresh(readme, latest, status, now=now)
    assert readme.read_text(encoding="utf-8") == content
    refresh(readme, latest, status, now=now.replace(hour=19))
    assert readme.read_text(encoding="utf-8").count("기록 수신: `2026-10-09 03:48 KST`") == 2


@pytest.mark.parametrize("status,reason", [
    ("SKIPPED", "PRICE_SNAPSHOT_STALE_OR_FUTURE"),
    ("SKIPPED", "SAME_DAY_CLOSE_UNVERIFIED"), ("ERROR", ""),
])
def test_failed_execution_is_never_reported_as_a_new_empty_decision(dashboard, status, reason):
    readme, latest = dashboard
    refresh(readme, latest, status, reason)
    content = readme.read_text(encoding="utf-8")
    assert "이번 실행의 무후보 판정이 아닙니다" in content
    assert "기준 완료 일봉: `2026-10-08`" in content
    refresh(readme, latest, "REUSED")
    assert "이번 실행의 무후보 판정이 아닙니다" not in readme.read_text(encoding="utf-8")
    assert readme.read_text(encoding="utf-8").count("> 실행 상태:") == 1


def test_invalid_panel_fails_without_writing(dashboard):
    readme, latest = dashboard
    readme.write_text("missing markers", encoding="utf-8")
    with pytest.raises(ValueError, match="MARKERS"):
        refresh(readme, latest, "ERROR")
    assert readme.read_text(encoding="utf-8") == "missing markers"
    assert not latest.exists()
