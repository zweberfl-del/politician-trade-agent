"""Insider-surge detection over prediction-market fills."""

from __future__ import annotations

from src.analysis.surge import SurgeTrade, detect_surges
from src.data.polymarket import WalletProfile

NOW = 1_800_000_000.0


def _fill(
    wallet: str,
    *,
    usd: float = 30_000.0,
    outcome: str = "Yes",
    side: str = "BUY",
    slug: str = "us-strike-on-iran",
    title: str = "Will the US launch an airstrike on Iran?",
    age: int = 60,
    category: str = "",
) -> SurgeTrade:
    return SurgeTrade(
        wallet=wallet,
        market_slug=slug,
        market_title=title,
        market_url=f"https://polymarket.com/event/{slug}",
        outcome=outcome,
        side=side,
        usd_size=usd,
        timestamp=int(NOW) - age,
        category=category,
    )


def _wallets(n: int, prefix: str = "0xw") -> list[str]:
    return [f"{prefix}{i}" for i in range(n)]


class TestDetectSurges:
    def test_crowded_one_sided_surge_fires(self) -> None:
        fills = [_fill(w) for w in _wallets(6)]  # 6 wallets x $30K = $180K
        surges = detect_surges(fills, {}, min_wallets=5, min_total_usd=100_000, now=NOW)
        assert len(surges) == 1
        s = surges[0]
        assert s.wallet_count == 6
        assert s.total_usd == 180_000
        assert s.buy_ratio == 1.0
        assert s.category == "military"

    def test_too_few_wallets_no_surge(self) -> None:
        fills = [_fill(w) for w in _wallets(3)]
        assert detect_surges(fills, {}, min_wallets=5, min_total_usd=1, now=NOW) == []

    def test_below_dollar_floor_no_surge(self) -> None:
        fills = [_fill(w, usd=1_000.0) for w in _wallets(6)]  # $6K total
        assert detect_surges(fills, {}, min_wallets=5, min_total_usd=100_000, now=NOW) == []

    def test_two_sided_book_no_surge(self) -> None:
        # 6 buyers, but heavy selling on the same outcome -> not one-sided
        fills = [_fill(w, usd=20_000) for w in _wallets(6)]
        fills += [_fill(w, usd=40_000, side="SELL") for w in _wallets(6, "0xs")]
        assert detect_surges(fills, {}, min_wallets=5, min_total_usd=1,
                             min_buy_ratio=0.7, now=NOW) == []

    def test_stale_fills_excluded(self) -> None:
        # Old fills fall outside the window and don't count toward the surge
        fills = [_fill(w, age=48 * 3600) for w in _wallets(6)]
        assert detect_surges(fills, {}, window_seconds=3600, min_wallets=5,
                             min_total_usd=1, now=NOW) == []

    def test_distinct_wallets_not_fill_count(self) -> None:
        # One wallet spamming many fills is not a crowd
        fills = [_fill("0xsolo", usd=50_000) for _ in range(10)]
        assert detect_surges(fills, {}, min_wallets=5, min_total_usd=1, now=NOW) == []

    def test_separate_outcomes_are_separate_surges(self) -> None:
        fills = [_fill(w, outcome="Yes") for w in _wallets(6)]
        fills += [_fill(w, outcome="No") for w in _wallets(6, "0xn")]
        surges = detect_surges(fills, {}, min_wallets=5, min_total_usd=100_000, now=NOW)
        assert len(surges) == 2
        assert {s.outcome for s in surges} == {"Yes", "No"}

    def test_fresh_and_sharp_counts_and_boost(self) -> None:
        wallets = _wallets(6)
        profiles = {
            wallets[0]: WalletProfile(wallet=wallets[0], first_seen_ts=int(NOW) - 3600,
                                      activity_count=1),  # fresh
            wallets[1]: WalletProfile(wallet=wallets[1], first_seen_ts=int(NOW) - 365 * 86400,
                                      activity_count=400, wins=20, losses=1),  # sharp
        }
        fills = [_fill(w) for w in wallets]
        surges = detect_surges(fills, profiles, min_wallets=5, min_total_usd=100_000, now=NOW)
        assert surges[0].fresh_count == 1
        assert surges[0].sharp_count == 1

    def test_high_stakes_outscores_mundane(self) -> None:
        mil = [_fill(w, slug="us-strike-on-iran",
                     title="Will the US strike Iran?") for w in _wallets(6)]
        misc = [_fill(w, slug="best-picture-oscars",
                      title="Who wins Best Picture?") for w in _wallets(6, "0xo")]
        surges = detect_surges(mil + misc, {}, min_wallets=5, min_total_usd=100_000, now=NOW)
        assert surges[0].category == "military"  # military ranks first despite equal dollars

    def test_surge_key_stable_within_bucket(self) -> None:
        fills = [_fill(w) for w in _wallets(6)]
        a = detect_surges(fills, {}, min_wallets=5, min_total_usd=100_000, now=NOW)[0]
        # A later fill a few minutes on keeps the same bucket key
        fills2 = fills + [_fill("0xlate", age=1)]
        b = detect_surges(fills2, {}, min_wallets=5, min_total_usd=100_000, now=NOW)[0]
        assert a.surge_key == b.surge_key
