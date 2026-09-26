"""
Unit tests for RAG Thesis Engine.
Verifies profile retrieval, factor diagnostics translation, and markdown table rendering.
"""

import pytest
import json
from pathlib import Path
from scripts.rag_thesis_engine import RagThesisEngine


@pytest.fixture
def rag_engine(tmp_path):
    kb_path = tmp_path / "stock_kb.json"
    kb_path.write_text(json.dumps({
        "192820": {"code": "192820", "name": "코스맥스", "market": "KOSPI",
                   "sector": "화장품", "description": "ODM 사업"},
        "008930": {"code": "008930", "name": "한미사이언스", "market": "KOSPI",
                   "sector": "제약", "description": "지주회사"},
    }, ensure_ascii=False), encoding="utf-8")
    return RagThesisEngine(kb_path=kb_path)


def test_get_stock_profile_existing(rag_engine):
    profile = rag_engine.get_stock_profile("192820")
    assert profile["name"] == "코스맥스"
    assert "K-뷰티" in profile["sector"] or "화장품" in profile["sector"]
    assert "ODM" in profile["description"]


def test_get_stock_profile_unknown(rag_engine):
    profile = rag_engine.get_stock_profile("999999")
    assert profile["code"] == "999999"
    assert profile["market"] == "KRX"


def test_evaluate_factors_high_momentum_high_cmf(rag_engine):
    evals = rag_engine.evaluate_factors(
        mom60_5=0.85,
        risk_adj_mom=1.35,
        cmf20=0.22
    )
    assert "최근 60번째→5번째 종가 수익률 +85.0%" in evals["mom_desc"]
    assert "🔥" in evals["mom_tag"]
    assert "변동성 조정 모멘텀 1.35" in evals["sharpe_desc"]
    assert "20일 종가 위치·거래량 지표 CMF +0.22" in evals["cmf_desc"]
    assert "기관" not in evals["cmf_desc"]


def test_generate_thesis_synthesis(rag_engine):
    thesis = rag_engine.generate_thesis(
        code="192820",
        mom60_5=0.8636,
        risk_adj_mom=1.333,
        cmf20=0.2003,
        composite_score=0.954,
        price=273000.0
    )
    assert thesis["name"] == "코스맥스"
    assert thesis["composite_score"] == 0.954
    assert "+86.4%" in thesis["mom_tag"]
    assert "변동성 조정 모멘텀 1.33" in thesis["thesis_full"]
    assert "CMF +0.20" in thesis["thesis_full"]
    assert "글로벌 1위" not in thesis["thesis_full"]


def test_render_markdown_table(rag_engine):
    sample = [
        rag_engine.generate_thesis("192820", 0.86, 1.33, 0.20, 0.95, 273000.0),
        rag_engine.generate_thesis("008930", 0.82, 0.92, 0.18, 0.86, 48500.0),
    ]
    table = rag_engine.render_markdown_table(sample)
    assert "🥇 1위" in table
    assert "🥈 2위" in table
    assert "코스맥스" in table
    assert "한미사이언스" in table
    assert "모드 2 · 일봉 팩터 관찰 (가상)" in table
    assert "평가일 종가" in table
    assert "기관 매집" not in table
