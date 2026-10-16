import pytest
import pandas as pd
from collector import calculate_smart_rank, build_results, _render_signal_rows


def test_calculate_smart_rank_goldilocks():
    features = {
        "score": 75.0,
        "breakout_level": 50000.0,
        "trigger_amount_e8": 25.0,
        "volume_ratio": 2.2,
        "pattern_type": "base_breakout",
    }
    entry_price = 51250.0  # +2.5% gap
    res = calculate_smart_rank(features, signal_time_str="2026-09-21 09:00", entry_price=entry_price)
    
    assert res["health"] == "안착🟢"
    assert res["gap_pct"] == 2.5
    assert res["amount_e8"] == 25.0
    assert res["vr"] == 2.2
    assert res["smart_score"] >= 80.0


def test_calculate_smart_rank_weak_fakeout():
    features = {
        "score": 60.0,
        "breakout_level": 50000.0,
        "trigger_amount_e8": 5.0,
        "volume_ratio": 1.2,
        "pattern_type": "base_breakout",
    }
    entry_price = 50150.0  # +0.3% gap
    res = calculate_smart_rank(features, signal_time_str="2026-09-21 14:00", entry_price=entry_price)
    
    assert res["health"] == "턱걸이⚠️"
    assert res["gap_pct"] == 0.3
    # 턱걸이 감점으로 점수가 낮아져야 함
    assert res["smart_score"] < 60.0


def test_calculate_smart_rank_overextended():
    features = {
        "score": 70.0,
        "breakout_level": 50000.0,
        "trigger_amount_e8": 15.0,
        "volume_ratio": 2.5,
        "pattern_type": "base_breakout",
    }
    entry_price = 53500.0  # +7.0% gap
    res = calculate_smart_rank(features, signal_time_str="2026-09-21 09:00", entry_price=entry_price)
    
    assert res["health"] == "과열⚠️"
    assert res["gap_pct"] == 7.0


def test_calculate_smart_rank_none_safe():
    res = calculate_smart_rank(None, signal_time_str=None, entry_price=None)
    assert isinstance(res["smart_score"], float)
    assert res["health"] == "턱걸이⚠️"
    assert res["gap_pct"] == 0.0


def test_build_results_sorts_by_smart_score():
    rows = [
        {
            "종목": "약한종목",
            "코드": "111111",
            "시장": "KOSPI",
            "구분": "일치",
            "점수": 80.0,
            "돌파봉종가": 10030.0,
            "돌파선": 10000.0,
            "기준봉(KST)": "09-21 14:00",
            "_bar_time_full": "2026-09-21 14:00",
            "_base": True,
            "_match": True,
            "_waiting": False,
            "_features": {
                "score": 80.0,
                "breakout_level": 10000.0,
                "trigger_amount_e8": 5.0,
                "volume_ratio": 1.2,
                "pattern_type": "base_breakout",
            },
        },
        {
            "종목": "골디락스종목",
            "코드": "222222",
            "시장": "KOSDAQ",
            "구분": "일치",
            "점수": 70.0,
            "돌파봉종가": 10250.0,
            "돌파선": 10000.0,
            "기준봉(KST)": "09-21 09:00",
            "_bar_time_full": "2026-09-21 09:00",
            "_base": True,
            "_match": True,
            "_waiting": False,
            "_features": {
                "score": 70.0,
                "breakout_level": 10000.0,
                "trigger_amount_e8": 30.0,
                "volume_ratio": 2.5,
                "pattern_type": "base_breakout",
            },
        },
    ]

    top, _ = build_results(rows)
    assert len(top) == 2
    # 골디락스 종목이 기본점수는 70으로 낮았어도 스마트 랭킹 가산점으로 1위가 되어야 함
    assert top.iloc[0]["종목"] == "골디락스종목"
    assert top.iloc[0]["순위"] == "🥇 1위"
    assert top.iloc[1]["종목"] == "약한종목"
    assert top.iloc[1]["순위"] == "🥈 2위"


def test_render_signal_rows_format():
    sig_list = [
        {
            "signal_id": "test_1",
            "strategy_version": "v1",
            "code": "319660",
            "name": "피에스케이",
            "entry_reference_price": 100000.0,
            "signal_time_kst": "2026-09-21 09:00",
            "trading_days_observed": 1,
            "features": {
                "score": 75.0,
                "breakout_level": 97500.0,
                "trigger_amount_e8": 25.0,
                "volume_ratio": 2.0,
                "pattern_type": "base_breakout",
            },
            "stops": {"stop_5": 95000.0},
        }
    ]
    eval_by_sigid = {
        "test_1": {
            "action_type": "KEEP",
            "decision": "HOLD",
            "current_price": 103000.0,
            "pnl_pct": 3.0,
        }
    }

    lines = _render_signal_rows(sig_list, eval_by_sigid, new_codes=set(), empty_message="없음")
    table_text = "\n".join(lines)
    assert "| 상태 | 종목(코드) | 현재가(수익률) | 손절가 | 돌파이격 (판정) | 대금 · VR | 신호시각 (경과) |" in table_text
    assert "피에스케이" in table_text
    assert "안착🟢" in table_text
    assert "25.0억 (2.0x)" in table_text


