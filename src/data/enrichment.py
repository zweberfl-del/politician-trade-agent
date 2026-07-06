"""Politician metadata enrichment.

Pulls the free `unitedstates/congress-legislators` dataset (party, state,
chamber, committee memberships) and joins it onto trades by normalized name.
The dataset is cached in the ``politicians`` table and refreshed weekly.
"""

from __future__ import annotations

import json
import logging
import re
import time

import httpx

from src.data.http_util import request_with_retry
from src.data.models import PoliticianTrade
from src.storage import database as db

logger = logging.getLogger(__name__)

LEGISLATORS_URL = (
    "https://unitedstates.github.io/congress-legislators/legislators-current.json"
)
COMMITTEES_URL = (
    "https://unitedstates.github.io/congress-legislators/committees-current.json"
)
MEMBERSHIP_URL = (
    "https://unitedstates.github.io/congress-legislators/committee-membership-current.json"
)

_REFRESH_SECONDS = 7 * 24 * 3600
_KV_KEY = "legislators_fetched_at"

_HONORIFICS = {"hon", "mr", "mrs", "ms", "dr", "sen", "rep", "senator", "representative"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "md", "phd"}


def normalize_name(name: str) -> str:
    """Reduce a politician name to a stable ``first last`` join key."""
    cleaned = re.sub(r"[^a-z\s]", " ", name.lower())
    tokens = [t for t in cleaned.split() if t not in _HONORIFICS and t not in _SUFFIXES]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    return f"{tokens[0]} {tokens[-1]}"


class EnrichmentService:
    """Loads the legislators dataset into SQLite and enriches trades."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # name_key -> {party, state, chamber, committees}
        self._index: dict[str, dict] | None = None

    async def ensure_fresh(self) -> None:
        """Refresh the dataset if it is older than a week; load the index."""
        fetched_at = await db.kv_get(self.db_path, _KV_KEY)
        stale = not fetched_at or (time.time() - float(fetched_at)) > _REFRESH_SECONDS
        if stale:
            try:
                await self._refresh()
                await db.kv_set(self.db_path, _KV_KEY, str(time.time()))
            except Exception:
                logger.warning("EnrichmentService: refresh failed", exc_info=True)
        if self._index is None or stale:
            await self._load_index()

    def enrich(self, trade: PoliticianTrade) -> PoliticianTrade:
        """Fill party (and state, when missing) from the dataset, in place."""
        if self._index is None:
            return trade
        info = self._index.get(normalize_name(trade.politician_name))
        if info is None:
            return trade
        if not trade.party:
            trade.party = info["party"]
        if not trade.state:
            trade.state = info["state"]
        return trade

    def lookup(self, name: str) -> dict | None:
        if self._index is None:
            return None
        return self._index.get(normalize_name(name))

    # -- internals -----------------------------------------------------------

    async def _load_index(self) -> None:
        rows = await db.get_politicians(self.db_path)
        index: dict[str, dict] = {}
        for row in rows:
            info = {
                "party": row["party"],
                "state": row["state"],
                "chamber": row["chamber"],
                "full_name": row["full_name"],
                "committees": json.loads(row["committees"] or "[]"),
            }
            for key in json.loads(row["name_keys"] or "[]"):
                index[key] = info
        self._index = index
        logger.info("EnrichmentService: loaded %d politicians", len(rows))

    async def _refresh(self) -> None:
        logger.info("EnrichmentService: downloading congress-legislators dataset")
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            legislators = (await request_with_retry(client, "GET", LEGISLATORS_URL)).json()

            committee_names: dict[str, str] = {}
            memberships: dict[str, list[str]] = {}
            try:
                committees = (await request_with_retry(client, "GET", COMMITTEES_URL)).json()
                for committee in committees:
                    committee_names[committee.get("thomas_id", "")] = committee.get("name", "")
                membership_raw = (
                    await request_with_retry(client, "GET", MEMBERSHIP_URL)
                ).json()
                for thomas_id, members in membership_raw.items():
                    # Sub-committee ids extend the parent id (e.g. "SSAF13")
                    name = committee_names.get(thomas_id)
                    if not name:
                        continue
                    for member in members:
                        bioguide = member.get("bioguide", "")
                        if bioguide:
                            memberships.setdefault(bioguide, []).append(name)
            except Exception:
                logger.warning(
                    "EnrichmentService: committee data unavailable; continuing without",
                    exc_info=True,
                )

        politicians: list[dict] = []
        for leg in legislators:
            ids = leg.get("id", {})
            name = leg.get("name", {})
            terms = leg.get("terms", [])
            if not terms:
                continue
            current = terms[-1]

            bioguide = ids.get("bioguide", "")
            first = name.get("first", "")
            last = name.get("last", "")
            official = name.get("official_full", f"{first} {last}")

            keys = {
                normalize_name(f"{first} {last}"),
                normalize_name(official),
            }
            nickname = name.get("nickname", "")
            if nickname:
                keys.add(normalize_name(f"{nickname} {last}"))
            keys.discard("")

            politicians.append(
                {
                    "bioguide": bioguide,
                    "full_name": official,
                    "name_keys": json.dumps(sorted(keys)),
                    "party": (current.get("party", "") or "")[:1].upper(),
                    "state": current.get("state", ""),
                    "chamber": "senate" if current.get("type") == "sen" else "house",
                    "committees": json.dumps(memberships.get(bioguide, [])[:8]),
                }
            )

        await db.upsert_politicians(self.db_path, politicians)
        logger.info("EnrichmentService: stored %d politicians", len(politicians))
