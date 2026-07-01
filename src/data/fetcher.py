from __future__ import annotations

import asyncio
import io
import logging
import time
import xml.etree.ElementTree as ET
import zipfile
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

import httpx

from src.config import settings
from src.data.models import Chamber, Filing, PoliticianTrade, TransactionType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class DataSource(ABC):
    """Interface that every trade-data provider must implement."""

    @abstractmethod
    async def fetch_trades(self) -> list[PoliticianTrade]:
        """Return all recently-disclosed trades from this source."""
        ...


# ---------------------------------------------------------------------------
# House Clerk XML feed
# ---------------------------------------------------------------------------


class HouseClerkSource(DataSource):
    """
    Fetches House representative trade filings from the official House Clerk
    Financial Disclosure XML index.

    The XML only provides filing metadata (filer name, date, doc-ID).
    Individual trade details (ticker, amount, buy/sell) are inside the PDF
    and are *not* parsed in this version.
    """

    BASE_URL = "https://disclosures-clerk.house.gov"
    ZIP_URL = BASE_URL + "/public_disc/financial-pdfs/{year}FD.zip"
    PDF_URL = BASE_URL + "/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

    def __init__(self, *, lookback_days: int = 90) -> None:
        self.lookback_days = lookback_days

        # Simple in-memory cache: (year -> (fetched_at, raw_bytes))
        self._zip_cache: dict[int, tuple[float, bytes]] = {}
        self._cache_ttl = 3600  # 1 hour

    # -- public API ----------------------------------------------------------

    async def fetch_trades(self) -> list[PoliticianTrade]:
        cutoff = date.today() - timedelta(days=self.lookback_days)
        years = _years_in_range(cutoff, date.today())

        trades: list[PoliticianTrade] = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            for year in years:
                try:
                    raw = await self._download_zip(client, year)
                    trades.extend(self._parse_zip(raw, year, cutoff))
                except Exception:
                    logger.warning(
                        "HouseClerkSource: failed to process year %d", year, exc_info=True
                    )
        return trades

    # -- internals -----------------------------------------------------------

    async def _download_zip(self, client: httpx.AsyncClient, year: int) -> bytes:
        cached = self._zip_cache.get(year)
        if cached is not None:
            fetched_at, data = cached
            if time.monotonic() - fetched_at < self._cache_ttl:
                return data

        url = self.ZIP_URL.format(year=year)
        logger.info("HouseClerkSource: downloading %s", url)
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.content
        self._zip_cache[year] = (time.monotonic(), data)
        return data

    def _parse_zip(
        self, raw: bytes, year: int, cutoff: date
    ) -> list[PoliticianTrade]:
        trades: list[PoliticianTrade] = []
        buf = io.BytesIO(raw)
        with zipfile.ZipFile(buf) as zf:
            xml_name = f"{year}FD.xml"
            # Find the XML file — name may vary in casing
            matched = [n for n in zf.namelist() if n.upper() == xml_name.upper()]
            if not matched:
                logger.warning(
                    "HouseClerkSource: no XML named %s in ZIP (contents: %s)",
                    xml_name,
                    zf.namelist(),
                )
                return trades

            with zf.open(matched[0]) as xf:
                tree = ET.parse(xf)
                root = tree.getroot()

            for member in root.iter("Member"):
                filing_type = (member.findtext("FilingType") or "").strip()
                if "P" not in filing_type.upper():
                    continue  # not a Periodic Transaction Report

                first = (member.findtext("First") or "").strip()
                last = (member.findtext("Last") or "").strip()
                state_dst = (member.findtext("StateDst") or "").strip()
                filing_date_str = (member.findtext("FilingDate") or "").strip()
                doc_id = (member.findtext("DocID") or "").strip()

                if not doc_id or not last:
                    continue

                filing_dt = _parse_date_flexible(filing_date_str)
                if filing_dt is None or filing_dt < cutoff:
                    continue

                pdf_url = self.PDF_URL.format(year=year, doc_id=doc_id)

                trades.append(
                    PoliticianTrade(
                        politician_name=f"{first} {last}".strip(),
                        chamber=Chamber.HOUSE,
                        state=state_dst[:2] if state_dst else "",
                        source="house_clerk",
                        source_id=doc_id,
                        filing_url=pdf_url,
                        filing_date=filing_dt,
                        transaction_type=TransactionType.UNKNOWN,
                        ticker="",
                    )
                )
        return trades


# ---------------------------------------------------------------------------
# Senate Electronic Financial Disclosure
# ---------------------------------------------------------------------------


