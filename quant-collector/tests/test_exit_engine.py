from exit_engine import ExitSignal, evaluate_position_exit

NOW = "2026-09-17 12:00"


def make_position(entry=10000.0, breakout_level=9000.0, target_10=None, stop_5=None,
                   highest_seen=None, days_held=0, signal_time="2026-09-17 10:00"):
    return {
        "code": "000000",
        "name": "TEST",
        "entry_reference_price": entry,
        "features": {"breakout_level": breakout_level},
        "targets": {"tgt_10": target_10 if target_10 is not None else entry * 1.10},
        "stops": {"stop_5": stop_5 if stop_5 is not None else entry * 0.95},
        "highest_seen": highest_seen if highest_seen is not None else entry,
        "trading_days_observed": days_held,
        "signal_time_kst": signal_time,
    }


def make_bar(time_kst, open, high, low, close):
    return {"time_kst": time_kst, "open": open, "high": high, "low": low, "close": close}


class TestHardStop:
    def test_triggers_on_current_price_touch(self):
        position = make_position(entry=10000, stop_5=9500, target_10=11000)
        result = evaluate_position_exit(position, [], 9400.0, NOW)
        assert result["decision"] == ExitSignal.HARD_STOP
        assert result["action_type"] == "CUT_LOSS"
        assert result["urgency"] == "IMMEDIATE"

    def test_triggers_on_bar_low_touch_even_if_current_price_recovered(self):
        position = make_position(entry=10000, stop_5=9500, target_10=11000)
        bars = [make_bar("2026-09-17 11:00", open=9600, high=9650, low=9400, close=9600)]
        result = evaluate_position_exit(position, bars, 9600.0, NOW)
        assert result["decision"] == ExitSignal.HARD_STOP


class TestTargetHit:
    def test_triggers_on_current_price(self):
        position = make_position(entry=10000, stop_5=9000, target_10=11000)
        result = evaluate_position_exit(position, [], 11100.0, NOW)
        assert result["decision"] == ExitSignal.TARGET_HIT
        assert result["action_type"] == "TAKE_PROFIT"
        assert result["suggested_price"] == 11000.0

    def test_triggers_on_bar_high_even_if_current_price_pulled_back(self):
        position = make_position(entry=10000, stop_5=9000, target_10=11000)
        bars = [make_bar("2026-09-17 11:00", open=10800, high=11050, low=10500, close=10900)]
        result = evaluate_position_exit(position, bars, 10900.0, NOW)
        assert result["decision"] == ExitSignal.TARGET_HIT

    def test_triggers_on_five_thirty_five_pct_target(self):
        position = {
            "code": "218410",
            "name": "RFHIC",
            "entry_reference_price": 10000.0,
            "targets": {"tgt_5_35": 10535.0},
            "stops": {"stop_5": 9500.0},
            "highest_seen": 10600.0,
            "trading_days_observed": 1,
            "signal_time_kst": "2026-09-17 09:00",
        }
        result = evaluate_position_exit(position, [], 10550.0, NOW)
        assert result["decision"] == ExitSignal.TARGET_HIT
        assert result["action_type"] == "TAKE_PROFIT"
        assert result["suggested_price"] == 10535.0


class TestTrailingProfit:
    def test_triggers_after_five_pct_gain_and_three_pct_retreat(self):
        position = make_position(entry=10000, stop_5=8000, target_10=13000, highest_seen=11000)
        result = evaluate_position_exit(position, [], 10600.0, NOW)
        assert result["decision"] == ExitSignal.TRAILING_PROFIT
        assert result["action_type"] == "TAKE_PROFIT"
        assert result["max_gain_pct"] == 10.0

    def test_does_not_trigger_below_five_pct_gain(self):
        position = make_position(entry=10000, stop_5=8000, target_10=13000, highest_seen=10300)
        result = evaluate_position_exit(position, [], 9900.0, NOW)
        assert result["decision"] != ExitSignal.TRAILING_PROFIT


class TestBreakoutCollapse:
    def test_triggers_when_bar_closes_below_breakout_level(self):
        position = make_position(entry=10000, breakout_level=9800, stop_5=8000, target_10=13000)
        bars = [make_bar("2026-09-17 11:00", open=9900, high=9950, low=9650, close=9700)]
        result = evaluate_position_exit(position, bars, 9850.0, NOW)
        assert result["decision"] == ExitSignal.BREAKOUT_COLLAPSE
        assert result["action_type"] == "CUT_LOSS"

    def test_ambiguous_when_breach_is_marginal(self):
        position = make_position(entry=10000, breakout_level=9800, stop_5=8000, target_10=13000)
        bars = [make_bar("2026-09-17 11:00", open=9850, high=9900, low=9740, close=9760)]
        result = evaluate_position_exit(position, bars, 9800.0, NOW)
        assert result["decision"] == ExitSignal.BREAKOUT_AMBIGUOUS
        assert result["action_type"] == "KEEP"
        assert result["urgency"] == "MEDIUM"


