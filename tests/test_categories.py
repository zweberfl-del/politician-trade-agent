"""Market topic classification."""

from __future__ import annotations

from src.data.categories import (
    ECONOMY,
    ELECTION,
    GEOPOLITICAL,
    MILITARY,
    OTHER,
    classify_market,
    is_high_stakes,
)


class TestClassifyMarket:
    def test_military_from_title(self) -> None:
        assert classify_market("Will the US launch an airstrike on Iran?") == MILITARY

    def test_military_from_slug(self) -> None:
        assert classify_market("", "us-military-operation-yemen") == MILITARY

    def test_military_beats_geopolitical(self) -> None:
        # Contains both "war" (geopolitical) and "missile" (military) -> military
        assert classify_market("Missile strike before ceasefire in the war?") == MILITARY

    def test_geopolitical(self) -> None:
        assert classify_market("Will new sanctions hit Russia this month?") == GEOPOLITICAL

    def test_election(self) -> None:
        assert classify_market("Who wins the 2028 presidential election?") == ELECTION

    def test_economy(self) -> None:
        assert classify_market("Will the Fed cut interest rates in March?") == ECONOMY

    def test_other(self) -> None:
        assert classify_market("Will it rain in Seattle on Tuesday?") == OTHER

    def test_dehyphenated_slug_match(self) -> None:
        assert classify_market("", "will-there-be-a-ground-offensive") == MILITARY

    def test_case_insensitive(self) -> None:
        assert classify_market("NUCLEAR TEST BY NORTH KOREA") == MILITARY

    def test_empty(self) -> None:
        assert classify_market("", "") == OTHER


class TestIsHighStakes:
    def test_military_high_stakes(self) -> None:
        assert is_high_stakes(MILITARY)

    def test_geopolitical_high_stakes(self) -> None:
        assert is_high_stakes(GEOPOLITICAL)

    def test_election_not_high_stakes(self) -> None:
        assert not is_high_stakes(ELECTION)

    def test_other_not_high_stakes(self) -> None:
        assert not is_high_stakes(OTHER)
