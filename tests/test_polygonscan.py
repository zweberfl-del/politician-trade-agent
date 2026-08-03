"""On-chain first-funder parsing (the pure half of the Polygonscan client)."""

from __future__ import annotations

from src.data.polygonscan import first_funder_from_txlist


def _tx(frm: str, to: str, value: str = "1000000000000000000") -> dict:
    return {"from": frm, "to": to, "value": value}


def test_first_inbound_transfer_is_the_funder() -> None:
    rows = [
        _tx("0xFUNDER", "0xme"),          # earliest inbound -> funder
        _tx("0xme", "0xsomeone"),         # outbound, ignored
        _tx("0xother", "0xme"),           # later inbound, ignored
    ]
    assert first_funder_from_txlist(rows, "0xME") == "0xfunder"


def test_zero_value_transfers_skipped() -> None:
    rows = [
        _tx("0xspam", "0xme", value="0"),
        _tx("0xreal", "0xme"),
    ]
    assert first_funder_from_txlist(rows, "0xme") == "0xreal"


def test_self_transfer_ignored() -> None:
    rows = [_tx("0xme", "0xme"), _tx("0xreal", "0xme")]
    assert first_funder_from_txlist(rows, "0xme") == "0xreal"


def test_no_inbound_returns_none() -> None:
    rows = [_tx("0xme", "0xsomeone")]
    assert first_funder_from_txlist(rows, "0xme") is None