class TestVolumeBear:
    def test_triggers_on_sharp_bearish_bar_while_in_loss(self):
        position = make_position(entry=10000, breakout_level=9000, stop_5=8000, target_10=13000)
        bars = [make_bar("2026-09-17 11:00", open=10100, high=10150, low=9800, close=9900)]
        result = evaluate_position_exit(position, bars, 9900.0, NOW)
        assert result["decision"] == ExitSignal.VOLUME_BEAR
        assert result["action_type"] == "CUT_LOSS"

    def test_does_not_trigger_while_in_profit(self):
        position = make_position(entry=10000, breakout_level=9000, stop_5=8000, target_10=13000)
        bars = [make_bar("2026-09-17 11:00", open=10300, high=10350, low=10050, close=10100)]
        result = evaluate_position_exit(position, bars, 10100.0, NOW)
        assert result["decision"] != ExitSignal.VOLUME_BEAR


class TestStagnation:
    def test_take_profit_when_flat_and_nonnegative(self):
        position = make_position(entry=10000, breakout_level=8000, stop_5=7000, target_10=13000, days_held=2)
        bars = [make_bar("2026-09-17 11:00", open=10000, high=10080, low=9950, close=10050)]
        result = evaluate_position_exit(position, bars, 10050.0, NOW)
        assert result["decision"] == ExitSignal.STAGNATION
        assert result["action_type"] == "TAKE_PROFIT"

    def test_cut_loss_when_flat_and_negative(self):
        position = make_position(entry=10000, breakout_level=8000, stop_5=7000, target_10=13000, days_held=3)
        bars = [make_bar("2026-09-17 11:00", open=9900, high=9980, low=9880, close=9950)]
        result = evaluate_position_exit(position, bars, 9950.0, NOW)
        assert result["decision"] == ExitSignal.STAGNATION
        assert result["action_type"] == "CUT_LOSS"

    def test_does_not_trigger_before_two_days_held(self):
        position = make_position(entry=10000, breakout_level=8000, stop_5=7000, target_10=13000, days_held=1)
        bars = [make_bar("2026-09-17 11:00", open=9900, high=9980, low=9880, close=9950)]
        result = evaluate_position_exit(position, bars, 9950.0, NOW)
        assert result["decision"] != ExitSignal.STAGNATION


class TestCautionAndHold:
    def test_caution_near_breakout_level(self):
        position = make_position(entry=10000, breakout_level=9800, stop_5=8000, target_10=13000, days_held=0)
        bars = [make_bar("2026-09-17 11:00", open=9800, high=9900, low=9750, close=9850)]
        result = evaluate_position_exit(position, bars, 9850.0, NOW)
        assert result["decision"] == ExitSignal.CAUTION
        assert result["action_type"] == "KEEP"

    def test_hold_when_nothing_triggers(self):
        position = make_position(entry=10000, breakout_level=9000, stop_5=8000, target_10=13000, days_held=0)
        bars = [make_bar("2026-09-17 11:00", open=10100, high=10250, low=10050, close=10200)]
        result = evaluate_position_exit(position, bars, 10200.0, NOW)
        assert result["decision"] == ExitSignal.HOLD
        assert result["action_type"] == "KEEP"
        assert result["urgency"] == "LOW"


class TestBarSelection:
    def test_ignores_bars_at_or_before_signal_time_and_uses_latest_future_bar(self):
        # A pre-signal bar and a stale (non-latest) post-signal bar both look like a
        # hard-stop touch; only the truly latest post-signal bar should be consulted.
        position = make_position(entry=10000, breakout_level=8000, stop_5=9000, target_10=13000,
                                  signal_time="2026-09-17 10:00")
        bars = [
            make_bar("2026-09-17 09:00", open=9500, high=9600, low=100, close=9500),
            make_bar("2026-09-17 10:30", open=9500, high=9600, low=100, close=9500),
            make_bar("2026-09-17 11:30", open=9450, high=9500, low=9400, close=9480),
        ]
        result = evaluate_position_exit(position, bars, 9500.0, NOW)
        assert result["decision"] == ExitSignal.HOLD
