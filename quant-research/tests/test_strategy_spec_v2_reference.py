"""Regression checks for the Python reference embedded in the official LRM-60 spec."""

from pathlib import Path

import pandas as pd
import pytest


SPEC = Path(__file__).resolve().parents[1] / "docs" / "strategy_spec_v2_robust.md"
source = SPEC.read_text(encoding="utf-8").split("```python", 1)[1].split("```", 1)[0]
namespace = {}
exec(compile(source, str(SPEC), "exec"), namespace)
Engine = namespace["RobustQuantEngine"]

SIGNAL_TS = pd.Timestamp("2026-09-23 15:00", tz="Asia/Seoul")
NEXT_TS = pd.Timestamp("2026-09-24 09:00", tz="Asia/Seoul")


def frame(open_, high, low, close, volume=1000, ts=NEXT_TS):
    return pd.DataFrame(
        {"Open": [open_], "High": [high], "Low": [low],
         "Close": [close], "Volume": [volume]},
        index=[ts],
    )


def holding(code="A", peak=100.0, qty=1):
    return {
        "code": code, "entry_ts": SIGNAL_TS, "entry_price": 100.0,
        "breakout_level": 90.0, "peak_price": peak,
        "last_close": 100.0, "bars_held": 5,
        "qty": qty, "cost": qty * 100.115,
    }


def test_trailing_gap_fills_at_open_not_untraded_trailing_price():
    engine = Engine({"A": frame(105, 106, 104, 105)})
    engine.positions["A"] = holding(peak=110)

    engine.step(SIGNAL_TS, NEXT_TS)

    trade = engine.trades[0]
    assert trade["reason"] == "TRAILING_GAP"
    assert trade["exit_price"] == pytest.approx(105 * (1 - engine.slippage))
    assert trade["exit_price"] <= 106


def test_active_trailing_stop_wins_ambiguous_intrabar_target_touch():
    engine = Engine({"A": frame(105, 109, 103, 106)})
    engine.positions["A"] = holding(peak=106)

    engine.step(SIGNAL_TS, NEXT_TS)

    assert engine.trades[0]["reason"] == "TRAILING_STOP"


def test_zero_trade_bar_cannot_fill_held_position():
    engine = Engine({"A": frame(90, 90, 90, 90, volume=0)})
    engine.positions["A"] = holding()

    engine.step(SIGNAL_TS, NEXT_TS)

    assert not engine.trades
    assert "A" in engine.positions
    assert pd.isna(engine.equity)
    assert engine.data_gaps[-1]["codes"] == ["A"]


def test_1500_zero_volume_display_bar_is_rejected():
    display_ts = pd.Timestamp("2026-09-24 15:00", tz="Asia/Seoul")
    with pytest.raises(ValueError, match="display bars"):
        Engine({"A": frame(100, 100, 100, 100, volume=0, ts=display_ts)})


def test_exit_does_not_reuse_slot_at_same_bar_open():
    data = {
        "A": frame(90, 100, 89, 95),
        "B": frame(100, 101, 99, 100),
        "C": frame(100, 101, 99, 100),
        "NEW": frame(100, 101, 99, 100),
    }
    engine = Engine(data)
    for code in ("A", "B", "C"):
        engine.positions[code] = holding(code)
    engine.evaluate_signals = lambda *args, **kwargs: [
        {"code": "NEW", "breakout_level": 90.0, "signal_close": 100.0,
         "r_vol": 9.0}
    ]

    engine.step(SIGNAL_TS, NEXT_TS)

    assert engine.trades[0]["reason"] == "STOP_GAP"
    assert "NEW" not in engine.positions


def test_period_end_liquidation_reports_unfilled_holding():
    engine = Engine({
        "A": frame(105, 106, 104, 105),
        "B": frame(100, 101, 99, 100, ts=SIGNAL_TS),
    })
    engine.positions["A"] = holding("A")
    engine.positions["B"] = holding("B")

    unresolved = engine.finalize_at_open(NEXT_TS)

    assert unresolved == ["B"]
    assert engine.trades[0]["reason"] == "PERIOD_END"
    assert engine.trades[0]["exit_price"] == pytest.approx(105 * (1 - engine.slippage))
    assert "B" in engine.positions
    assert pd.isna(engine.equity)
