"""Tests for research.collector_signal_analyzer (including T-120 R2~R7 regression tests)."""
import json
import math
from pathlib import Path
import pandas as pd
import pytest

from research.collector_signal_analyzer import (
    CollectorSignalLoader,
    PayoffMatrixAnalyzer,
    RiskFilterEngine,
    build_default_filters,
    generate_analysis_report,
    pass_composite_filter,
    pass_goldilocks_atr_filter,
    pass_meta_filter,
    pass_min_score_filter,
    pass_pattern_filter,
)


@pytest.fixture
def sample_signals_data():
    """합성 신호 데이터 4건 생성."""
    return [
        {
            "signal_id": "sig_1",
            "strategy_version": "algorithm260917_v1",
            "signal_time_kst": "2026-09-18 09:00",
            "registered_at_kst": "2026-09-18 10:05:00",
            "name": "성공종목1",
            "code": "000001",
            "entry_reference_price": 10000.0,
            "features": {
                "pattern_type": "base_breakout",
                "score": 75.0,
                "extension_atr": 2.5,
                "volume_ratio": 2.0,
                "trigger_ratio": 1.5,
                "trigger_amount_e8": 20.0,
            },
            "evaluations": {
                "h5_t5_s5": {"status": "TARGET_FIRST", "return_pct": 5.0},
                "h3_t3_s3": {"status": "TARGET_FIRST", "return_pct": 3.0},
            },
        },
        {
            "signal_id": "sig_2",
            "strategy_version": "algorithm260917_v1",
            "signal_time_kst": "2026-09-18 10:00",
            "registered_at_kst": "2026-09-18 11:05:00",
            "name": "성공종목2",
            "code": "000002",
            "entry_reference_price": 20000.0,
            "features": {
                "pattern_type": "base_breakout",
                "score": 80.0,
                "extension_atr": 3.0,
                "volume_ratio": 1.8,
                "trigger_ratio": 1.2,
                "trigger_amount_e8": 15.0,
            },
            "evaluations": {
                "h5_t5_s5": {"status": "TARGET_FIRST", "return_pct": 5.0},
                "h3_t3_s3": {"status": "TARGET_FIRST", "return_pct": 3.0},
            },
        },
        {
            "signal_id": "sig_3",
            "strategy_version": "algorithm260917_v1",
            "signal_time_kst": "2026-09-18 13:00",
            "registered_at_kst": "2026-09-18 14:05:00",
            "name": "실패반등종목",
            "code": "000003",
            "entry_reference_price": 15000.0,
            "features": {
                "pattern_type": "rebound",
                "score": 62.0,
                "extension_atr": 4.2,
                "volume_ratio": 1.5,
                "trigger_ratio": 2.5,
                "trigger_amount_e8": 50.0,
            },
            "evaluations": {
                "h5_t5_s5": {"status": "STOP_FIRST", "return_pct": -5.0},
                "h3_t3_s3": {"status": "STOP_FIRST", "return_pct": -3.0},
            },
        },
        {
            "signal_id": "sig_4",
            "strategy_version": "algorithm260917_v1",
            "signal_time_kst": "2026-09-18 14:00",
            "registered_at_kst": "2026-09-18 15:05:00",
            "name": "실패과열종목",
            "code": "000004",
            "entry_reference_price": 30000.0,
            "features": {
                "pattern_type": "base_breakout",
                "score": 64.0,
                "extension_atr": 4.5,
                "volume_ratio": 2.5,
                "trigger_ratio": 3.0,
                "trigger_amount_e8": 40.0,
            },
            "evaluations": {
                "h5_t5_s5": {"status": "STOP_FIRST", "return_pct": -5.0},
                "h3_t3_s3": {"status": "STOP_FIRST", "return_pct": -3.0},
            },
        },
    ]


