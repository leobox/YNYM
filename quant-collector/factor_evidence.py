"""Evidence-only explanations for the read-only Mode 2 daily factor panel.

This is deterministic retrieval from the ranking row, not an LLM or a claim
about investor identity, company fundamentals, or future returns.
"""

from __future__ import annotations

import math
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse


FACTS_PATH = Path(__file__).resolve().parent / "data" / "mode2_sourced_facts.json"


@lru_cache(maxsize=1)
def _sourced_facts() -> dict:
    return json.loads(FACTS_PATH.read_text(encoding="utf-8"))


def profile_fact(code: str) -> str:
    """Retrieve a company fact only when its source and review date are present."""
    item = _sourced_facts().get(str(code).zfill(6))
    if not isinstance(item, dict):
        return ""
    fact, source, checked = (item.get(key) for key in ("fact", "source_url", "checked_on"))
    parsed = urlparse(source) if isinstance(source, str) else None
    if not (isinstance(fact, str) and fact and parsed and parsed.scheme == "https"
            and parsed.netloc and isinstance(checked, str) and len(checked) == 10):
        return ""
    return f"회사 참고: {fact} ([공식 자료]({source}), {checked} 확인)"


def explain_rank(row) -> str:
    """Show raw factor values and their weighted percentile contributions."""
    keys = ("mom60_5", "risk_adj_mom", "cmf20", "rank_ramom", "rank_mom",
            "rank_cmf", "composite_score")
    values = {key: float(getattr(row, key)) for key in keys}
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("non-finite Mode 2 factor evidence")
    risk = 0.40 * values["rank_ramom"]
    mom = 0.30 * values["rank_mom"]
    cmf = 0.30 * values["rank_cmf"]
    if not math.isclose(risk + mom + cmf, values["composite_score"], abs_tol=1e-9):
        raise ValueError("Mode 2 factor contributions do not match score")
    return (
        f"60→5거래일 수익률 **{values['mom60_5']:+.1%}** · "
        f"변동성 조정 모멘텀 **{values['risk_adj_mom']:.2f}** · "
        f"CMF20 **{values['cmf20']:+.2f}**<br>"
        f"점수 기여: 위험조정 {risk:.3f} + 추세 {mom:.3f} + CMF {cmf:.3f}"
    )
