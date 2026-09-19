import pandas as pd
import pytest
from research.account_simulator import simulate_account

class TestMultiPositionAccount:
    def test_cash_conservation_multi_position(self, sample_ohlcv, sessions_list):
        """Verify cash conservation holds for multi-position simulation."""
        # Create 3 synthetic stocks
        data = {
            f"00000{i}": sample_ohlcv.copy() for i in range(3)
        }
        # Create staggered entries
        entries = []
        for i, code in enumerate(data):
            idx = 20 + i * 5
            entries.append({
                "scan": str(sample_ohlcv.index[idx]),
                "code": code,
                "score": 90.0 - i,
            })
        entries_df = pd.DataFrame(entries)
        
        for mp in (1, 2, 3):
            res, trades, equity = simulate_account(entries_df, data, sessions_list, mode="time", max_positions=mp)
            assert "ending_cash" in res
            assert "ending_equity" in res
            assert res["max_positions"] == mp
            if not res["open_position"]:
                # When no open positions, cash must exactly equal equity
                assert abs(res["ending_cash"] - res["ending_equity"]) < 1.0
                total_pnl = trades["pnl"].sum() if not trades.empty else 0.0
                assert abs(1_000_000.0 + total_pnl - res["ending_cash"]) < 1.0

    def test_max_positions_respected(self, sample_ohlcv, sessions_list):
        """Verify concurrent positions never exceed max_positions."""
        data = {f"00000{i}": sample_ohlcv.copy() for i in range(5)}
        # All 5 stocks signal at the exact same bar
        scan_time = str(sample_ohlcv.index[30])
        entries = [{"scan": scan_time, "code": code, "score": 95.0 - i} for i, code in enumerate(data)]
        entries_df = pd.DataFrame(entries)

        for mp in (1, 2, 3):
            res, trades, equity = simulate_account(entries_df, data, sessions_list, mode="time", max_positions=mp)
            # In time mode with identical bars, all entered positions should close at the same time
            # Max simultaneous trades cannot exceed mp
            assert res["closed_trades"] <= mp
            assert res["skipped_signals"] == 5 - min(5, mp)
