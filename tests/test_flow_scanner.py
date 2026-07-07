"""FlowScanner pipeline: provider → detection → alert with per-day dedup."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.data.flow_scanner import FlowScanner
from src.data.options import OptionContract, OptionsChain, OptionsProvider
from src.storage.migrations import run_migrations


class FakeProvider(OptionsProvider):
    def __init__(self, chain: OptionsChain | None) -> None:
        self.chain = chain
        self.calls = 0

    async def fetch_chain(self, ticker: str) -> OptionsChain | None:
        self.calls += 1
        return self.chain


class FakeBot:
    def __init__(self) -> None:
        self.flow_alerts = []

    async def send_flow_alert(self, hit) -> None:
        self.flow_alerts.append(hit)


def _hot_chain() -> OptionsChain:
    contract = OptionContract(
        contract_symbol="TST260814C00105000",
        ticker="TST",
        option_type="call",
        strike=105.0,
        expiration=date.today() + timedelta(days=14),
        bid=1.95,
        ask=2.05,
        volume=5000,
        open_interest=500,
        implied_volatility=0.3,
    )
    return OptionsChain(ticker="TST", spot=100.0, contracts=[contract])


@pytest.fixture
async def scanner(tmp_path, monkeypatch):
    from src import config

    db_path = str(tmp_path / "flow.db")
    await run_migrations(db_path)
    monkeypatch.setattr(config.settings, "flow_watchlist", "TST")
    monkeypatch.setattr(config.settings, "flow_min_premium_usd", 100_000.0)

    provider = FakeProvider(_hot_chain())
    bot = FakeBot()
    return FlowScanner(provider=provider, bot=bot, db_path=db_path), provider, bot


class TestFlowScanner:
    async def test_posts_alert_for_unusual_contract(self, scanner) -> None:
        flow_scanner, _, bot = scanner
        posted = await flow_scanner.scan_once()
        assert posted == 1
        assert bot.flow_alerts[0].contract.ticker == "TST"

    async def test_same_contract_not_realerted_same_day(self, scanner) -> None:
        flow_scanner, _, bot = scanner
        await flow_scanner.scan_once()
        posted = await flow_scanner.scan_once()
        assert posted == 0
        assert len(bot.flow_alerts) == 1

    async def test_provider_failure_degrades_quietly(self, scanner) -> None:
        flow_scanner, provider, bot = scanner
        provider.chain = None
        posted = await flow_scanner.scan_once()
        assert posted == 0
        assert bot.flow_alerts == []