class SenateEFDSource(DataSource):
    """
    Fetches Senate trade filings from the Senate Electronic Financial
    Disclosure (EFD) search system.
    """

    SEARCH_URL = "https://efdsearch.senate.gov/search/report/data/"
    HOME_URL = "https://efdsearch.senate.gov/search/"

    def __init__(self, *, lookback_days: int = 90) -> None:
        self.lookback_days = lookback_days

    async def fetch_trades(self) -> list[PoliticianTrade]:
        today = date.today()
        start = today - timedelta(days=self.lookback_days)

        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            ) as client:
                # Step 1 — visit the search page to obtain cookies / CSRF token
                home_resp = await client.get(self.HOME_URL)
                home_resp.raise_for_status()

                csrf_token = _extract_csrf_token(home_resp.text)
                headers: dict[str, str] = {
                    "Referer": self.HOME_URL,
                }
                if csrf_token:
                    headers["X-CSRFToken"] = csrf_token

                # Step 2 — POST for PTR search results
                form_data = {
                    "start": "0",
                    "length": "100",
                    "report_type_id": "11",
                    "filer_type_id": "1",
                    "submitted_start_date": start.strftime("%m/%d/%Y"),
                    "submitted_end_date": today.strftime("%m/%d/%Y"),
                }
                resp = await client.post(
                    self.SEARCH_URL,
                    data=form_data,
                    headers=headers,
                )
                resp.raise_for_status()

                payload = resp.json()
                return self._parse_response(payload)
        except Exception:
            logger.warning(
                "SenateEFDSource: failed to fetch Senate disclosures",
                exc_info=True,
            )
            return []

    # -- parsing -------------------------------------------------------------

    def _parse_response(self, payload: dict) -> list[PoliticianTrade]:
        rows: list[list[str]] = payload.get("data", [])
        trades: list[PoliticianTrade] = []

        for row in rows:
            try:
                trade = self._parse_row(row)
                if trade is not None:
                    trades.append(trade)
            except Exception:
                logger.debug("SenateEFDSource: failed to parse row: %s", row, exc_info=True)
        return trades

    def _parse_row(self, row: list[str]) -> PoliticianTrade | None:
        # Expected columns:
        # [0] checkbox HTML, [1] first_name, [2] last_name, [3] office/state,
        # [4] report_type, [5] filing_date, [6] link HTML
        if len(row) < 7:
            return None

        first_name = _strip_html(row[1]).strip()
        last_name = _strip_html(row[2]).strip()
        office = _strip_html(row[3]).strip()
        filing_date_str = _strip_html(row[5]).strip()
        link_html = row[6]

        filing_url = _extract_href(link_html)
        if filing_url and not filing_url.startswith("http"):
            filing_url = f"https://efdsearch.senate.gov{filing_url}"

        filing_dt = _parse_date_flexible(filing_date_str)

        # Derive a stable source_id from the URL path if available
        source_id = filing_url.rsplit("/", 1)[-1] if filing_url else ""

        return PoliticianTrade(
            politician_name=f"{first_name} {last_name}".strip(),
            chamber=Chamber.SENATE,
            state=office[:2] if office else "",
            source="senate_efd",
            source_id=source_id,
            filing_url=filing_url,
            filing_date=filing_dt,
            transaction_type=TransactionType.UNKNOWN,
            ticker="",
        )


# ---------------------------------------------------------------------------
# Finnhub congressional-trading endpoint
# ---------------------------------------------------------------------------

# Top ~50 tickers commonly traded by members of Congress
_CONGRESS_TICKERS: list[str] = [
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA",
    "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK",
    "JNJ", "PFE", "UNH", "ABBV", "MRK", "LLY",
    "XOM", "CVX", "COP", "OXY",
    "DIS", "NFLX", "CMCSA",
    "BA", "LMT", "RTX", "GD", "NOC",
    "INTC", "AMD", "QCOM", "AVGO", "TXN",
    "V", "MA", "PYPL",
    "HD", "LOW", "WMT", "COST", "TGT",
    "CRM", "ORCL", "NOW", "SNOW",
]