class TestCollectorSignalLoader:
    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            CollectorSignalLoader.load_signals("non_existent_file.jsonl")

    def test_load_valid_file(self, sample_signals_data, tmp_path):
        test_file = tmp_path / "test_signals.jsonl"
        with open(test_file, "w", encoding="utf-8") as f:
            for item in sample_signals_data:
                f.write(json.dumps(item) + "\n")

        loaded = CollectorSignalLoader.load_signals(test_file)
        assert len(loaded) == 4
        assert loaded[0]["code"] == "000001"
        assert loaded[0]["features"]["pattern_type"] == "base_breakout"

    def test_r7_loader_validation_and_dedup(self, sample_signals_data, tmp_path):
        test_file = tmp_path / "mixed_signals.jsonl"
        other_strat = {
            **sample_signals_data[0],
            "signal_id": "sig_other_strat",
            "strategy_version": "volume_zscore_accel_v1",
        }
        missing_fields = {"signal_id": "bad_1", "code": "000999"}  # missing evaluations/features
        with open(test_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(sample_signals_data[0]) + "\n")
            f.write(json.dumps(sample_signals_data[0]) + "\n")  # duplicate signal_id
            f.write("{corrupted_json_line\n")
            f.write(json.dumps([1, 2, 3]) + "\n")  # non-dict JSON
            f.write(json.dumps(missing_fields) + "\n")
            f.write(json.dumps(other_strat) + "\n")

        # Non-strict mode: skips corrupted, non-dict, missing schema, and duplicate IDs, tracks stats
        records, stats = CollectorSignalLoader.load_signals_with_stats(
            test_file,
            strategy_version="algorithm260917_v1",
            strict=False,
        )
        assert len(records) == 1
        assert stats.total_lines == 6
        assert stats.loaded_count == 1
        assert stats.malformed_count == 1
        assert stats.invalid_schema_count == 2
        assert stats.duplicate_count == 1
        assert stats.filtered_strategy_count == 1
        assert len(stats.sha256) == 64

        # Strict mode: raises ValueError on corrupted/duplicate/missing schema
        with pytest.raises(ValueError):
            CollectorSignalLoader.load_signals(test_file, strict=True)

    def test_r2_relabel_from_5m_bars_post_registration(self, tmp_path):
        """등록 시각(registered_at_kst) 이전의 5분봉은 무시하고 등록 이후 첫 5분봉 시가로 재라벨링하는지 검증."""
        bh_dir = tmp_path / "bars_history"
        bh_dir.mkdir()

        # 신호 시각: 2026-09-18 09:00 (09:00~10:00 봉), 등록 시각: 2026-09-18 10:41:46
        # 09:05 봉에서 급락(-10%)했으나 등록 시각 이전이므로 진입 불가.
        # 등록 시각(10:41:46) 이후 첫 5분봉은 10:45 봉(시가 10,000원).
        # 이후 10:50 봉에서 고가 10,600원(+6%) 달성 -> TARGET_FIRST (+5% - 0.35% 비용 = +4.65%)
        idx = pd.to_datetime([
            "2026-09-18 09:05:00+09:00",
            "2026-09-18 10:40:00+09:00",
            "2026-09-18 10:45:00+09:00",
            "2026-09-18 10:50:00+09:00",
        ])
        df5 = pd.DataFrame(
            {
                "Open": [10000.0, 9500.0, 10000.0, 10100.0],
                "High": [10000.0, 9600.0, 10200.0, 10600.0],
                "Low": [8800.0, 9400.0, 9950.0, 10050.0],
                "Close": [8900.0, 9550.0, 10100.0, 10550.0],
                "Volume": [1000, 500, 800, 1200],
            },
            index=idx,
        )
        df5.to_csv(bh_dir / "229640_5m.csv")

        raw_record = {
            "signal_id": "algorithm260917_v1_229640_2026-09-18_0900",
            "strategy_version": "algorithm260917_v1",
            "code": "229640",
            "name": "테스트종목",
            "signal_time_kst": "2026-09-18 09:00",
            "registered_at_kst": "2026-09-18 10:41:46",
            "entry_reference_price": 10000.0,
            "features": {"pattern_type": "base_breakout", "score": 80.0, "extension_atr": 2.5},
            "evaluations": {
                "h5_t5_s5": {
                    "status": "STOP_FIRST",
                    "exit_price": 8900.0,
                    "exit_time": "2026-09-18 09:05",
                    "return_pct": -11.0,
                }
            },
        }

        relabeled = CollectorSignalLoader.relabel_from_5m_bars([raw_record], bh_dir, one_way_cost_bps=17.5)
        assert len(relabeled) == 1
        r = relabeled[0]
        assert r["relabel_meta"]["status"] == "OK"
        assert r["relabel_meta"]["pre_registration_exit_in_raw"] is True
        assert "h5_t5_s5" in r["relabel_meta"]["pre_registration_exit_keys"]
        assert r["relabel_meta"]["entry_bar_time"] == "2026-09-18 10:45"
        assert r["relabel_meta"]["entry_price"] == 10000.0

        ev = r["evaluations"]["h5_t5_s5"]
        assert ev["status"] == "TARGET_FIRST"
        assert ev["exit_time"] == "2026-09-18 10:50"
        # +5.0% target minus 0.35% round-trip cost = +4.65%
        assert ev["return_pct"] == pytest.approx(4.65)


