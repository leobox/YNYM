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
