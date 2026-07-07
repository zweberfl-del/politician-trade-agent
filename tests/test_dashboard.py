"""Web dashboard endpoints against a real database, external calls faked."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.data.models import Chamber, PoliticianTrade, TransactionType
from src.data.options import OptionContract, OptionsChain, OptionsProvider
from src.storage.database import insert_trades, record_flow_event
from src.storage.migrations import run_migrations
from src.web.dashboard import DashboardServer


@pytest.fixture
async def client(tmp_path):
    db_path = str(tmp_path / "dash.db")
    await run_migrations(db_path)

    await insert_trades(
        db_path,
        [
            PoliticianTrade(
                politician_name="Jane Example",
                chamber=Chamber.HOUSE,
                ticker="AAPL",
                transaction_type=TransactionType.PURCHASE,
                transaction_date=date.today() - timedelta(days=2),
                filing_date=date.today() - timedelta(days=1),
                amount_range="$15,001 - $50,000",
                source="house_clerk",
                source_id="doc1",
            )
        ],
    )
    await record_flow_event(
        db_path,
        {
            "contract_key": "TST-C-105",
            "alert_date": date.today().isoformat(),
            "ticker": "TST",
            "option_type": "call",
            "strike": 105.0,
            "expiration": (date.today() + timedelta(days=14)).isoformat(),
            "premium_usd": 1_000_000.0,
            "volume": 5000,
            "open_interest": 500,
            "vol_oi_ratio": 10.0,
            "sentiment": "bullish",
            "spot": 100.0,
        },
    )

    server = DashboardServer(db_path)
    async with TestClient(TestServer(server.build_app())) as test_client:
        test_client.db_path = db_path
        yield test_client


class TestDashboard:
    async def test_index_serves_html(self, client) -> None:
        resp = await client.get("/")
        assert resp.status == 200
        body = await resp.text()
        assert "Politician Trade Agent" in body
        assert "viewport" in body  # responsive/mobile meta

    async def test_api_trades(self, client) -> None:
        rows = await (await client.get("/api/trades")).json()
        assert rows[0]["politician_name"] == "Jane Example"
        assert rows[0]["ticker"] == "AAPL"

    async def test_api_trades_filter(self, client) -> None:
        rows = await (await client.get("/api/trades?ticker=NVDA")).json()
        assert rows == []

    async def test_api_flow(self, client) -> None:
        rows = await (await client.get("/api/flow")).json()
        assert rows[0]["ticker"] == "TST"
        assert rows[0]["sentiment"] == "bullish"

    async def test_api_top_and_active(self, client) -> None:
        top = await (await client.get("/api/top")).json()
        assert top[0]["ticker"] == "AAPL"
        active = await (await client.get("/api/active")).json()
        assert active[0]["politician_name"] == "Jane Example"

    async def test_api_gex_uses_configured_provider(self, client, monkeypatch) -> None:
        class FakeProvider(OptionsProvider):
            async def fetch_chain(self, ticker: str) -> OptionsChain:
                contract = OptionContract(
                    contract_symbol="TST-C-100",
                    ticker=ticker,
                    option_type="call",
                    strike=100.0,
                    expiration=date.today() + timedelta(days=30),
                    volume=100,
                    open_interest=1000,
                    implied_volatility=0.25,
                )
                return OptionsChain(ticker=ticker, spot=100.0, contracts=[contract])

        import src.data.options as options_module

        monkeypatch.setattr(options_module, "build_options_provider", FakeProvider)
        payload = await (await client.get("/api/gex/TST")).json()
        assert payload["ticker"] == "TST"
        assert payload["net_gex"] > 0

    async def test_api_gex_404_when_no_chain(self, client, monkeypatch) -> None:
        class EmptyProvider(OptionsProvider):
            async def fetch_chain(self, ticker: str) -> None:
                return None

        import src.data.options as options_module

        monkeypatch.setattr(options_module, "build_options_provider", EmptyProvider)
        resp = await client.get("/api/gex/NOPE")
        assert resp.status == 404


class TestAuth:
    """Token gate: off by default, enforced everywhere except PWA plumbing."""

    async def test_no_token_configured_means_open(self, client) -> None:
        assert (await client.get("/api/trades")).status == 200

    async def test_requests_rejected_without_token(self, client, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "dashboard_auth_token", "s3cret")
        assert (await client.get("/")).status == 401
        assert (await client.get("/api/trades")).status == 401

    async def test_bearer_header_accepted(self, client, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "dashboard_auth_token", "s3cret")
        resp = await client.get(
            "/api/trades", headers={"Authorization": "Bearer s3cret"}
        )
        assert resp.status == 200

    async def test_query_token_sets_cookie_session(self, client, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "dashboard_auth_token", "s3cret")
        resp = await client.get("/?token=s3cret")
        assert resp.status == 200
        assert "dash_token" in resp.cookies
        # Follow-up request rides the cookie, no token needed
        assert (await client.get("/api/trades")).status == 200

    async def test_wrong_token_rejected(self, client, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "dashboard_auth_token", "s3cret")
        assert (await client.get("/?token=wrong")).status == 401

    async def test_pwa_assets_stay_public(self, client, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config.settings, "dashboard_auth_token", "s3cret")
        for path in ("/manifest.webmanifest", "/sw.js", "/icon.svg", "/icon-192.png"):
            assert (await client.get(path)).status == 200, path


class TestHealth:
    async def test_health_reports_counts_and_heartbeats(self, client) -> None:
        payload = await (await client.get("/api/health")).json()
        assert payload["status"] == "ok"
        assert payload["trades_stored"] == 1
        assert payload["last_poll_age_s"] is None  # poller never ran

    async def test_health_age_computed(self, client) -> None:
        import time

        from src.storage.database import kv_set

        await kv_set(client.db_path, "last_poll_at", str(time.time() - 30))
        payload = await (await client.get("/api/health")).json()
        assert payload["last_poll_age_s"] is not None
        assert 25 <= payload["last_poll_age_s"] <= 60


class TestPwa:
    """The dashboard installs as a phone app: manifest + SW + icons."""

    async def test_index_wires_up_pwa(self, client) -> None:
        body = await (await client.get("/")).text()
        assert 'rel="manifest"' in body
        assert "serviceWorker" in body
        assert 'name="theme-color"' in body

    async def test_manifest(self, client) -> None:
        resp = await client.get("/manifest.webmanifest")
        assert resp.status == 200
        assert resp.content_type == "application/manifest+json"
        manifest = await resp.json(content_type=None)
        assert manifest["display"] == "standalone"
        assert manifest["start_url"] == "/"
        assert len(manifest["icons"]) >= 2

    async def test_service_worker(self, client) -> None:
        resp = await client.get("/sw.js")
        assert resp.status == 200
        assert resp.content_type == "application/javascript"
        assert "fetch" in await resp.text()

    async def test_svg_icon(self, client) -> None:
        resp = await client.get("/icon.svg")
        assert resp.status == 200
        assert resp.content_type == "image/svg+xml"

    async def test_png_icon_rendered(self, client) -> None:
        resp = await client.get("/icon-192.png")
        assert resp.status == 200
        assert resp.content_type == "image/png"
        body = await resp.read()
        assert body.startswith(b"\x89PNG")