class TestPayoffMatrixAnalyzer:
    def test_evaluate_scenario_metrics(self, sample_signals_data):
        m = PayoffMatrixAnalyzer.evaluate_scenario(sample_signals_data, "h5_t5_s5")
        assert m.scenario_id == "h5_t5_s5"
        assert m.sample_count == 4
        assert m.target_count == 2
        assert m.stop_count == 2
        assert m.win_rate == 50.0
        assert m.loss_rate == 50.0
        assert m.avg_return_pct == 0.0
        assert m.ev_pct == 0.0
        assert m.profit_factor == 1.0

    def test_evaluate_all_returns_16_scenarios(self, sample_signals_data):
        all_metrics = PayoffMatrixAnalyzer.evaluate_all(sample_signals_data)
        assert len(all_metrics) == 16
        assert "h5_t5_s5" in all_metrics
        assert "h3_t10_s5" in all_metrics

    def test_empty_records_safe(self):
        m = PayoffMatrixAnalyzer.evaluate_scenario([], "h5_t5_s5")
        assert m.sample_count == 0
        assert m.win_rate == 0.0
        assert m.profit_factor is None

    def test_r5_pending_missing_nan_and_ambiguous_stop_integrity(self):
        """PENDING/결측/NaN을 완료 표본 0%로 위장하지 않고 AMBIGUOUS_STOP을 손절 수에 포함하는지 검증."""
        records = [
            {
                "signal_id": "s1",
                "code": "000001",
                "features": {},
                "evaluations": {"h5_t5_s5": {"status": "TARGET_FIRST", "return_pct": 5.0}},
            },
            {
                "signal_id": "s2_pending",
                "code": "000002",
                "features": {},
                "evaluations": {"h5_t5_s5": {"status": "PENDING", "return_pct": None}},
            },
            {
                "signal_id": "s3_missing_ret",
                "code": "000003",
                "features": {},
                "evaluations": {"h5_t5_s5": {"status": "TARGET_FIRST", "return_pct": None}},
            },
            {
                "signal_id": "s4_nan_ret",
                "code": "000004",
                "features": {},
                "evaluations": {"h5_t5_s5": {"status": "STOP_FIRST", "return_pct": float("nan")}},
            },
            {
                "signal_id": "s5_ambiguous",
                "code": "000005",
                "features": {},
                "evaluations": {"h5_t5_s5": {"status": "AMBIGUOUS_STOP", "return_pct": -5.0}},
            },
        ]
        m = PayoffMatrixAnalyzer.evaluate_scenario(records, "h5_t5_s5")
        assert m.sample_count == 2  # s1 and s5_ambiguous only
        assert m.target_count == 1
        assert m.stop_count == 1
        assert m.ambiguous_stop_count == 1
        assert m.timeout_count == 0
        assert m.target_count + m.stop_count + m.timeout_count == m.sample_count
        assert m.pending_count == 1
        assert m.invalid_count == 2
        assert m.win_rate == 50.0
        assert m.loss_rate == 50.0
        assert m.ev_pct == 0.0
        assert m.profit_factor == 1.0

    def test_r5_zero_loss_profit_factor_is_none_not_999(self):
        records = [
            {
                "signal_id": "s1",
                "code": "000001",
                "features": {},
                "evaluations": {"h5_t5_s5": {"status": "TARGET_FIRST", "return_pct": 5.0}},
            }
        ]
        m = PayoffMatrixAnalyzer.evaluate_scenario(records, "h5_t5_s5")
        assert m.sample_count == 1
        assert m.profit_factor is None


