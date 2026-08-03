"""Topic classification for prediction markets.

The 60 Minutes / Bubblemaps reporting turned on *which* markets the informed
money was in — "almost exclusively U.S. military actions." Surge detection
weights those categories heavily, so we need a cheap, offline way to bucket a
market from its title/slug alone (no extra API call, no key).

Ordered keyword tables: the first bucket that matches wins, so the more
specific/high-stakes categories are checked first. Pure and unit-testable.
"""

from __future__ import annotations

MILITARY = "military"
GEOPOLITICAL = "geopolitical"
ELECTION = "election"
ECONOMY = "economy"
CRYPTO = "crypto"
OTHER = "other"

# Categories whose surges read as potential insider activity and get a score
# boost in the surge engine.
HIGH_STAKES = {MILITARY, GEOPOLITICAL}

# Checked in order; first hit wins. Keep military ahead of geopolitical so a
# "US strike on Iran" market lands in the sharper bucket.
_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (
        MILITARY,
        (
            "airstrike", "air strike", "strike on", "strikes on", "missile",
            "troops", "troop", "invade", "invasion", "military operation",
            "military action", "military strike", "nuclear", "warhead",
            "ceasefire", "cease-fire", "hostage", "bombing", "bomb ", "drone strike",
            "ground offensive", "war with", "go to war", "declare war",
            "special operation", "special forces", "shoot down", "shot down",
        ),
    ),
    (
        GEOPOLITICAL,
        (
            "sanction", "annex", "coup", "regime", "peace deal", "peace talks",
            "summit", "treaty", "nato", "united nations", " un ", "diplomat",
            "president of", "prime minister", "supreme leader", "border",
            "territory", "occupation", "war", "conflict", "assassinat",
        ),
    ),
    (
        ELECTION,
        (
            "election", "elected", "primary", "nominee", "nomination", "vote",
            "ballot", "poll", "senate race", "house race", "governor",
            "presidential", "reelect", "re-elect", "electoral",
        ),
    ),
    (
        ECONOMY,
        (
            "fed ", "federal reserve", "interest rate", "rate cut", "rate hike",
            "inflation", "cpi", "recession", "gdp", "unemployment", "jobs report",
            "tariff", "shutdown", "debt ceiling",
        ),
    ),
    (
        CRYPTO,
        (
            "bitcoin", "btc", "ethereum", "ether", "crypto", "solana", "token",
            "stablecoin", "etf approval",
        ),
    ),
]


def classify_market(title: str, slug: str = "") -> str:
    """Bucket a market by keyword. Returns one of the module-level constants.

    Matches against the lowercased title and slug (slug words are hyphenated,
    so we also match against a de-hyphenated form).
    """
    hay = f" {(title or '').lower()} {(slug or '').lower()} {(slug or '').replace('-', ' ').lower()} "
    for category, keywords in _KEYWORDS:
        if any(kw in hay for kw in keywords):
            return category
    return OTHER


def is_high_stakes(category: str) -> bool:
    """True for categories that warrant a surge score boost (military/geopolitical)."""
    return category in HIGH_STAKES