def test_build_results_watch_vs_top5_feature_alignment():
    rows = [
        {
            "종목": "확정돌파종목",
            "코드": "100001",
            "시장": "KOSPI",
            "구분": "일치",
            "점수": 78.0,
            "돌파봉종가": 10250.0,
            "돌파선": 10000.0,
            "기준봉(KST)": "09-21 10:00",
            "_bar_time_full": "2026-09-21 10:00",
            "_base": True,
            "_match": True,
            "_early_trigger": False,
            "_waiting": False,
            "_current_amount": 18.0,
            "_current_ratio": 1.6,
            "_current_level": 10300.0,
            "_current_close": 10280.0,
            "_features": {
                "score": 78.0,
                "breakout_level": 10000.0,
                "trigger_amount_e8": 35.0,
                "trigger_ratio": 3.2,
                "trigger_close": 10250.0,
                "current_close": 10280.0,
                "volume_ratio": 2.4,
                "pattern_type": "base_breakout",
            },
        },
        {
            "종목": "조기돌파종목",
            "코드": "100002",
            "시장": "KOSDAQ",
            "구분": "일치",
            "점수": 74.0,
            "돌파봉종가": 20400.0,
            "돌파선": 20000.0,
            "기준봉(KST)": "09-21 09:00",
            "_bar_time_full": "2026-09-21 09:00",
            "_base": True,
            "_match": True,
            "_early_trigger": True,
            "_waiting": False,
            "_current_amount": 15.0,
            "_current_ratio": 1.9,
            "_current_level": 20000.0,
            "_current_close": 20400.0,
            "_features": {
                "score": 74.0,
                "breakout_level": 20000.0,
                "trigger_amount_e8": 22.0,
                "trigger_ratio": 2.1,
                "trigger_close": 20400.0,
                "current_close": 20400.0,
                "volume_ratio": 2.1,
                "pattern_type": "base_breakout",
                "early_trigger": True,
            },
        },
        {
            "종목": "관찰대기종목",
            "코드": "100003",
            "시장": "KOSDAQ",
            "구분": "제외",
            "점수": 81.2,
            "돌파봉종가": 9480.0,
            "돌파선": 9500.0,
            "기준봉(KST)": "09-21 09:00",
            "_bar_time_full": "2026-09-21 09:00",
            "_base": True,
            "_match": False,
            "_early_trigger": False,
            "_waiting": True,
            "_current_amount": 48.5,
            "_current_ratio": 4.1,
            "_current_level": 9800.0,
            "_current_close": 10100.0,
            "_features": {
                "score": 81.2,
                "breakout_level": 9500.0,
                "trigger_amount_e8": 1.2,
                "trigger_ratio": 0.4,
                "trigger_close": 9480.0,
                "current_close": 10100.0,
                "volume_ratio": 3.1,
                "pattern_type": "base_breakout",
            },
        },
    ]

    top, watch = build_results(rows)
    assert len(top) == 2
    assert len(watch) == 1

    by_code_top = {r["코드"]: r["_features"] for _, r in top.iterrows()}
    assert by_code_top["100001"]["signal_stage"] == "top5_hold"
    assert by_code_top["100001"]["trigger_amount_e8"] == 35.0
    assert by_code_top["100002"]["signal_stage"] == "top5_early"

    w_row = watch.iloc[0]
    w_feat = w_row["_features"]
    assert w_feat["signal_stage"] == "watch"
    assert w_feat["trigger_amount_e8"] == 48.5
    assert w_feat["trigger_ratio"] == 4.1
    assert w_feat["breakout_level"] == 9800.0
    assert w_feat["trigger_close"] == 10100.0
    assert w_feat["current_close"] == 10100.0
    assert w_row["돌파봉대금_억"] == round(w_feat["trigger_amount_e8"], 1)
    assert w_row["동시간배수"] == round(w_feat["trigger_ratio"], 2)
    assert w_row["돌파선"] == round(w_feat["breakout_level"], 2)
    assert w_row["돌파봉종가"] == w_feat["trigger_close"]