class FinnhubSource(DataSource):
    """
    Uses Finnhub's congressional-trading endpoint to pull disclosed trades
    for a curated list of tickers.

    Only active when ``settings.finnhub_api_key`` is set.
    """

    API_URL = "https://finnhub.io/api/v1/stock/congressional-trading"

    def __init__(
        self,
        *,
        api_key: str = "",
        tickers: list[str] | None = None,
        requests_per_minute: int = 55,
        lookback_days: int = 90,
    ) -> None:
        self.api_key = api_key or settings.finnhub_api_key
        self.tickers = tickers if tickers is not None else list(_CONGRESS_TICKERS)
        self.lookback_days = lookback_days
        # Semaphore limits concurrency; we also sleep to stay within RPM
        self._semaphore = asyncio.Semaphore(requests_per_minute)
        self._interval = 60.0 / requests_per_minute  # seconds between requests

    async def fetch_trades(self) -> list[PoliticianTrade]:
        if not self.api_key:
            logger.debug("FinnhubSource: no API key configured — skipping")
            return []

        cutoff = date.today() - timedelta(days=self.lookback_days)
        trades: list[PoliticianTrade] = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            tasks = [
                self._fetch_ticker(client, ticker, cutoff) for ticker in self.tickers
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, BaseException):
                logger.warning("FinnhubSource: ticker fetch failed: %s", result)
                continue
            trades.extend(result)

        return trades

    async def _fetch_ticker(
        self,
        client: httpx.AsyncClient,
        ticker: str,
        cutoff: date,
    ) -> list[PoliticianTrade]:
        async with self._semaphore:
            await asyncio.sleep(self._interval)

            resp = await client.get(
                self.API_URL,
                params={"symbol": ticker, "token": self.api_key},
            )
            resp.raise_for_status()
            payload = resp.json()

        items: list[dict] = payload.get("data", [])
        symbol: str = payload.get("symbol", ticker)
        trades: list[PoliticianTrade] = []

        for item in items:
            filing_dt = _parse_date_flexible(item.get("filingDate", ""))
            txn_dt = _parse_date_flexible(item.get("transactionDate", ""))

            # Apply cutoff based on filing date (or transaction date as fallback)
            ref_dt = filing_dt or txn_dt
            if ref_dt is not None and ref_dt < cutoff:
                continue

            amount_from = item.get("amountFrom")
            amount_to = item.get("amountTo")
            amount_range = ""
            if amount_from is not None and amount_to is not None:
                amount_range = f"${amount_from:,.0f} - ${amount_to:,.0f}"

            raw_txn_type = item.get("transactionType", "")
            txn_type = TransactionType.from_raw(raw_txn_type)

            name = (item.get("name") or "").strip()

            # Build a deterministic source_id
            source_id = f"finnhub|{symbol}|{name}|{txn_dt or ''}|{raw_txn_type}"

            trades.append(
                PoliticianTrade(
                    politician_name=name,
                    chamber=Chamber.HOUSE,  # Finnhub doesn't distinguish; default
                    ticker=symbol,
                    transaction_type=txn_type,
                    transaction_date=txn_dt,
                    filing_date=filing_dt,
                    amount_range=amount_range,
                    owner=item.get("ownerType", ""),
                    source="finnhub",
                    source_id=source_id,
                )
            )
        return trades


# ---------------------------------------------------------------------------
# MultiFetcher — combines sources with deduplication
# ---------------------------------------------------------------------------


class MultiFetcher:
    """
    Runs multiple :class:`DataSource` instances concurrently and merges
    their results, removing duplicates by ``trade_id``.
    """

    def __init__(self, sources: list[DataSource]) -> None:
        self.sources = sources

    async def fetch_all_trades(self) -> list[PoliticianTrade]:
        coros = [source.fetch_trades() for source in self.sources]
        results = await asyncio.gather(*coros, return_exceptions=True)

        seen: set[str] = set()
        trades: list[PoliticianTrade] = []

        for result in results:
            if isinstance(result, BaseException):
                logger.warning("MultiFetcher: source failed: %s", result)
                continue
            for trade in result:
                tid = trade.trade_id
                if tid not in seen:
                    seen.add(tid)
                    trades.append(trade)

        logger.info(
            "MultiFetcher: fetched %d trades from %d source(s) (%d after dedup)",
            sum(len(r) for r in results if not isinstance(r, BaseException)),
            len(self.sources),
            len(trades),
        )
        return trades


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------


def build_default_fetcher() -> MultiFetcher:
    """
    Construct a :class:`MultiFetcher` with all available sources enabled,
    respecting the current :data:`settings`.
    """
    sources: list[DataSource] = [
        HouseClerkSource(),
        SenateEFDSource(),
    ]
    if settings.finnhub_api_key:
        sources.append(FinnhubSource())
    return MultiFetcher(sources)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _years_in_range(start: date, end: date) -> list[int]:
    """Return a sorted list of years that overlap [start, end]."""
    return list(range(start.year, end.year + 1))


def _parse_date_flexible(value: str | None) -> date | None:
    """Try several common date formats and return a :class:`date` or None."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    logger.debug("_parse_date_flexible: could not parse %r", value)
    return None


class _HrefExtractor(HTMLParser):
    """Tiny HTML parser that pulls the first ``href`` from an ``<a>`` tag."""

    def __init__(self) -> None:
        super().__init__()
        self.href: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and not self.href:
            for name, val in attrs:
                if name == "href" and val:
                    self.href = val
                    break


def _extract_href(html: str) -> str:
    """Return the first href found in an HTML snippet, or empty string."""
    parser = _HrefExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.href


class _TextExtractor(HTMLParser):
    """Strip all HTML tags and return inner text."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_html(html: str) -> str:
    """Return the plain text content of an HTML snippet."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    return "".join(parser.parts)


def _extract_csrf_token(html: str) -> str:
    """
    Pull a Django-style CSRF token from HTML.

    Looks for ``<input ... name="csrfmiddlewaretoken" value="...">``
    or a ``csrftoken`` cookie value embedded via JS.
    """

    class _CSRFParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.token: str = ""

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag != "input" or self.token:
                return
            attr_dict = dict(attrs)
            if attr_dict.get("name") == "csrfmiddlewaretoken":
                self.token = attr_dict.get("value", "") or ""

    parser = _CSRFParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.token
