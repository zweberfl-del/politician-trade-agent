"""Wallet clustering: behavioral co-occurrence and on-chain funder graph."""

from __future__ import annotations

from src.analysis.clusters import (
    CooccurrenceEvent,
    annotate_surge_with_clusters,
    cooccurrence_clusters,
    funder_clusters,
)

T0 = 1_800_000_000


def _ev(wallet: str, slug: str, outcome: str, t: int) -> CooccurrenceEvent:
    return CooccurrenceEvent(wallet=wallet, market_slug=slug, outcome=outcome, timestamp=t)


class TestCooccurrence:
    def test_two_wallets_cobet_repeatedly_cluster(self) -> None:
        events = [
            _ev("0xa", "m1", "Yes", T0),
            _ev("0xb", "m1", "Yes", T0 + 60),
            _ev("0xa", "m2", "Yes", T0 + 1000),
            _ev("0xb", "m2", "Yes", T0 + 1030),
        ]
        clusters = cooccurrence_clusters(events, window_seconds=300, min_shared=2)
        assert len(clusters) == 1
        assert clusters[0].members == {"0xa", "0xb"}
        assert clusters[0].method == "behavioral"

    def test_single_shared_event_below_threshold(self) -> None:
        events = [
            _ev("0xa", "m1", "Yes", T0),
            _ev("0xb", "m1", "Yes", T0 + 60),
        ]
        assert cooccurrence_clusters(events, window_seconds=300, min_shared=2) == []

    def test_far_apart_in_time_not_linked(self) -> None:
        events = [
            _ev("0xa", "m1", "Yes", T0),
            _ev("0xb", "m1", "Yes", T0 + 10_000),
            _ev("0xa", "m2", "Yes", T0),
            _ev("0xb", "m2", "Yes", T0 + 10_000),
        ]
        assert cooccurrence_clusters(events, window_seconds=300, min_shared=2) == []

    def test_different_outcomes_not_linked(self) -> None:
        events = [
            _ev("0xa", "m1", "Yes", T0),
            _ev("0xb", "m1", "No", T0 + 60),
            _ev("0xa", "m2", "Yes", T0),
            _ev("0xb", "m2", "No", T0 + 60),
        ]
        assert cooccurrence_clusters(events, window_seconds=300, min_shared=2) == []

    def test_transitive_cluster(self) -> None:
        # a~b and b~c each twice -> all three in one component
        events = []
        for i, slug in enumerate(["m1", "m2"]):
            events += [_ev("0xa", slug, "Yes", T0 + i), _ev("0xb", slug, "Yes", T0 + i + 30)]
        for i, slug in enumerate(["m3", "m4"]):
            events += [_ev("0xb", slug, "Yes", T0 + i), _ev("0xc", slug, "Yes", T0 + i + 30)]
        clusters = cooccurrence_clusters(events, window_seconds=300, min_shared=2)
        assert len(clusters) == 1
        assert clusters[0].members == {"0xa", "0xb", "0xc"}


class TestFunderClusters:
    def test_shared_funder_clusters(self) -> None:
        funding = {"0xa": "0xFUND", "0xb": "0xfund", "0xc": "0xother"}
        clusters = funder_clusters(funding)
        assert len(clusters) == 1
        assert clusters[0].members == {"0xa", "0xb"}
        assert clusters[0].method == "funder"

    def test_missing_funder_skipped(self) -> None:
        funding = {"0xa": "", "0xb": None, "0xc": "0xf"}  # type: ignore[dict-item]
        assert funder_clusters(funding) == []

    def test_solo_funder_not_a_cluster(self) -> None:
        assert funder_clusters({"0xa": "0xf1", "0xb": "0xf2"}) == []


class TestAnnotate:
    def test_note_reports_overlap(self) -> None:
        from src.analysis.clusters import Cluster

        clusters = [Cluster(id=0, members={"0xa", "0xb", "0xc"}, method="funder",
                            shared_context="funded by 0x1234…abcd")]
        note = annotate_surge_with_clusters(["0xA", "0xB", "0xZ"], clusters)
        assert "2 of 3" in note
        assert "funded by" in note

    def test_no_overlap_empty_note(self) -> None:
        from src.analysis.clusters import Cluster

        clusters = [Cluster(id=0, members={"0xa"}, method="behavioral")]
        assert annotate_surge_with_clusters(["0xz", "0xy"], clusters) == ""
