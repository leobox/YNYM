from tracker import SignalTracker


def make_tracker(tmp_path):
    return SignalTracker(tmp_path)


class TestRegisterSignalDedup:
    def test_same_bar_time_is_idempotent(self, tmp_path):
        tracker = make_tracker(tmp_path)
        first = tracker.register_signal(
            strategy_version="algorithm260917_v1", code="420770", name="기가비스",
            market="KOSDAQ", bar_time_kst="2026-09-18 09:00", features={}, entry_reference_price=100000.0,
        )
        second = tracker.register_signal(
            strategy_version="algorithm260917_v1", code="420770", name="기가비스",
            market="KOSDAQ", bar_time_kst="2026-09-18 09:00", features={}, entry_reference_price=100000.0,
        )
        assert first is not None
        assert second is None
        assert len(tracker.pending_signals) == 1

    def test_next_bar_does_not_register_while_still_active(self, tmp_path):
        tracker = make_tracker(tmp_path)
        first = tracker.register_signal(
            strategy_version="algorithm260917_v1", code="090710", name="휴림로봇",
            market="KOSDAQ", bar_time_kst="2026-09-18 09:00", features={}, entry_reference_price=6400.0,
        )
        second = tracker.register_signal(
            strategy_version="algorithm260917_v1", code="090710", name="휴림로봇",
            market="KOSDAQ", bar_time_kst="2026-09-18 10:00", features={}, entry_reference_price=6390.0,
        )
        assert first is not None
        assert second is None  # 아직 미해소 상태이므로 다음 봉에서 재등록되면 안 됨
        assert len(tracker.pending_signals) == 1

    def test_different_strategy_can_track_same_code_independently(self, tmp_path):
        tracker = make_tracker(tmp_path)
        main_sig = tracker.register_signal(
            strategy_version="algorithm260917_v1", code="420770", name="기가비스",
            market="KOSDAQ", bar_time_kst="2026-09-18 09:00", features={}, entry_reference_price=100000.0,
        )
        experimental_sig = tracker.register_signal(
            strategy_version="volume_zscore_accel_v1", code="420770", name="기가비스",
            market="KOSDAQ", bar_time_kst="2026-09-18 09:00", features={}, entry_reference_price=100000.0,
        )
        assert main_sig is not None
        assert experimental_sig is not None
        assert len(tracker.pending_signals) == 2

    def test_registers_again_once_prior_signal_is_resolved(self, tmp_path):
        tracker = make_tracker(tmp_path)
        first = tracker.register_signal(
            strategy_version="algorithm260917_v1", code="420770", name="기가비스",
            market="KOSDAQ", bar_time_kst="2026-09-18 09:00", features={}, entry_reference_price=100000.0,
        )
        tracker.pending_signals[first]["is_fully_resolved"] = True

        second = tracker.register_signal(
            strategy_version="algorithm260917_v1", code="420770", name="기가비스",
            market="KOSDAQ", bar_time_kst="2026-09-18 10:00", features={}, entry_reference_price=101000.0,
        )
        assert second is not None
        assert second != first


class TestGetSummaryStatsRefKey:
    def _write_resolved(self, tracker, strategy_version, evaluations):
        record = {
            "strategy_version": strategy_version,
            "evaluations": evaluations,
        }
        with open(tracker.resolved_file, "a", encoding="utf-8") as f:
            f.write(__import__("json").dumps(record, ensure_ascii=False) + "\n")

    def test_defaults_to_h5_t535_s5(self, tmp_path):
        tracker = make_tracker(tmp_path)
        self._write_resolved(tracker, "s1", {
            "h5_t535_s5": {"status": "TARGET_FIRST"},
            "h5_t535_s3": {"status": "STOP_FIRST"},
        })
        stats = tracker.get_summary_stats(strategy_version="s1")
        assert stats["ref_benchmark"] == "h5_t535_s5"
        assert stats["target_first"] == 1
        assert stats["stop_first"] == 0

    def test_custom_ref_key_changes_counted_outcome(self, tmp_path):
        tracker = make_tracker(tmp_path)
        self._write_resolved(tracker, "s1", {
            "h5_t10_s5": {"status": "TARGET_FIRST"},
            "h5_t10_s3": {"status": "STOP_FIRST"},
        })
        stats = tracker.get_summary_stats(strategy_version="s1", ref_key="h5_t10_s3")
        assert stats["ref_benchmark"] == "h5_t10_s3"
        assert stats["target_first"] == 0
        assert stats["stop_first"] == 1


