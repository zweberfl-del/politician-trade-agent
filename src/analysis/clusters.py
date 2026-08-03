"""Wallet clustering — linking accounts that appear to act as one.

Two independent methods, both pure:

- **behavioral** (`cooccurrence_clusters`) — wallets that keep buying the same
  ``(market, outcome)`` within a short window of each other are probably
  coordinated. Build a graph whose edge weight is the number of such shared
  events; connected components joined by an edge at/above ``min_shared`` become
  a cluster. Needs no data beyond the trade feed we already pull.

- **on-chain** (`funder_clusters`) — wallets that were first funded by the same
  address are linked on Polygon itself (the "nine connected accounts" that
  Bubblemaps surfaced on 60 Minutes). Needs a ``{wallet: funder}`` map, which
  the optional Polygonscan client supplies.

``annotate_surge_with_clusters`` turns clusters into the human note a surge
alert carries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations


@dataclass
class Cluster:
    id: int
    members: set[str] = field(default_factory=set)
    method: str = "behavioral"  # "behavioral" | "funder"
    shared_context: str = ""


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


@dataclass
class CooccurrenceEvent:
    """A wallet's bet, reduced to what co-occurrence needs."""

    wallet: str
    market_slug: str
    outcome: str
    timestamp: int


def cooccurrence_clusters(
    events: list[CooccurrenceEvent],
    *,
    window_seconds: int,
    min_shared: int = 2,
) -> list[Cluster]:
    """Cluster wallets that repeatedly co-bet the same outcome within a window.

    Two wallets share an "event" each time both bought the same
    ``(market_slug, outcome)`` with timestamps within *window_seconds*. Pairs
    reaching *min_shared* shared events are unioned into a cluster.
    """
    # Group events by market+outcome, then count time-proximate wallet pairs.
    by_key: dict[tuple[str, str], list[CooccurrenceEvent]] = {}
    for e in events:
        by_key.setdefault((e.market_slug, e.outcome), []).append(e)

    pair_counts: dict[tuple[str, str], int] = {}
    for group in by_key.values():
        for a, b in combinations(group, 2):
            if a.wallet == b.wallet:
                continue
            if abs(a.timestamp - b.timestamp) > window_seconds:
                continue
            pair = tuple(sorted((a.wallet, b.wallet)))
            pair_counts[pair] = pair_counts.get(pair, 0) + 1

    uf = _UnionFind()
    linked = False
    for (a, b), count in pair_counts.items():
        if count >= min_shared:
            uf.union(a, b)
            linked = True

    if not linked:
        return []

    roots: dict[str, set[str]] = {}
    for wallet in uf.parent:
        roots.setdefault(uf.find(wallet), set()).add(wallet)

    clusters = [
        Cluster(id=i, members=members, method="behavioral",
                shared_context=f"co-bet the same outcome ≥{min_shared}× together")
        for i, members in enumerate(m for m in roots.values() if len(m) > 1)
    ]
    return clusters


def funder_clusters(funding: dict[str, str]) -> list[Cluster]:
    """Group wallets that share a first-funder address.

    *funding* maps ``wallet -> funder_address``; wallets with no known funder
    (value falsy) are skipped. Only funders backing 2+ wallets form a cluster.
    """
    by_funder: dict[str, set[str]] = {}
    for wallet, funder in funding.items():
        if not funder:
            continue
        by_funder.setdefault(funder.lower(), set()).add(wallet.lower())

    clusters = []
    cid = 0
    for funder, members in by_funder.items():
        if len(members) < 2:
            continue
        clusters.append(
            Cluster(id=cid, members=members, method="funder",
                    shared_context=f"funded by {_short(funder)}")
        )
        cid += 1
    return clusters


def annotate_surge_with_clusters(wallets: list[str], clusters: list[Cluster]) -> str:
    """Human note: the largest cluster overlap among a surge's wallets.

    Returns "" when no cluster covers 2+ of the surge's wallets.
    """
    members = {w.lower() for w in wallets}
    best: tuple[int, Cluster] | None = None
    for c in clusters:
        overlap = len(members & {m.lower() for m in c.members})
        if overlap >= 2 and (best is None or overlap > best[0]):
            best = (overlap, c)
    if best is None:
        return ""
    overlap, c = best
    return f"{overlap} of {len(members)} wallets {c.shared_context}"


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr
