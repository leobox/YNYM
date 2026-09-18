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
