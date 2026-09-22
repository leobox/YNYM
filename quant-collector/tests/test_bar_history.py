import pandas as pd

import collector as c


def _make_df(times, start_price=10000.0):
    idx = pd.DatetimeIndex(times, tz="Asia/Seoul")
    n = len(idx)
    return pd.DataFrame(
        {
            "Open": [start_price + i for i in range(n)],
            "High": [start_price + i + 50 for i in range(n)],
            "Low": [start_price + i - 50 for i in range(n)],
            "Close": [start_price + i + 10 for i in range(n)],
            "Volume": [1000 + i for i in range(n)],
        },
        index=idx,
    )


class TestSaveBarHistory:
    def test_creates_file_on_first_save(self, tmp_path, monkeypatch):
        monkeypatch.setattr(c, "BARS_HISTORY_DIR", tmp_path)
        df = _make_df(["2026-09-18 09:00", "2026-09-18 10:00"])
        c.save_bar_history("000000", df)

        saved = pd.read_csv(tmp_path / "000000.csv", index_col=0)
        assert len(saved) == 2

    def test_appends_only_new_rows_on_rerun(self, tmp_path, monkeypatch):
        monkeypatch.setattr(c, "BARS_HISTORY_DIR", tmp_path)
        first = _make_df(["2026-09-18 09:00", "2026-09-18 10:00"])
        c.save_bar_history("000000", first)

        # 다음 스캔: 기존 2봉 + 신규 1봉을 포함한 전체 윈도우가 다시 들어옴 (fetch_bars 특성)
        second = _make_df(["2026-09-18 09:00", "2026-09-18 10:00", "2026-09-18 11:00"])
        c.save_bar_history("000000", second)

        saved = pd.read_csv(tmp_path / "000000.csv", index_col=0)
        assert len(saved) == 3  # 중복 없이 신규 1봉만 추가됨

    def test_rerun_with_no_new_bars_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(c, "BARS_HISTORY_DIR", tmp_path)
        df = _make_df(["2026-09-18 09:00", "2026-09-18 10:00"])
        c.save_bar_history("000000", df)
        c.save_bar_history("000000", df)  # 신규 봉 없이 동일 윈도우로 재실행

        saved = pd.read_csv(tmp_path / "000000.csv", index_col=0)
        assert len(saved) == 2

    def test_separate_codes_get_separate_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(c, "BARS_HISTORY_DIR", tmp_path)
        df = _make_df(["2026-09-18 09:00"])
        c.save_bar_history("000000", df)
        c.save_bar_history("111111", df)

        assert (tmp_path / "000000.csv").exists()
        assert (tmp_path / "111111.csv").exists()

    def test_multiframe_save_creates_distinct_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(c, "BARS_HISTORY_DIR", tmp_path)
        df_60 = _make_df(["2026-09-18 09:00"])
        df_30 = _make_df(["2026-09-18 09:00", "2026-09-18 09:30"])
        df_10 = _make_df(["2026-09-18 09:00", "2026-09-18 09:10", "2026-09-18 09:20"])
        df_5 = _make_df(["2026-09-18 09:00", "2026-09-18 09:05", "2026-09-18 09:10", "2026-09-18 09:15"])

        c.save_bar_history("005930", df_60, "60m")
        c.save_bar_history("005930", df_30, "30m")
        c.save_bar_history("005930", df_10, "10m")
        c.save_bar_history("005930", df_5, "5m")

        assert (tmp_path / "005930.csv").exists()
        assert (tmp_path / "005930_30m.csv").exists()
        assert (tmp_path / "005930_10m.csv").exists()
        assert (tmp_path / "005930_5m.csv").exists()

        assert len(pd.read_csv(tmp_path / "005930.csv", index_col=0)) == 1
        assert len(pd.read_csv(tmp_path / "005930_30m.csv", index_col=0)) == 2
        assert len(pd.read_csv(tmp_path / "005930_10m.csv", index_col=0)) == 3
        assert len(pd.read_csv(tmp_path / "005930_5m.csv", index_col=0)) == 4

    def test_parse_multiframe_bars_consistency(self):
        # 09:00부터 10:00까지 13개 5분봉 목 데이터 생성
        times = [
            "2026-09-18 09:00", "2026-09-18 09:05", "2026-09-18 09:10", "2026-09-18 09:15",
            "2026-09-18 09:20", "2026-09-18 09:25", "2026-09-18 09:30", "2026-09-18 09:35",
            "2026-09-18 09:40", "2026-09-18 09:45", "2026-09-18 09:50", "2026-09-18 09:55",
            "2026-09-18 10:00",
        ]
        timestamps = [int(pd.Timestamp(t, tz="Asia/Seoul").timestamp()) for t in times]
        item = {
            "timestamp": timestamps,
            "indicators": {
                "quote": [{
                    "open": [1000.0 + i for i in range(len(times))],
                    "high": [1050.0 + i for i in range(len(times))],
                    "low": [990.0 + i for i in range(len(times))],
                    "close": [1010.0 + i for i in range(len(times))],
                    "volume": [100 for _ in range(len(times))],
                }]
            }
        }
        now = pd.Timestamp("2026-09-18 11:00", tz="Asia/Seoul")
        bars = c.parse_multiframe_bars(item, now)

        assert "5m" in bars and "10m" in bars and "30m" in bars and "60m" in bars
        assert len(bars["5m"]) == 13
        # 10m: 09:00, 09:10, 09:20, 09:30, 09:40, 09:50, 10:00 -> 7개
        assert len(bars["10m"]) == 7
        # 30m: 09:00, 09:30, 10:00 -> 3개
        assert len(bars["30m"]) == 3
        # 60m: 09:00, 10:00 -> 2개
        assert len(bars["60m"]) == 2

