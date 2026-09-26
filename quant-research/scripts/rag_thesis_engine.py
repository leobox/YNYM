"""
RAG Thesis Engine for Quantitative Factor Portfolios.

Combines a static stock profile with historical daily-bar factors:
1. Return from the 60th-most-recent close to the 5th-most-recent close
2. Return divided by annualized 60-session volatility
3. Chaikin Money Flow (CMF20), an OHLCV indicator

Produces descriptive, read-only reports. The profile database has no
independent source or as-of-date verification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_KB_PATH = Path(__file__).resolve().parent.parent / "data" / "stock_knowledge_base.json"


class RagThesisEngine:
    def __init__(self, kb_path: Path | str = DEFAULT_KB_PATH):
        self.kb_path = Path(kb_path)
        self.knowledge_base: Dict[str, Dict[str, Any]] = {}
        self.load_knowledge_base()

    def load_knowledge_base(self):
        if self.kb_path.exists():
            with open(self.kb_path, "r", encoding="utf-8") as f:
                self.knowledge_base = json.load(f)

    def get_stock_profile(self, code: str) -> Dict[str, Any]:
        c = str(code).zfill(6)
        if c in self.knowledge_base:
            return self.knowledge_base[c]
        return {
            "code": c,
            "name": c,
            "market": "KRX",
            "sector": "일반/제조",
            "description": "국내 주요 상장사"
        }

    def evaluate_factors(
        self,
        mom60_5: float,
        risk_adj_mom: float,
        cmf20: float
    ) -> Dict[str, str]:
        """
        Translates raw numeric factors into intuitive human-readable diagnostics.
        """
        mom_pct = mom60_5 * 100.0

        # The portfolio manager currently uses c.iloc[-60] and c.iloc[-5].
        if mom_pct >= 50.0:
            mom_desc = f"최근 60번째→5번째 종가 수익률 {mom_pct:+.1f}% (높음)"
            mom_tag = f"+{mom_pct:.1f}% 🔥"
        elif mom_pct >= 25.0:
            mom_desc = f"최근 60번째→5번째 종가 수익률 {mom_pct:+.1f}%"
            mom_tag = f"+{mom_pct:.1f}% 📈"
        elif mom_pct >= 10.0:
            mom_desc = f"최근 60번째→5번째 종가 수익률 {mom_pct:+.1f}%"
            mom_tag = f"+{mom_pct:.1f}% ↗️"
        else:
            mom_desc = f"최근 60번째→5번째 종가 수익률 {mom_pct:+.1f}%"
            mom_tag = f"{mom_pct:+.1f}%"

        # This ratio is not a conventional Sharpe ratio.
        if risk_adj_mom >= 1.20:
            sharpe_desc = f"변동성 조정 모멘텀 {risk_adj_mom:.2f} (상위 구간)"
            sharpe_tag = f"{risk_adj_mom:.2f} (높음)"
        elif risk_adj_mom >= 0.80:
            sharpe_desc = f"변동성 조정 모멘텀 {risk_adj_mom:.2f}"
            sharpe_tag = f"{risk_adj_mom:.2f}"
        else:
            sharpe_desc = f"변동성 조정 모멘텀 {risk_adj_mom:.2f}"
            sharpe_tag = f"{risk_adj_mom:.2f}"

        # CMF measures volume-weighted close location; it cannot identify buyer type.
        if cmf20 >= 0.15:
            cmf_desc = f"20일 종가 위치·거래량 지표 CMF {cmf20:+.2f} (양수)"
            cmf_tag = f"{cmf20:+.2f}"
        elif cmf20 >= 0.05:
            cmf_desc = f"20일 종가 위치·거래량 지표 CMF {cmf20:+.2f} (양수)"
            cmf_tag = f"{cmf20:+.2f}"
        else:
            cmf_desc = f"20일 종가 위치·거래량 지표 CMF {cmf20:+.2f}"
            cmf_tag = f"{cmf20:+.2f}"

        return {
            "mom_desc": mom_desc,
            "mom_tag": mom_tag,
            "sharpe_desc": sharpe_desc,
            "sharpe_tag": sharpe_tag,
            "cmf_desc": cmf_desc,
            "cmf_tag": cmf_tag
        }

    def generate_thesis(
        self,
        code: str,
        mom60_5: float,
        risk_adj_mom: float,
        cmf20: float,
        composite_score: float,
        price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Synthesizes the full RAG thesis record for a stock.
        """
        profile = self.get_stock_profile(code)
        f_eval = self.evaluate_factors(mom60_5, risk_adj_mom, cmf20)

        name = profile["name"]
        sector = profile["sector"]
        desc = profile["description"]

        # Profiles currently lack source and as-of metadata. Keep them out of
        # the diagnostic prose until their claims can be checked independently.
        thesis_full = (
            f"{f_eval['mom_desc']} · "
            f"{f_eval['sharpe_desc']} · "
            f"{f_eval['cmf_desc']}"
        )

        return {
            "code": code,
            "name": name,
            "market": profile.get("market", "KRX"),
            "sector": sector,
            "desc": desc,
            "price": price,
            "composite_score": composite_score,
            "mom_pct": mom60_5 * 100.0,
            "mom_tag": f_eval["mom_tag"],
            "sharpe": risk_adj_mom,
            "sharpe_tag": f_eval["sharpe_tag"],
            "cmf": cmf20,
            "cmf_tag": f_eval["cmf_tag"],
            "thesis_full": thesis_full
        }

    def render_markdown_table(self, items: list[dict]) -> str:
        """
        Renders a clean, high-readability GitHub markdown table with RAG thesis.
        """
        lines = [
            "> 모드 2 · 일봉 팩터 관찰 (가상). 수치는 저장된 일봉의 평가일 종가 기준이며 매수 신호나 기관·외국인 순매수 자료가 아닙니다. 출처·기준일을 확인할 수 없는 정적 회사 설명은 판정에서 제외했습니다.",
            "",
            "| 순위 | 종목명 (코드) | 평가일 종가 | 종합점수 | 최근 60번째→5번째 종가 수익률 | 변동성 조정 모멘텀 | CMF20 | 팩터 판정이유 |",
            "| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :--- |"
        ]

        medals = ["🥇 1위", "🥈 2위", "🥉 3위"]
        for i, item in enumerate(items):
            rank_str = medals[i] if i < 3 else f"**{i+1}위**"
            px_str = f"{item['price']:,.0f}원" if item.get("price") else "-"
            score_str = f"**{item['composite_score']:.3f}**"
            mom_str = item["mom_tag"]
            sharpe_str = item["sharpe_tag"]
            cmf_str = item["cmf_tag"]
            thesis = item["thesis_full"]

            lines.append(
                f"| {rank_str} | **{item['name']}**<br>`({item['code']})` | {px_str} | {score_str} | {mom_str} | {sharpe_str} | {cmf_str} | {thesis} |"
            )

        return "\n".join(lines)
