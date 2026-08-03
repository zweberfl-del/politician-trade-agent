"""Polymarket unusual-activity detection: parsers, classifier, scanner."""

from __future__ import annotations

import time

import pytest

from src.analysis.prediction import classify_bet, classify_bets, wallet_signals
from src.data.polymarket import (
    PredictionTrade,
    WalletProfile,
    build_wallet_profile,
    parse_trade,
)
from src.data.prediction_scanner import PredictionScanner
from src.storage.migrations import run_migrations

NOW = 1_800_000_000.0  # fixed clock for deterministic ages


def _trade(usd: float = 25_000.0, price: float = 0.5, side: str = "BUY") -> PredictionTrade:
    return PredictionTrade(
        wallet="0xabc123",
        pseudonym="quiet-owl",
        market_title="Will X happen by March?",
        market_slug="will-x-happen",
        outcome="Yes",
        side=side,
        shares=usd / price,
        price=price,
        timestamp=int(NOW) - 60,
        tx_hash="0xdeadbeef",
    )


def _veteran(wins: int = 0, losses: int = 0, pnl: float = 0.0) -> WalletProfile:
    """A wallet with a year of history and plenty of activity."""
    return WalletProfile(
        wallet="0xabc123",
        first_seen_ts=int(NOW) - 365 * 24 * 3600,
        activity_count=400,
        wins=wins,
        losses=losses,
        total_pnl_usd=pnl,
    )


class TestWalletSignals:
    def test_fresh_wallet_by_age(self) -> None:
        profile = WalletProfile(
            wallet="0xabc", first_seen_ts=int(NOW) - 3600, activity_count=1
        )
        signals = wallet_signals(_trade(), profile, min_bet_usd=10_000, now=NOW)
        assert "fresh-wallet" in signals

    def test_fresh_wallet_by_low_activity(self) -> None:
        # Old first-seen but almost never used still reads as a burner
        profile = WalletProfile(
            wallet="0xabc", first_seen_ts=int(NOW) - 90 * 24 * 3600, activity_count=2
        )
        signals = wallet_signals(_trade(), profile, min_bet_usd=10_000, now=NOW)
        assert "fresh-wallet" in signals

    def test_veteran_wallet_not_fresh(self) -> None:
        signals = wallet_signals(_trade(), _veteran(), min_bet_usd=10_000, now=NOW)
        assert "fresh-wallet" not in signals

    def test_sharp_wallet_by_win_rate(self) -> None:
        profile = _veteran(wins=12, losses=2)
        signals = wallet_signals(_trade(), profile, min_bet_usd=10_000, now=NOW)
        assert "sharp-wallet" in signals

    def test_sharp_wallet_needs_track_record(self) -> None:
        # 3-0 is not enough resolved positions to call someone sharp
        profile = _veteran(wins=3, losses=0)
        signals = wallet_signals(_trade(), profile, min_bet_usd=10_000, now=NOW)
        assert "sharp-wallet" not in signals

    def test_sharp_wallet_by_big_pnl(self) -> None:
        profile = _veteran(wins=6, losses=3, pnl=250_000.0)
        signals = wallet_signals(_trade(), profile, min_bet_usd=10_000, now=NOW)
        assert "sharp-wallet" in signals

    def test_longshot_conviction(self) -> None:
        signals = wallet_signals(
            _trade(price=0.08), _veteran(), min_bet_usd=10_000, now=NOW
        )
        assert "longshot-conviction" in signals

    def test_selling_a_longshot_is_not_conviction(self) -> None:
        signals = wallet_signals(
            _trade(price=0.08, side="SELL"), _veteran(), min_bet_usd=10_000, now=NOW
        )
        assert "longshot-conviction" not in signals

    def test_whale_size(self) -> None:
        signals = wallet_signals(
            _trade(usd=60_000), _veteran(), min_bet_usd=10_000, now=NOW
        )
        assert "whale-size" in signals


