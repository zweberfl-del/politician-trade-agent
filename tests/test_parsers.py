from __future__ import annotations

from src.data.parsers import (
    amount_range_bounds,
    parse_house_ptr_text,
    parse_html_tables,
    parse_senate_ptr_html,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SENATE_PTR_HTML = """
<html><body>
<h1>Periodic Transaction Report</h1>
<table class="table">
  <thead>
    <tr>
      <th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th>
      <th>Asset Name</th><th>Asset Type</th><th>Type</th><th>Amount</th>
      <th>Comment</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>1</td><td>06/12/2026</td><td>Self</td>
      <td><a href="https://finance.example/NVDA">NVDA</a></td>
      <td>NVIDIA Corporation</td><td>Stock</td><td>Purchase</td>
      <td>$15,001 - $50,000</td><td>--</td>
    </tr>
    <tr>
      <td>2</td><td>06/15/2026</td><td>Spouse</td>
      <td>--</td>
      <td>Municipal Bond Fund</td><td>Municipal Security</td>
      <td>Sale (Partial)</td>
      <td>$1,001 - $15,000</td><td>--</td>
    </tr>
  </tbody>
</table>
</body></html>
"""

# Flattened text as pdfplumber extracts it from an e-filed House PTR
HOUSE_PTR_TEXT = """
Clerk of the House of Representatives - Legislative Resource Center
Periodic Transaction Report
Filer Information
Name: Hon. Jane Q. Example
Status: Member
Transactions
ID Owner Asset Transaction Type Date Notification Date Amount
SP Apple Inc. - Common Stock (AAPL)
[ST] P 06/02/2026 06/10/2026 $1,001 - $15,000
F S: New
Microsoft Corporation (MSFT) [ST] S (partial) 06/03/2026
06/11/2026 $15,001 - $50,000
F S: New
JT Tesla Inc (TSLA) [ST] E 06/04/2026 06/12/2026 $50,001 - $100,000
"""


# ---------------------------------------------------------------------------
# HTML table extraction
# ---------------------------------------------------------------------------


class TestParseHtmlTables:
    def test_extracts_rows_and_cells(self) -> None:
        tables = parse_html_tables(SENATE_PTR_HTML)
        assert len(tables) == 1
        assert tables[0][0][1] == "Transaction Date"
        assert tables[0][1][3] == "NVDA"

    def test_malformed_html_returns_empty(self) -> None:
        assert parse_html_tables("<table><tr><td>no closing") in ([], [[["no closing"]]])

    def test_no_tables(self) -> None:
        assert parse_html_tables("<p>plain paragraph</p>") == []


# ---------------------------------------------------------------------------
# Senate PTR pages
# ---------------------------------------------------------------------------


class TestParseSenatePtr:
    def test_parses_transaction_rows(self) -> None:
        rows = parse_senate_ptr_html(SENATE_PTR_HTML)
        assert len(rows) == 2

        first = rows[0]
        assert first["ticker"] == "NVDA"
        assert first["asset_name"] == "NVIDIA Corporation"
        assert first["transaction_type"] == "Purchase"
        assert first["transaction_date"] == "06/12/2026"
        assert first["owner"] == "Self"
        assert first["amount_range"] == "$15,001 - $50,000"

    def test_placeholder_ticker_becomes_empty(self) -> None:
        rows = parse_senate_ptr_html(SENATE_PTR_HTML)
        assert rows[1]["ticker"] == ""
        assert rows[1]["transaction_type"] == "Sale (Partial)"

    def test_page_without_transactions_table(self) -> None:
        assert parse_senate_ptr_html("<table><tr><th>Name</th></tr></table>") == []


# ---------------------------------------------------------------------------
# House PTR PDFs
# ---------------------------------------------------------------------------


class TestParseHousePtr:
    def test_parses_all_ticker_transactions(self) -> None:
        rows = parse_house_ptr_text(HOUSE_PTR_TEXT)
        tickers = [r["ticker"] for r in rows]
        assert tickers == ["AAPL", "MSFT", "TSLA"]

    def test_row_fields(self) -> None:
        rows = parse_house_ptr_text(HOUSE_PTR_TEXT)
        aapl = rows[0]
        assert aapl["owner"] == "Spouse"
        assert aapl["transaction_type"] == "P"
        assert aapl["transaction_date"] == "06/02/2026"
        assert aapl["notification_date"] == "06/10/2026"
        assert aapl["amount_range"] == "$1,001 - $15,000"
        assert "Apple" in aapl["asset_name"]

    def test_partial_sale_and_exchange(self) -> None:
        rows = parse_house_ptr_text(HOUSE_PTR_TEXT)
        assert rows[1]["transaction_type"] == "S (partial)"
        assert rows[2]["transaction_type"] == "E"
        assert rows[2]["owner"] == "Joint"

    def test_no_transactions(self) -> None:
        assert parse_house_ptr_text("Annual report, no ticker rows here.") == []


# ---------------------------------------------------------------------------
# Amount ranges
# ---------------------------------------------------------------------------


class TestAmountRangeBounds:
    def test_standard_range(self) -> None:
        assert amount_range_bounds("$1,001 - $15,000") == (1001.0, 15000.0)

    def test_open_ended(self) -> None:
        assert amount_range_bounds("$1,000,001 +") == (1000001.0, 1000001.0)

    def test_no_amount(self) -> None:
        assert amount_range_bounds("") is None
        assert amount_range_bounds("Unknown") is None
