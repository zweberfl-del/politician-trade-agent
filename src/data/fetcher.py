from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import xml.etree.ElementTree as ET
import zipfile
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import httpx

from src.config import settings
from src.data.http_util import request_with_retry
from src.data.models import Chamber, PoliticianTrade, TransactionType
from src.data.parsers import parse_house_ptr_text, parse_senate_ptr_html

logger = logging.getLogger(__name__)

try:
    import pdfplumber

    _PDFPLUMBER_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on install extras
    _PDFPLUMBER_AVAILABLE = False

# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class DataSource(ABC):
    """Interface that every trade-data provider must implement."""

    #: matches PoliticianTrade.source for rows produced by this source
    name: str = ""

    @abstractmethod
    async def fetch_trades(self, known_ids: set[str] | None = None) -> list[PoliticianTrade]:
        """Return recently-disclosed trades.

        ``known_ids`` contains source_ids of filings already ingested;
        sources skip those entirely (no detail re-fetch, no duplicate rows).
        """
        ...


# ---------------------------------------------------------------------------
# House Clerk XML feed + PTR PDFs
# ---------------------------------------------------------------------------


class HouseClerkSource(DataSource):
    """
    Fetches House representative trade filings from the official House Clerk
    Financial Disclosure XML index, then extracts per-trade detail (ticker,
    type, dates, amount) from the e-filed PTR PDFs.

    Paper filings (scanned images) and PDFs that fail parsing fall back to a
    metadata-only row so the filing is still surfaced and deduplicated.
    """

    name = "house_clerk"

    BASE_URL = "https://disclosures-clerk.house.gov"
    ZIP_URL = BASE_URL + "/public_disc/financial-pdfs/{year}FD.zip"
    PDF_URL = BASE_URL + "/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

    def __init__(self, *, lookback_days: int = 90) -> None:
        self.lookback_days = lookback_days

        # In-memory cache: (year -> (fetched_at, raw_bytes)); a disk cache in
        # settings.cache_dir persists across restarts and enables ETag 304s.
        self._zip_cache: dict[int, tuple[float, bytes]] = {}
        self._cache_ttl = 3600  # 1 hour

    # -- public API ----------------------------------------------------------

    async def fetch_trades(self, known_ids: set[str] | None = None) -> list[PoliticianTrade]:
        known_ids = known_ids or set()
        cutoff = date.today() - timedelta(days=self.lookback_days)
        years = _years_in_range(cutoff, date.today())

        filings: list[dict] = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            for year in years:
                try:
                    raw = await self._download_zip(client, year)
                    filings.extend(self._parse_zip(raw, year, cutoff))
                except Exception:
                    logger.warning(
                        "HouseClerkSource: failed to process year %d", year, exc_info=True
                    )

            new_filings = [f for f in filings if f["doc_id"] not in known_ids]
            new_filings.sort(key=lambda f: f["filing_date"], reverse=True)

            if not settings.parse_filing_details:
                return [self._metadata_trade(f) for f in new_filings]

            budget = settings.max_filings_per_cycle
            trades: list[PoliticianTrade] = []
            for filing in new_filings[:budget]:
                trades.extend(await self._filing_to_trades(client, filing))
            skipped = len(new_filings) - min(budget, len(new_filings))
            if skipped:
                logger.info(
                    "HouseClerkSource: %d new filings deferred to later cycles", skipped
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
        zip_path, meta_path = self._disk_cache_paths(year)

        headers: dict[str, str] = {}
        if zip_path is not None and zip_path.exists() and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                if meta.get("etag"):
                    headers["If-None-Match"] = meta["etag"]
                if meta.get("last_modified"):
                    headers["If-Modified-Since"] = meta["last_modified"]
            except Exception:
                logger.debug("HouseClerkSource: unreadable cache meta", exc_info=True)

        logger.info("HouseClerkSource: checking %s", url)
        resp = await request_with_retry(client, "GET", url, headers=headers)

        if resp.status_code == 304 and zip_path is not None and zip_path.exists():
            logger.info("HouseClerkSource: %dFD.zip unchanged (304), using disk cache", year)
            data = zip_path.read_bytes()
        else:
            resp.raise_for_status()
            data = resp.content
            if zip_path is not None:
                try:
                    zip_path.write_bytes(data)
                    meta_path.write_text(
                        json.dumps(
                            {
                                "etag": resp.headers.get("ETag", ""),
                                "last_modified": resp.headers.get("Last-Modified", ""),
                            }
                        )
                    )
                except OSError:
                    logger.debug("HouseClerkSource: could not write disk cache", exc_info=True)

        self._zip_cache[year] = (time.monotonic(), data)
        return data

    def _disk_cache_paths(self, year: int) -> tuple[Path | None, Path | None]:
        if not settings.cache_dir:
            return None, None
        cache_dir = Path(settings.cache_dir)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None, None
        return cache_dir / f"{year}FD.zip", cache_dir / f"{year}FD.zip.meta"

    def _parse_zip(self, raw: bytes, year: int, cutoff: date) -> list[dict]:
        filings: list[dict] = []
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
                return filings

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

                filings.append(
                    {
                        "politician_name": f"{first} {last}".strip(),
                        "state": state_dst[:2] if state_dst else "",
                        "filing_date": filing_dt,
                        "doc_id": doc_id,
                        "year": year,
                        "pdf_url": self.PDF_URL.format(year=year, doc_id=doc_id),
                    }
                )
        return filings

    def _metadata_trade(self, filing: dict) -> PoliticianTrade:
        return PoliticianTrade(
            politician_name=filing["politician_name"],
            chamber=Chamber.HOUSE,
            state=filing["state"],
            source=self.name,
            source_id=filing["doc_id"],
            filing_url=filing["pdf_url"],
            filing_date=filing["filing_date"],
            transaction_type=TransactionType.UNKNOWN,
            ticker="",
        )

    async def _filing_to_trades(
        self, client: httpx.AsyncClient, filing: dict
    ) -> list[PoliticianTrade]:
        """Download and parse one PTR PDF; fall back to a metadata row."""
        doc_id: str = filing["doc_id"]

        # DocIDs starting with "2" are e-filed (text PDFs); "8"/"9" are
        # scanned paper filings that text extraction cannot handle.
        if not doc_id.startswith("2") or not _PDFPLUMBER_AVAILABLE:
            if not _PDFPLUMBER_AVAILABLE:
                logger.debug("HouseClerkSource: pdfplumber not installed; metadata only")
            return [self._metadata_trade(filing)]

        try:
            resp = await request_with_retry(client, "GET", filing["pdf_url"])
            resp.raise_for_status()
            text = await asyncio.to_thread(_extract_pdf_text, resp.content)
            rows = parse_house_ptr_text(text)
        except Exception:
            logger.warning(
                "HouseClerkSource: failed to parse PDF %s", doc_id, exc_info=True
            )
            rows = []

        if not rows:
            return [self._metadata_trade(filing)]

        trades: list[PoliticianTrade] = []
        for row in rows:
            trades.append(
                PoliticianTrade(
                    politician_name=filing["politician_name"],
                    chamber=Chamber.HOUSE,
                    state=filing["state"],
                    ticker=row["ticker"],
                    asset_name=row["asset_name"],
                    transaction_type=TransactionType.from_raw(row["transaction_type"]),
                    transaction_date=_parse_date_flexible(row["transaction_date"]),
                    filing_date=filing["filing_date"],
                    amount_range=row["amount_range"],
                    owner=row["owner"],
                    source=self.name,
                    source_id=doc_id,
                    filing_url=filing["pdf_url"],
                )
            )
        logger.info(
            "HouseClerkSource: parsed %d transaction(s) from PTR %s (%s)",
            len(trades),
            doc_id,
            filing["politician_name"],
        )
        return trades


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from every page of a PDF (runs in a worker thread)."""
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Senate Electronic Financial Disclosure
# ---------------------------------------------------------------------------


class SenateEFDSource(DataSource):
    """
    Fetches Senate trade filings from the Senate Electronic Financial
    Disclosure (EFD) search system, then parses per-trade detail out of
    e-filed PTR pages (which are plain HTML tables).

    EFD requires every session to accept the "prohibition on use" agreement
    (a POST to /search/home/ with the CSRF token) before any search or view
    URL responds with data.
    """

    name = "senate_efd"

    BASE_URL = "https://efdsearch.senate.gov"
    HOME_URL = BASE_URL + "/search/home/"
    SEARCH_PAGE_URL = BASE_URL + "/search/"
    SEARCH_URL = BASE_URL + "/search/report/data/"

    def __init__(self, *, lookback_days: int = 90) -> None:
        self.lookback_days = lookback_days

    async def fetch_trades(self, known_ids: set[str] | None = None) -> list[PoliticianTrade]:
        known_ids = known_ids or set()
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
                await self._accept_agreement(client)

                csrf_token = client.cookies.get("csrftoken") or ""
                headers: dict[str, str] = {"Referer": self.SEARCH_PAGE_URL}
                if csrf_token:
                    headers["X-CSRFToken"] = csrf_token

                form_data = {
                    "start": "0",
                    "length": "100",
                    "report_type_id": "11",
                    "filer_type_id": "1",
                    "submitted_start_date": start.strftime("%m/%d/%Y"),
                    "submitted_end_date": today.strftime("%m/%d/%Y"),
                }
                resp = await request_with_retry(
                    client, "POST", self.SEARCH_URL, data=form_data, headers=headers
                )
                resp.raise_for_status()

                filings = self._parse_response(resp.json())
                new_filings = [f for f in filings if f["source_id"] not in known_ids]

                if not settings.parse_filing_details:
                    return [self._metadata_trade(f) for f in new_filings]

                budget = settings.max_filings_per_cycle
                trades: list[PoliticianTrade] = []
                for filing in new_filings[:budget]:
                    trades.extend(await self._filing_to_trades(client, filing))
                skipped = len(new_filings) - min(budget, len(new_filings))
                if skipped:
                    logger.info(
                        "SenateEFDSource: %d new filings deferred to later cycles", skipped
                    )
                return trades
        except Exception:
            logger.warning(
                "SenateEFDSource: failed to fetch Senate disclosures",
                exc_info=True,
            )
            return []

    # -- session handshake ----------------------------------------------------

    async def _accept_agreement(self, client: httpx.AsyncClient) -> None:
        """Accept the EFD prohibition-on-use agreement to unlock the session."""
        home_resp = await request_with_retry(client, "GET", self.HOME_URL)
        home_resp.raise_for_status()

        csrf_token = _extract_csrf_token(home_resp.text) or client.cookies.get("csrftoken", "")
        resp = await request_with_retry(
            client,
            "POST",
            self.HOME_URL,
            data={"prohibition_agreement": "1", "csrfmiddlewaretoken": csrf_token},
            headers={"Referer": self.HOME_URL},
        )
        resp.raise_for_status()

    # -- parsing -------------------------------------------------------------

    def _parse_response(self, payload: dict) -> list[dict]:
        rows: list[list[str]] = payload.get("data", [])
        filings: list[dict] = []

        for row in rows:
            try:
                filing = self._parse_row(row)
                if filing is not None:
                    filings.append(filing)
            except Exception:
                logger.debug("SenateEFDSource: failed to parse row: %s", row, exc_info=True)
        return filings

    def _parse_row(self, row: list[str]) -> dict | None:
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

        filing_url = _extract_href(link_html) or _extract_href(row[4])
        if filing_url and not filing_url.startswith("http"):
            filing_url = f"{self.BASE_URL}{filing_url}"

        filing_dt = _parse_date_flexible(filing_date_str)

        # Derive a stable source_id from the URL path if available
        source_id = filing_url.rstrip("/").rsplit("/", 1)[-1] if filing_url else ""

        return {
            "politician_name": f"{first_name} {last_name}".strip(),
            "state": office[:2] if office else "",
            "filing_date": filing_dt,
            "filing_url": filing_url,
            "source_id": source_id,
        }

    def _metadata_trade(self, filing: dict) -> PoliticianTrade:
        return PoliticianTrade(
            politician_name=filing["politician_name"],
            chamber=Chamber.SENATE,
            state=filing["state"],
            source=self.name,
            source_id=filing["source_id"],
            filing_url=filing["filing_url"],
            filing_date=filing["filing_date"],
            transaction_type=TransactionType.UNKNOWN,
            ticker="",
        )

    async def _filing_to_trades(
        self, client: httpx.AsyncClient, filing: dict
    ) -> list[PoliticianTrade]:
        """Fetch and parse one e-filed PTR page; fall back to a metadata row."""
        url: str = filing["filing_url"]

        # Only electronic PTRs are HTML tables; paper filings are scans.
        if "/view/ptr/" not in url:
            return [self._metadata_trade(filing)]

        try:
            resp = await request_with_retry(client, "GET", url)
            resp.raise_for_status()
            rows = parse_senate_ptr_html(resp.text)
        except Exception:
            logger.warning(
                "SenateEFDSource: failed to parse PTR %s", filing["source_id"], exc_info=True
            )
            rows = []

        if not rows:
            return [self._metadata_trade(filing)]

        trades: list[PoliticianTrade] = []
        for row in rows:
            asset_name = row["asset_name"]
            if row["asset_type"] and row["asset_type"].lower() not in asset_name.lower():
                asset_name = f"{asset_name} ({row['asset_type']})" if asset_name else row["asset_type"]
            trades.append(
                PoliticianTrade(
                    politician_name=filing["politician_name"],
                    chamber=Chamber.SENATE,
                    state=filing["state"],
                    ticker=row["ticker"].upper(),
                    asset_name=asset_name,
                    transaction_type=TransactionType.from_raw(row["transaction_type"]),
                    transaction_date=_parse_date_flexible(row["transaction_date"]),
                    filing_date=filing["filing_date"],
                    amount_range=row["amount_range"],
                    owner=row["owner"],
                    source=self.name,
                    source_id=filing["source_id"],
                    filing_url=url,
                )
            )
        logger.info(
            "SenateEFDSource: parsed %d transaction(s) from PTR %s (%s)",
            len(trades),
            filing["source_id"],
            filing["politician_name"],
        )
        return trades


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

    Only active when ``settings.finnhub_api_key`` is set. Note this endpoint
    is on Finnhub's premium tier — free keys receive 403s, which are logged
    once per cycle with a clear message.
    """

    name = "finnhub"

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
        # Serialize dispatch so requests are spaced ``interval`` apart,
        # keeping the burst rate under the API's per-minute limit.
        self._interval = 60.0 / requests_per_minute
        self._pace_lock = asyncio.Lock()
        self._next_slot = 0.0
        self._access_denied = False

    async def fetch_trades(self, known_ids: set[str] | None = None) -> list[PoliticianTrade]:
        if not self.api_key:
            logger.debug("FinnhubSource: no API key configured — skipping")
            return []

        self._access_denied = False
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

    async def _pace(self) -> None:
        """Reserve the next dispatch slot, spacing requests evenly."""
        async with self._pace_lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            await asyncio.sleep(wait)

    async def _fetch_ticker(
        self,
        client: httpx.AsyncClient,
        ticker: str,
        cutoff: date,
    ) -> list[PoliticianTrade]:
        if self._access_denied:
            return []
        await self._pace()

        resp = await client.get(
            self.API_URL,
            params={"symbol": ticker, "token": self.api_key},
        )
        if resp.status_code in (401, 403):
            if not self._access_denied:
                self._access_denied = True
                logger.error(
                    "FinnhubSource: API key rejected (%d). The congressional-trading "
                    "endpoint requires a paid Finnhub plan — the free tier cannot "
                    "access it. Remove FINNHUB_API_KEY or upgrade the plan.",
                    resp.status_code,
                )
            return []
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
                    source=self.name,
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

    async def fetch_all_trades(
        self, known_ids: dict[str, set[str]] | None = None
    ) -> list[PoliticianTrade]:
        """Fetch from every source.

        ``known_ids`` maps source name -> set of already-ingested source_ids
        so sources can skip filings that are in the database.
        """
        known_ids = known_ids or {}
        coros = [
            source.fetch_trades(known_ids.get(source.name, set()))
            for source in self.sources
        ]
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
