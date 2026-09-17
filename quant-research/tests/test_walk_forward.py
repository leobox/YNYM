from functools import partial

import pytest

from research.account_simulator import simulate_account
from research.signal_generator import generate_signals
from research.walk_forward import WalkForwardTest
from scanner.pattern import hourly_pattern


def _split(sessions_list):
    train = sessions_list[:15]
    validate = sessions_list[15:22]
    test = sessions_list[22:]
    return train, validate, test


class TestWalkForwardConstruction:
    def test_rejects_overlapping_sessions(self, sessions_list):
        train, validate, test = _split(sessions_list)
        with pytest.raises(ValueError):
            WalkForwardTest(train, validate + [train[-1]], test)

    def test_accepts_disjoint_sessions(self, sessions_list):
        train, validate, test = _split(sessions_list)
        wf = WalkForwardTest(train, validate, test)
        assert wf.train_sessions == train
        assert wf.validate_sessions == validate
        assert wf.test_sessions == test


class TestWalkForwardRun:
    def _harness(self, sessions_list):
        train, validate, test = _split(sessions_list)
        return WalkForwardTest(train, validate, test)

    def _adapters(self):
        signal_adapter = partial(generate_signals, days=999, signal_func=hourly_pattern)
        account_adapter = partial(simulate_account, mode="time")
        return signal_adapter, account_adapter

    def test_runs_each_period_independently(self, sample_ohlcv, sessions_list):
        wf = self._harness(sessions_list)
        signal_adapter, account_adapter = self._adapters()
        data = {"000000": sample_ohlcv}
        names = {"000000": "TEST"}

        out = wf.run(signal_adapter, account_adapter, data, names)

        for period in ("train", "validate", "test"):
            assert out[period]["period"] == period
            assert "result" in out[period]
            assert "closed_trades" in out[period]["result"]
            assert out[period]["warnings"]
        assert out["aggregate"]["total_closed_trades"] >= 0

    def test_rerunning_same_instance_flags_repeated_samples(self, sample_ohlcv, sessions_list):
        wf = self._harness(sessions_list)
        signal_adapter, account_adapter = self._adapters()
        data = {"000000": sample_ohlcv}
        names = {"000000": "TEST"}

        first = wf.run(signal_adapter, account_adapter, data, names)
        second = wf.run(signal_adapter, account_adapter, data, names)

        assert not any("Repeated sample" in w for w in first["train"]["warnings"])
        assert any("Repeated sample" in w for w in second["train"]["warnings"])

    def test_empty_window_is_reported_not_crashed(self, sample_ohlcv):
        wf = WalkForwardTest([], [], [])
        signal_adapter, account_adapter = self._adapters()
        data = {"000000": sample_ohlcv}
        names = {"000000": "TEST"}

        out = wf.run(signal_adapter, account_adapter, data, names)

        for period in ("train", "validate", "test"):
            assert out[period]["session_count"] == 0
        assert out["aggregate"]["total_closed_trades"] == 0