class TestUpdateWithBarsRegistrationCutoff:
    def test_ignores_bars_prior_to_registered_at_kst(self, tmp_path):
        from datetime import datetime
        from tracker import KST

        tracker = make_tracker(tmp_path)
        sig_id = tracker.register_signal(
            strategy_version="algorithm260917_v1",
            code="420770",
            name="기가비스",
            market="KOSDAQ",
            bar_time_kst="2026-09-18 09:00",
            features={"signal_stage": "watch"},
            entry_reference_price=10000.0,
            registered_at_kst="2026-09-18 10:41:46",
        )
        assert sig_id is not None

        # 09:05~10:40 봉은 등록 시각(10:41:46) 이전이므로 손절/익절 평가에서 제외되어야 함
        candles = [
            {"time_kst": "2026-09-18 09:05", "open": 9900.0, "high": 9950.0, "low": 9000.0, "close": 9100.0, "volume": 1000},
            {"time_kst": "2026-09-18 10:40", "open": 10000.0, "high": 12000.0, "low": 9950.0, "close": 11500.0, "volume": 1500},
            {"time_kst": "2026-09-18 10:45", "open": 10100.0, "high": 10600.0, "low": 10050.0, "close": 10550.0, "volume": 2000},
        ]
        now_dt = datetime(2026, 9, 18, 10, 50, tzinfo=KST)
        tracker.update_with_bars({"420770": candles}, now_dt)

        sig = tracker.pending_signals[sig_id]
        assert sig["effective_eval_start_kst"] == "2026-09-18 10:41"
        assert sig["highest_seen"] == 10600.0
        assert sig["lowest_seen"] == 10000.0
        assert sig["evaluations"]["h5_t3_s3"]["status"] == "TARGET_FIRST"
        assert sig["evaluations"]["h5_t3_s3"]["exit_time"] == "2026-09-18 10:45"
        assert sig["evaluations"]["h5_t535_s5"]["status"] == "TARGET_FIRST"
        assert sig["evaluations"]["h5_t535_s5"]["exit_time"] == "2026-09-18 10:45"
        assert sig["evaluations"]["h5_t10_s5"]["status"] == "PENDING"

    def test_same_run_without_post_registration_bars_stays_pending(self, tmp_path):
        from datetime import datetime
        from tracker import KST

        tracker = make_tracker(tmp_path)
        sig_id = tracker.register_signal(
            strategy_version="algorithm260917_v1",
            code="090710",
            name="휴림로봇",
            market="KOSDAQ",
            bar_time_kst="2026-09-18 09:00",
            features={"signal_stage": "top5_hold"},
            entry_reference_price=10000.0,
            registered_at_kst="2026-09-18 10:41:46",
        )
        candles = [
            {"time_kst": "2026-09-18 09:05", "open": 9500.0, "high": 9600.0, "low": 9000.0, "close": 9100.0, "volume": 500},
            {"time_kst": "2026-09-18 10:40", "open": 9200.0, "high": 9400.0, "low": 9100.0, "close": 9300.0, "volume": 600},
        ]
        now_dt = datetime(2026, 9, 18, 10, 41, 46, tzinfo=KST)
        tracker.update_with_bars({"090710": candles}, now_dt)

        sig = tracker.pending_signals[sig_id]
        assert sig["effective_eval_start_kst"] == "2026-09-18 10:41"
        assert sig["trading_days_observed"] == 0
        assert sig["evaluations"]["h5_t535_s5"]["status"] == "PENDING"

