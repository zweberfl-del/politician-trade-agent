"""Pure parsing helpers for disclosure documents.

These functions take raw HTML / extracted PDF text and return plain dicts so
they can be unit-tested with fixtures, independent of any network I/O.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Generic HTML table extraction (stdlib only, matching the rest of the repo)
# ---------------------------------------------------------------------------


class _TableParser(HTMLParser):
    """Collect every <table> as a list of rows of stripped cell texts."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._in_table = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._in_table += 1
            if self._in_table == 1:
                self.tables.append([])
        elif self._in_table and tag == "tr":
            self._row = []
        elif self._in_table and tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._in_table:
            self._in_table -= 1
        elif self._in_table and tag == "tr" and self._row is not None:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None
        elif self._in_table and tag in ("td", "th") and self._cell is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            if self._row is not None:
                self._row.append(text)
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_html_tables(html: str) -> list[list[list[str]]]:
    """Return all tables in *html* as ``[table][row][cell]`` text."""
    parser = _TableParser()
    try:
        parser.feed(html)
    except Exception:
        logger.debug("parse_html_tables: malformed HTML", exc_info=True)
    return [t for t in parser.tables if t]


# ---------------------------------------------------------------------------
# Senate e-filed PTR pages (efdsearch.senate.gov/search/view/ptr/<uuid>/)
# ---------------------------------------------------------------------------

_SENATE_COLUMNS = {
    "transaction date": "transaction_date",
    "owner": "owner",
    "ticker": "ticker",
    "asset name": "asset_name",
    "asset type": "asset_type",
    "type": "transaction_type",
    "amount": "amount_range",
}


def parse_senate_ptr_html(html: str) -> list[dict]:
    """Parse the transactions table of a Senate e-filed PTR page.

    Returns one dict per transaction row with keys: ``transaction_date``,
    ``owner``, ``ticker``, ``asset_name``, ``asset_type``,
    ``transaction_type``, ``amount_range``. Values are raw strings; the
    caller normalizes them. Returns [] when no transactions table is found.
    """
    for table in parse_html_tables(html):
        header = [c.lower().strip() for c in table[0]]
        if "transaction date" not in header or "amount" not in header:
            continue

        col_map: dict[int, str] = {}
        for idx, name in enumerate(header):
            key = _SENATE_COLUMNS.get(name)
            if key:
                col_map[idx] = key

        rows: list[dict] = []
        for row in table[1:]:
            entry = {key: "" for key in _SENATE_COLUMNS.values()}
            for idx, key in col_map.items():
                if idx < len(row):
                    entry[key] = row[idx].strip()
            # "--" is EFD's placeholder for assets without a ticker
            if entry["ticker"] in ("--", "-"):
                entry["ticker"] = ""
            if any(entry.values()):
                rows.append(entry)
        return rows
    return []


# ---------------------------------------------------------------------------
# House e-filed PTR PDFs (text extracted with pdfplumber)
# ---------------------------------------------------------------------------

# One disclosed transaction inside the flattened PDF text, e.g.:
#   "SP Apple Inc. - Common Stock (AAPL) [ST] P 01/02/2024 01/03/2024 $1,001 - $15,000"
_HOUSE_TXN_RE = re.compile(
    r"(?:\b(?P<owner>SP|DC|JT)\b\s+)?"
    r"(?P<asset>[^()$]{0,120}?)"
    r"\((?P<ticker>[A-Z][A-Z0-9.\-/]{0,9})\)\s*"
    r"(?:\[[A-Z]{2,4}\]\s*)?"
    r"(?P<type>P|S\s*\(partial\)|S\s*\(full\)|S|E)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<ndate>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+(?:\s*-\s*\$[\d,]+)?)"
)

_OWNER_LABELS = {"SP": "Spouse", "DC": "Dependent Child", "JT": "Joint"}


def parse_house_ptr_text(text: str) -> list[dict]:
    """Parse transactions out of the text of an e-filed House PTR PDF.

    Best-effort: only rows with a ``(TICKER)`` are captured (those are the
    only ones a mirroring strategy can act on). Returns dicts with keys:
    ``owner``, ``asset_name``, ``ticker``, ``transaction_type``,
    ``transaction_date``, ``notification_date``, ``amount_range``.
    """
    flat = re.sub(r"\s+", " ", text)
    rows: list[dict] = []
    for m in _HOUSE_TXN_RE.finditer(flat):
        raw_type = re.sub(r"\s+", " ", m.group("type")).strip()
        asset = m.group("asset")
        owner = m.group("owner") or ""

        # The leftmost-match rule lets the asset capture swallow both the
        # owner code and trailing text from the previous row, so recover the
        # owner from inside the capture and drop everything before it.
        if not owner:
            owner_matches = list(re.finditer(r"\b(SP|DC|JT)\b", asset))
            if owner_matches:
                owner = owner_matches[-1].group(1)
                asset = asset[owner_matches[-1].end():]

        # Strip residual header / filing-status fragments preceding the name.
        asset = re.sub(r"^.*?\bF S: New\b", "", asset)
        asset = re.sub(r"^.*?\bAmount\b", "", asset)
        asset = asset.strip(" -–:") or ""
        if len(asset) > 80:
            asset = asset[-80:].lstrip()

        rows.append(
            {
                "owner": _OWNER_LABELS.get(owner, owner),
                "asset_name": asset,
                "ticker": m.group("ticker"),
                "transaction_type": raw_type,
                "transaction_date": m.group("date"),
                "notification_date": m.group("ndate"),
                "amount_range": re.sub(r"\s*", "", m.group("amount")).replace("-", " - "),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Amount ranges
# ---------------------------------------------------------------------------

_AMOUNT_RE = re.compile(r"\$([\d,]+)")


def amount_range_bounds(amount_range: str) -> tuple[float, float] | None:
    """Return (low, high) dollars for a range like ``"$1,001 - $15,000"``.

    Open-ended ranges (``"$1,000,001 +"``) return the same value for both
    bounds. Returns None when no dollar figure is present.
    """
    figures = [float(m.replace(",", "")) for m in _AMOUNT_RE.findall(amount_range)]
    if not figures:
        return None
    return figures[0], figures[-1]
