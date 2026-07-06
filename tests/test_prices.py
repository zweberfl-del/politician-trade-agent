from __future__ import annotations

from datetime import date

from src.data.prices import leaderboard_from_performance, window_return


class TestWindowReturn:
    HISTORY = {
        "2026-06-01": 100.0,
        "2026-06-02": 110.0,
        "2026-06-05": 120.0,
    }

    def test_entry_on_disclosure_day(self) -> None:
        ret = window_return(self.HISTORY, date(2026, 6, 1))
        assert ret is not None
        assert abs(ret - 0.2) < 1e-9

    def test_entry_skips_to_next_trading_day(self) -> None:
        # Disclosed on a non-trading day — entry is the next close
        ret = window_return(self.HISTORY, date(2026, 6, 3))
        assert ret is not None
        assert abs(ret - 0.0) < 1e-9  # 120/120 - 1

    def test_disclosure_after_last_close(self) -> None:
        assert window_return(self.HISTORY, date(2026, 6, 10)) is None

    def test_empty_history(self) -> None:
        assert window_return({}, date(2026, 6, 1)) is None


class TestLeaderboard:
    def test_ranks_by_excess_and_applies_min_trades(self) -> None:
        performance = [
            {"politician_name": "A", "return_pct": 10.0, "spy_return_pct": 2.0, "excess_pct": 8.0},
            {"politician_name": "A", "return_pct": 6.0, "spy_return_pct": 2.0, "excess_pct": 4.0},
            {"politician_name": "B", "return_pct": 30.0, "spy_return_pct": 2.0, "excess_pct": 28.0},
            {"politician_name": "C", "return_pct": 1.0, "spy_return_pct": 2.0, "excess_pct": -1.0},
            {"politician_name": "C", "return_pct": 3.0, "spy_return_pct": 2.0, "excess_pct": 1.0},
        ]
        board = leaderboard_from_performance(performance, min_trades=2)
        # B has only one trade and is dropped
        assert [r["politician_name"] for r in board] == ["A", "C"]
        assert board[0]["avg_excess_pct"] == 6.0
        assert board[0]["trades"] == 2
