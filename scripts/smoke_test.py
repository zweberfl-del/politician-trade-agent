"""Phase 2 market-data smoke test — see LIVE_SETUP.md.

Run with: python scripts/smoke_test.py
Exercises the three keyless market-data sources (Yahoo options chain,
FINRA short volume, Polymarket trade feed) and prints OK/FAILED for each.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.darkpool import DarkPoolService  # noqa: E402
from src.data.options import YahooOptionsProvider  # noqa: E402
from src.data.polymarket import PolymarketClient  # noqa: E402


async def main() -> None:
    chain = await YahooOptionsProvider().fetch_chain("SPY")
    print(
        "Yahoo chain:",
        "OK," if chain and chain.contracts else "FAILED,",
        len(chain.contracts) if chain else 0,
        "contracts, spot",
        chain.spot if chain else "-",
    )
    short = await DarkPoolService().get_short_volume("AAPL", days=2)
    print(
        "FINRA short volume:",
        "OK," if short else "FAILED,",
        [f"{d.day}:{d.short_ratio:.0%}" for d in short],
    )
    trades = await PolymarketClient().get_recent_trades(limit=25)
    print("Polymarket feed:", "OK," if trades else "FAILED,", len(trades), "trades")


if __name__ == "__main__":
    asyncio.run(main())
