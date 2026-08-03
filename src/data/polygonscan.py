"""Optional on-chain enrichment via the Polygonscan API.

Polymarket runs on Polygon, and every proxy wallet had to be funded from
somewhere. Tracing that first inbound transfer lets us link wallets that were
seeded by the same address — the on-chain half of the "nine connected
accounts" finding. This is strictly optional: it only runs when
``POLYGONSCAN_API_KEY`` is set, and clustering falls back to behavioral
co-occurrence when it isn't.

The client is deliberately thin (one endpoint, defensive parsing) so the
clustering logic in ``analysis.clusters`` stays pure and testable.
"""

from __future__ import annotations

import logging

import httpx

from src.data.http_util import request_with_retry

logger = logging.getLogger(__name__)

BASE_URL = "https://api.polygonscan.com/api"


def first_funder_from_txlist(rows: list[dict], wallet: str) -> str | None:
    """The sender of the earliest inbound native transfer to *wallet*.

    Pure so it can be unit-tested without the network. Rows are Polygonscan
    ``txlist`` entries (already sorted ascending); we take the first where the
    wallet is the recipient of value and return the ``from`` address.
    """
    wallet = wallet.lower()
    for row in rows:
        to = (row.get("to") or "").lower()
        frm = (row.get("from") or "").lower()
        try:
            value = int(row.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        if to == wallet and frm and frm != wallet and value > 0:
            return frm
    return None


class PolygonscanClient:
    """Read-only Polygonscan client for first-funder lookups."""

    def __init__(self, api_key: str, base_url: str = BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def first_funder(self, wallet: str) -> str | None:
        """Return the address that first funded *wallet*, or None."""
        params = {
            "module": "account",
            "action": "txlist",
            "address": wallet,
            "startblock": 0,
            "endblock": 99_999_999,
            "page": 1,
            "offset": 20,
            "sort": "asc",
            "apikey": self.api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await request_with_retry(client, "GET", self.base_url, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception:
            logger.warning("PolygonscanClient: txlist for %s failed", wallet, exc_info=True)
            return None
        rows = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        return first_funder_from_txlist(rows, wallet)

    async def funding_map(self, wallets: list[str]) -> dict[str, str]:
        """Best-effort ``{wallet: funder}`` for a set of wallets.

        Wallets whose funder can't be resolved are omitted. Callers pace their
        own cycles; this issues the lookups sequentially to stay under
        Polygonscan's free-tier rate limit.
        """
        import asyncio

        funding: dict[str, str] = {}
        for w in wallets:
            funder = await self.first_funder(w)
            if funder:
                funding[w] = funder
            await asyncio.sleep(0.25)
        return funding