class TestClassifyBet:
    def test_small_bet_never_alerts(self) -> None:
        fresh = WalletProfile(wallet="0xabc", first_seen_ts=int(NOW) - 60, activity_count=1)
        assert classify_bet(_trade(usd=500), fresh, min_bet_usd=10_000, now=NOW) is None

    def test_large_bet_from_boring_wallet_does_not_alert(self) -> None:
        # $25K from a mediocre veteran: big, but nothing suspicious
        profile = _veteran(wins=5, losses=5)
        assert classify_bet(_trade(), profile, min_bet_usd=10_000, now=NOW) is None

    def test_fresh_wallet_large_bet_alerts(self) -> None:
        fresh = WalletProfile(wallet="0xabc", first_seen_ts=int(NOW) - 7200, activity_count=1)
        bet = classify_bet(_trade(), fresh, min_bet_usd=10_000, now=NOW)
        assert bet is not None
        assert "fresh-wallet" in bet.signals

    def test_more_signals_score_higher(self) -> None:
        fresh = WalletProfile(wallet="0xabc", first_seen_ts=int(NOW) - 7200, activity_count=1)
        one = classify_bet(_trade(), fresh, min_bet_usd=10_000, now=NOW)
        two = classify_bet(_trade(price=0.08), fresh, min_bet_usd=10_000, now=NOW)
        assert one is not None and two is not None
        assert two.score > one.score

    def test_batch_ranked(self) -> None:
        fresh = WalletProfile(wallet="0xabc", first_seen_ts=int(NOW) - 7200, activity_count=1)
        sharp = _veteran(wins=12, losses=1)
        pairs = [
            (_trade(usd=15_000), fresh),
            (_trade(usd=90_000, price=0.08), sharp),
        ]
        hits = classify_bets(pairs, min_bet_usd=10_000, now=NOW)
        assert len(hits) == 2
        assert hits[0].trade.usd_size > hits[1].trade.usd_size


class TestParsers:
    def test_parse_trade(self) -> None:
        raw = {
            "proxyWallet": "0xABCDEF0123",
            "pseudonym": "quiet-owl",
            "title": "Will X happen?",
            "eventSlug": "will-x-happen",
            "outcome": "Yes",
            "side": "buy",
            "size": 50_000,
            "price": 0.42,
            "timestamp": 1_799_999_000,
            "transactionHash": "0xdead",
        }
        t = parse_trade(raw)
        assert t is not None
        assert t.wallet == "0xabcdef0123"
        assert t.side == "BUY"
        assert abs(t.usd_size - 21_000) < 1e-6
        assert t.market_url == "https://polymarket.com/event/will-x-happen"

    def test_parse_trade_without_wallet_is_none(self) -> None:
        assert parse_trade({"size": 10}) is None

    def test_build_wallet_profile(self) -> None:
        activity = [{"timestamp": 1_700_000_000}, {"timestamp": 1_750_000_000}]
        positions = [
            {"cashPnl": 1200.0, "redeemable": True},
            {"cashPnl": -300.0, "redeemable": True},
            {"cashPnl": 900.0},
            {"cashPnl": 0.0},  # open position: not resolved
        ]
        p = build_wallet_profile("0xABC", activity, positions)
        assert p.first_seen_ts == 1_700_000_000
        assert p.wins == 2 and p.losses == 1
        assert abs(p.total_pnl_usd - 1800.0) < 1e-6
        assert abs(p.win_rate - 2 / 3) < 1e-9


class FakePolymarketClient:
    def __init__(self, trades, profile) -> None:
        self.trades = trades
        self.profile = profile

    async def get_recent_trades(self):
        return self.trades

    async def get_wallet_profile(self, wallet):
        return self.profile


class FakeBot:
    def __init__(self) -> None:
        self.alerts = []
        self.surges = []

    async def send_prediction_alert(self, bet) -> None:
        self.alerts.append(bet)

    async def send_surge_alert(self, surge) -> None:
        self.surges.append(surge)


@pytest.fixture
async def scanner(tmp_path, monkeypatch):
    from src import config

    db_path = str(tmp_path / "pred.db")
    await run_migrations(db_path)
    monkeypatch.setattr(config.settings, "prediction_min_bet_usd", 10_000.0)

    fresh = WalletProfile(
        wallet="0xabc123", first_seen_ts=int(time.time()) - 3600, activity_count=1
    )
    client = FakePolymarketClient([_trade()], fresh)
    bot = FakeBot()
    return PredictionScanner(client=client, bot=bot, db_path=db_path), bot


