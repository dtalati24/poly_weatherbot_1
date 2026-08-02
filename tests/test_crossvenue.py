"""Tests for Kalshi ingestion and the cross-venue basis change.

Two of these guard bugs that actually occurred and were expensive to find:
an empty Kalshi book reported as bid 0 / ask 100 being read as a genuine 50c
quote, and the two venues settling on different numbers.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from weatherbot.crossvenue import (  # noqa: E402
    SourceOffset,
    alignment,
    to_distribution,
    to_poly_buckets,
)
from weatherbot.sources.kalshi import (  # noqa: E402
    KalshiCandle,
    KalshiSnapshot,
    parse_bucket,
    parse_ticker_day,
    snapshot_from_series,
)
from weatherbot.sources.polymarket import BucketKind  # noqa: E402
from weatherbot.sources.polymarket import parse_bucket as poly_bucket  # noqa: E402


def candle(ts, bid, ask):
    return KalshiCandle(ts=ts, bid=bid, ask=ask, mean=None, volume=0.0,
                        open_interest=0.0)


class TestTickerParsing:
    def test_extracts_the_event_day(self):
        assert parse_ticker_day("KXHIGHLAX-26AUG01-B80.5") == date(2026, 8, 1)

    def test_rejects_a_malformed_ticker(self):
        assert parse_ticker_day("nonsense") is None


class TestKalshiBucketParsing:
    def test_range(self):
        b = parse_bucket("80° to 81°")
        assert (b.kind, b.low, b.high, b.unit) == (BucketKind.RANGE, 80, 81, "F")

    def test_or_below(self):
        b = parse_bucket("77° or below")
        assert b.kind is BucketKind.AT_OR_BELOW and b.low == 77

    def test_or_above(self):
        b = parse_bucket("86° or above")
        assert b.kind is BucketKind.AT_OR_ABOVE and b.low == 86

    def test_open_ended_is_not_swallowed_as_exact(self):
        """The tails contain a bare number; order of the patterns matters."""
        assert parse_bucket("77° or below").kind is not BucketKind.EXACT

    def test_empty(self):
        assert parse_bucket("") is None


class TestEmptyBookIsNotAQuote:
    """Kalshi reports an empty book as bid 0 / ask 1, not as null."""

    def test_empty_book_has_no_mid(self):
        assert candle(1, 0.0, 1.0).mid is None
        assert candle(1, 0.0, 1.0).has_book is False

    def test_one_sided_book_has_no_mid(self):
        assert candle(1, 0.0, 0.4).mid is None
        assert candle(1, 0.6, 1.0).mid is None

    def test_a_real_book_does(self):
        c = candle(1, 0.53, 0.54)
        assert c.has_book is True
        assert c.mid == pytest.approx(0.535)
        assert c.spread == pytest.approx(0.01)

    def test_null_fields(self):
        assert candle(1, None, None).mid is None


class TestSnapshot:
    buckets = {
        "a": parse_bucket("77° or below"),
        "b": parse_bucket("78° to 79°"),
        "c": parse_bucket("80° to 81°"),
        "d": parse_bucket("82° or above"),
    }

    def series(self, **kw):
        base = {k: [candle(100, 0.2, 0.22)] for k in self.buckets}
        base.update(kw)
        return base

    def test_builds_a_sorted_cross_section(self):
        snap = snapshot_from_series(self.series(), self.buckets, 200)
        assert [b.low for b in snap.buckets] == [77, 78, 80, 82]

    def test_reads_backwards_only(self):
        series = self.series(c=[candle(100, 0.2, 0.22), candle(300, 0.9, 0.92)])
        snap = snapshot_from_series(series, self.buckets, 200)
        assert snap.mids[2] == pytest.approx(0.21), "the 300 candle is in the future"

    def test_empty_books_are_excluded_not_treated_as_fifty_cents(self):
        series = self.series(d=[candle(100, 0.0, 1.0)])
        snap = snapshot_from_series(series, self.buckets, 200, min_buckets=3)
        assert len(snap.buckets) == 3
        assert all(m < 0.5 for m in snap.mids)

    def test_too_few_quoted_buckets_returns_none(self):
        series = self.series(c=[candle(100, 0.0, 1.0)], d=[candle(100, 0.0, 1.0)])
        assert snapshot_from_series(series, self.buckets, 200, min_buckets=4) is None

    def test_stale_quotes_are_refused(self):
        assert snapshot_from_series(self.series(), self.buckets, 999999) is None

    def test_probabilities_normalise(self):
        snap = snapshot_from_series(self.series(), self.buckets, 200)
        assert sum(snap.probabilities) == pytest.approx(1.0)


class TestAlignment:
    def test_same_lattice(self):
        k = (parse_bucket("80° to 81°"), parse_bucket("82° to 83°"))
        p = (poly_bucket("78-79°F"), poly_bucket("80-81°F"))
        assert alignment(k, p).aligned is True

    def test_offset_by_one_degree(self):
        k = (parse_bucket("65° to 66°"), parse_bucket("67° to 68°"))
        p = (poly_bucket("64-65°F"), poly_bucket("66-67°F"))
        assert alignment(k, p).aligned is False


class TestToDistribution:
    snap = KalshiSnapshot(
        ts=0,
        buckets=(parse_bucket("78° to 79°"), parse_bucket("80° to 81°")),
        mids=(0.4, 0.6),
        spreads=(0.01, 0.01),
    )

    def test_preserves_each_buckets_probability(self):
        d = to_distribution(self.snap)
        assert d.probability(78) + d.probability(79) == pytest.approx(0.4, abs=1e-3)
        assert d.probability(80) + d.probability(81) == pytest.approx(0.6, abs=1e-3)

    def test_uniform_split_within_a_bucket(self):
        d = to_distribution(self.snap)
        assert d.probability(78) == pytest.approx(d.probability(79), abs=1e-6)

    def test_normalised(self):
        assert sum(to_distribution(self.snap).probabilities) == pytest.approx(1.0)

    def test_maps_onto_polymarket_buckets(self):
        poly = (poly_bucket("78-79°F"), poly_bucket("80-81°F"))
        got = to_poly_buckets(self.snap, poly)
        assert got[0] == pytest.approx(0.4, abs=1e-3)
        assert got[1] == pytest.approx(0.6, abs=1e-3)

    def test_an_offset_ladder_splits_mass_across_two_buckets(self):
        poly = (poly_bucket("77-78°F"), poly_bucket("79-80°F"), poly_bucket("81-82°F"))
        got = to_poly_buckets(self.snap, poly)
        assert all(p > 0 for p in got), "mass must be split, not dropped"
        assert sum(got) == pytest.approx(1.0)


class TestSourceOffset:
    def test_learns_that_kalshi_settles_higher(self):
        # Polymarket settles 1F below Kalshi on most days.
        pairs = [(80, 79)] * 7 + [(80, 80)] * 3
        offset = SourceOffset.fit(pairs)
        assert offset.mean < 0
        assert offset.weights[-1] > offset.weights[0]

    def test_smoothing_leaves_unseen_offsets_possible(self):
        offset = SourceOffset.fit([(80, 79)] * 20)
        assert all(w > 0 for w in offset.weights.values())

    def test_no_pairs_is_the_identity(self):
        assert SourceOffset.fit([]).weights == {0: 1.0}

    def test_apply_shifts_the_distribution_down(self):
        snap = KalshiSnapshot(
            ts=0, buckets=(parse_bucket("80° to 81°"),), mids=(1.0,), spreads=(0.01,)
        )
        base = to_distribution(snap)
        shifted = SourceOffset({-1: 1.0}).apply(base)
        assert shifted.probability(79) == pytest.approx(base.probability(80), abs=1e-6)

    def test_apply_preserves_total_mass(self):
        snap = KalshiSnapshot(
            ts=0, buckets=(parse_bucket("80° to 81°"),), mids=(1.0,), spreads=(0.01,)
        )
        shifted = SourceOffset({-1: 0.5, 0: 0.5}).apply(to_distribution(snap))
        assert sum(shifted.probabilities) == pytest.approx(1.0)