class TestRiskFilterEngine:
    def test_feature_summary(self, sample_signals_data):
        summary = RiskFilterEngine.get_feature_summary(sample_signals_data, "h5_t5_s5")
        assert summary["target_count"] == 2
        assert summary["stop_count"] == 2

        score_stats = summary["features"]["score"]
        assert score_stats["target_mean"] == pytest.approx(77.5)
        assert score_stats["stop_mean"] == pytest.approx(63.0)

        atr_stats = summary["features"]["extension_atr"]
        assert atr_stats["target_mean"] == pytest.approx(2.75)
        assert atr_stats["stop_mean"] == pytest.approx(4.35)

    def test_filter_evaluation_rebound_pruning(self, sample_signals_data):
        res = RiskFilterEngine.evaluate_filter(
            sample_signals_data,
            pass_pattern_filter,
            filter_name="NoRebound",
            description="rebound 차단",
            scenario_key="h5_t5_s5",
        )
        assert res.total_signals == 4
        assert res.passed_signals == 3
        assert res.stopped_false_positives == 1
        assert res.retained_true_positives == 2
        assert res.filtered_win_rate == pytest.approx(66.67, rel=1e-2)

    def test_composite_filter_improves_metrics(self, sample_signals_data):
        filters = build_default_filters()
        composite = next(f for f in filters if f[0] == "CompositeFilter")
        res = RiskFilterEngine.evaluate_filter(
            sample_signals_data,
            composite[2],
            composite[0],
            composite[1],
            scenario_key="h5_t5_s5",
        )
        assert res.passed_signals == 2
        assert res.filtered_win_rate == 100.0
        assert res.stopped_false_positives == 2

    def test_r6_missing_or_invalid_features_rejected_by_filters(self):
        """빈 딕셔너리({})나 결측/NaN 피처가 위험 필터를 통과하지 않는지 검증."""
        assert pass_pattern_filter({}) is False
        assert pass_goldilocks_atr_filter({}) is False
        assert pass_min_score_filter({}) is False
        assert pass_composite_filter({}) is False
        assert pass_meta_filter({}) is False

        assert pass_composite_filter({"pattern_type": "base_breakout", "extension_atr": None, "score": 80.0}) is False
        assert pass_composite_filter({"pattern_type": "base_breakout", "extension_atr": 2.5, "score": None}) is False
        assert pass_composite_filter({"pattern_type": "base_breakout", "extension_atr": float("nan"), "score": 80.0}) is False
        assert pass_composite_filter({"pattern_type": "base_breakout", "extension_atr": 2.5, "score": 80.0}) is True


class TestReportGeneration:
    def test_generate_report_text_and_r3_dynamic_updates(self, sample_signals_data):
        report_4 = generate_analysis_report(sample_signals_data, "h5_t5_s5")
        assert "손익비 & 보유기간 매트릭스 그리드 성과" in report_4
        assert "피처 변별력 분석" in report_4
        assert "2차 위험 필터(Meta-Classifier) 탐색 가설 검증" in report_4
        assert "CompositeFilter" in report_4
        assert "완료 유효 표본(`h5_t5_s5`)**: `4건`" in report_4
        assert "`rebound` 익절 `0건` / 손절 `1건`" in report_4

        # R3 회귀 검증: 하드코딩된 과거 문구가 없어야 하며 입력 변경 시 서술이 동적으로 바뀌어야 함
        assert "38.9%" not in report_4
        assert "현재 표본은 18건" not in report_4
        assert "관측된 5건 전량 손절" not in report_4

        # 반등형(rebound) 익절 사례를 추가했을 때 보고서 내 패턴 집계가 동적으로 변경되는지 확인
        modified = list(sample_signals_data) + [
            {
                "signal_id": "sig_5_rebound_win",
                "strategy_version": "algorithm260917_v1",
                "code": "000005",
                "features": {"pattern_type": "rebound", "score": 78.0, "extension_atr": 2.1},
                "evaluations": {"h5_t5_s5": {"status": "TARGET_FIRST", "return_pct": 5.0}},
            }
        ]
        report_5 = generate_analysis_report(modified, "h5_t5_s5")
        assert "완료 유효 표본(`h5_t5_s5`)**: `5건`" in report_5
        assert "`rebound` 익절 `1건` / 손절 `1건`" in report_5