class TestPredictionScanner:
    async def test_posts_and_stores_alert(self, scanner) -> None:
        pred_scanner, bot = scanner
        posted = await pred_scanner.scan_once()
        assert posted == 1
        assert bot.alerts[0].trade.wallet == "0xabc123"

        from src.storage.database import get_recent_prediction_events

        events = await get_recent_prediction_events(pred_scanner.db_path)
        assert events[0]["market_title"] == "Will X happen by March?"
        assert "fresh-wallet" in events[0]["signals"]

    async def test_same_bet_not_realerted(self, scanner) -> None:
        pred_scanner, bot = scanner
        await pred_scanner.scan_once()
        posted = await pred_scanner.scan_once()
        assert posted == 0
        assert len(bot.alerts) == 1

    async def test_scan_once_logs_raw_trades(self, scanner) -> None:
        pred_scanner, _bot = scanner
        await pred_scanner.scan_once()

        from src.storage.database import get_recent_prediction_trades

        rows = await get_recent_prediction_trades(pred_scanner.db_path, 0)
        assert len(rows) == 1
        assert rows[0]["market_slug"] == "will-x-happen"
        assert rows[0]["category"]  # tagged


def _surge_trade(wallet: str, tx: str, usd: float = 30_000.0) -> PredictionTrade:
    now = int(time.time())
    return PredictionTrade(
        wallet=wallet,
        pseudonym=wallet,
        market_title="Will the US strike Iran this month?",
        market_slug="us-strike-on-iran",
        outcome="Yes",
        side="BUY",
        shares=usd / 0.5,
        price=0.5,
        timestamp=now - 60,
        tx_hash=tx,
    )


@pytest.fixture
async def surge_scanner(tmp_path, monkeypatch):
    from src import config

    db_path = str(tmp_path / "surge.db")
    await run_migrations(db_path)
    monkeypatch.setattr(config.settings, "prediction_min_bet_usd", 10_000.0)
    monkeypatch.setattr(config.settings, "surge_min_wallets", 5)
    monkeypatch.setattr(config.settings, "surge_min_total_usd", 100_000.0)
    monkeypatch.setattr(config.settings, "enable_surge_alerts", True)

    trades = [_surge_trade(f"0xw{i}", f"0xtx{i}") for i in range(6)]  # 6 x $30K
    fresh = WalletProfile(
        wallet="0xw0", first_seen_ts=int(time.time()) - 3600, activity_count=1
    )
    client = FakePolymarketClient(trades, fresh)
    bot = FakeBot()
    return PredictionScanner(client=client, bot=bot, db_path=db_path), bot


class TestSurgeScanner:
    async def test_detects_and_alerts_surge(self, surge_scanner) -> None:
        pred_scanner, bot = surge_scanner
        await pred_scanner.scan_once()  # log raw fills
        posted = await pred_scanner.scan_surges()
        assert posted == 1
        assert bot.surges[0].wallet_count == 6
        assert bot.surges[0].category == "military"

        from src.storage.database import get_recent_surges

        surges = await get_recent_surges(pred_scanner.db_path)
        assert len(surges) == 1
        assert surges[0]["outcome"] == "Yes"

    async def test_surge_not_realerted(self, surge_scanner) -> None:
        pred_scanner, bot = surge_scanner
        await pred_scanner.scan_once()
        await pred_scanner.scan_surges()
        posted = await pred_scanner.scan_surges()
        assert posted == 0
        assert len(bot.surges) == 1

    async def test_no_surge_below_thresholds(self, tmp_path, monkeypatch) -> None:
        from src import config

        db_path = str(tmp_path / "nosurge.db")
        await run_migrations(db_path)
        monkeypatch.setattr(config.settings, "prediction_min_bet_usd", 10_000.0)
        monkeypatch.setattr(config.settings, "surge_min_wallets", 5)
        monkeypatch.setattr(config.settings, "surge_min_total_usd", 100_000.0)

        trades = [_surge_trade(f"0xw{i}", f"0xtx{i}") for i in range(3)]  # only 3 wallets
        client = FakePolymarketClient(
            trades, WalletProfile(wallet="0xw0", first_seen_ts=int(time.time()) - 3600)
        )
        scanner = PredictionScanner(client=client, bot=FakeBot(), db_path=db_path)
        await scanner.scan_once()
        assert await scanner.scan_surges() == 0
