from research.bias_reporter import get_bias_warnings


class WalkForwardTest:
    """Train/validate/test session-window harness.

    `signal_func` and `account_func` are adapters bound (e.g. via
    functools.partial) to concrete implementations such as
    `research.signal_generator.generate_signals` and
    `research.account_simulator.simulate_account`:
      - signal_func(data, names, sessions) -> entries DataFrame
      - account_func(entries, data, sessions) -> (result_dict, trades_df, equity_df)

    Any extra keyword the underlying function needs (fee, mode, days, ...)
    must be pre-bound by the caller. In particular, `generate_signals`
    defaults to scanning only its last 10 sessions (`days=10`) — bind a
    `days` large enough to cover the full window, or this harness will
    silently under-scan a longer period.
    """

    def __init__(self, train_sessions, validate_sessions, test_sessions):
        train_set = set(train_sessions)
        validate_set = set(validate_sessions)
        test_set = set(test_sessions)
        leaks = {
            "train/validate": sorted(train_set & validate_set),
            "train/test": sorted(train_set & test_set),
            "validate/test": sorted(validate_set & test_set),
        }
        leaks = {pair: days for pair, days in leaks.items() if days}
        if leaks:
            raise ValueError(f"Overlapping sessions between periods (data leakage): {leaks}")

        self.train_sessions = list(train_sessions)
        self.validate_sessions = list(validate_sessions)
        self.test_sessions = list(test_sessions)
        # Sessions already consumed by a prior run() on this instance. Re-running
        # a period that reuses these is not a fresh out-of-sample look.
        self._seen_sessions = set()

    def _run_period(self, name, sessions, signal_func, account_func, data, names):
        if not sessions:
            return {"period": name, "session_count": 0, "note": "no sessions in this window"}

        repeated_samples = bool(self._seen_sessions & set(sessions))
        self._seen_sessions.update(sessions)

        entries = signal_func(data, names, sessions)
        if len(entries) == 0:
            # Zero eligible candidates is allowed (see AGENTS.md); skip the
            # account simulator rather than feed it a column-less DataFrame.
            result, trades, equity = {"closed_trades": 0}, entries, entries
        else:
            result, trades, equity = account_func(entries, data, sessions)

        return {
            "period": name,
            "session_count": len(sessions),
            "start": str(min(sessions)),
            "end": str(max(sessions)),
            "signal_count": len(entries),
            "result": result,
            "trades": trades,
            "equity": equity,
            "warnings": get_bias_warnings(
                trade_count=result.get("closed_trades", 0),
                repeated_samples=repeated_samples,
            ).splitlines(),
        }

    def run(self, signal_func, account_func, data, names):
        """
        Runs signal generation + account sim on each period separately.
        Periods never share sessions (enforced in __init__), and each
        period's warnings flag whether this instance already consumed
        those sessions in an earlier run() call (repeated-sample warning).
        Per-period results are independent: no equity curve is chained
        across periods, and no CAGR/Sharpe is fabricated for short windows.
        """
        periods = {
            "train": self._run_period("train", self.train_sessions, signal_func, account_func, data, names),
            "validate": self._run_period("validate", self.validate_sessions, signal_func, account_func, data, names),
            "test": self._run_period("test", self.test_sessions, signal_func, account_func, data, names),
        }
        closed_trades = sum(
            p["result"].get("closed_trades", 0) for p in periods.values() if "result" in p
        )
        return {
            **periods,
            "aggregate": {
                "total_closed_trades": closed_trades,
                "note": "Per-period results only; periods are not chained into one equity curve.",
            },
        }
