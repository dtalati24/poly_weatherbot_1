"""Tests for CLOB price ingestion and the price archive.

The tests that matter most guard three things:

  - `price_at` never reads a price from the future, and refuses to invent one
    outside the interval the series is actually evidence about. A backtest that
    gets this wrong fails silently and flatteringly.
  - change-point encoding round-trips exactly, since it is how the archive is
    stored and any loss there is permanent.
  - `market_window` stays inside the server's 15-day cap.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from weatherbot.priceharvest import (  # noqa: E402
    SCHEMA_VERSION,
    archive_path,
    count_revisions,
)
from weatherbot.sources.clob import (  # noqa: E402
    MAX_WINDOW_MINUTES,
    OutcomeSeries,
    PricePoint,
    market_window,
    to_change_points,
    token_ids,
)


def series(token: str, points: list[tuple[int, float]], label: str = "20C", **kw):
    pts = tuple(PricePoint(ts=t, price=p) for t, p in points)
    return OutcomeSeries(
        bucket_label=label,
        token_id=token,
        points=pts,
        first_ts=kw.get("first_ts", pts[0].ts if pts else None),
        last_ts=kw.get("last_ts", pts[-1].ts if pts else None),
    )


class TestTokenIds:
    def test_parses_the_json_encoded_string_gamma_returns(self):
        assert token_ids({"clobTokenIds": '["111", "222"]'}) == ("111", "222")

    def test_accepts_a_real_list_too(self):
        assert token_ids({"clobTokenIds": ["111", "222"]}) == ("111", "222")

    def test_missing_field(self):
        assert token_ids({}) is None

    def test_malformed_json_is_not_an_exception(self):
        assert token_ids({"clobTokenIds": "[not json"}) is None

    def test_too_few_ids(self):
        assert token_ids({"clobTokenIds": '["111"]'}) is None


class TestChangePoints:
    def test_drops_repeats_and_keeps_the_first_of_each_run(self):
        rows = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.6), (5, 0.6), (6, 0.5)]
        assert [(p.ts, p.price) for p in to_change_points(rows)] == [
            (1, 0.5),
            (4, 0.6),
            (6, 0.5),
        ]

    def test_keeps_a_price_that_returns_to_an_earlier_value(self):
        rows = [(1, 0.5), (2, 0.6), (3, 0.5)]
        assert len(to_change_points(rows)) == 3

    def test_empty(self):
        assert to_change_points([]) == []

    def test_reconstructs_every_original_sample(self):
        rows = [(t, round(0.4 + 0.01 * (t // 7 % 5), 2)) for t in range(1, 200)]
        s = series("a", [(p.ts, p.price) for p in to_change_points(rows)],
                   first_ts=rows[0][0], last_ts=rows[-1][0])
        assert all(s.price_at(t) == p for t, p in rows), "encoding must be lossless"


class TestMarketWindow:
    def test_covers_the_day_and_the_lookback(self):
        start, end = market_window(date(2026, 7, 15), lookback_days=7)
        assert (end - start) // 60 == 8 * 24 * 60

    def test_stays_inside_the_server_cap(self):
        start, end = market_window(date(2026, 7, 15))
        assert (end - start) // 60 <= MAX_WINDOW_MINUTES

    def test_too_long_a_lookback_is_refused_here_not_by_the_server(self):
        with pytest.raises(ValueError, match="exceeds the server cap"):
            market_window(date(2026, 7, 15), lookback_days=20)


class TestPriceAt:
    s = series("t", [(1000, 0.10), (2000, 0.20), (3000, 0.30)],
               first_ts=1000, last_ts=4000)

    def test_exact_hit(self):
        assert self.s.price_at(2000) == 0.20

    def test_holds_the_last_change_forward(self):
        assert self.s.price_at(2500) == 0.20

    def test_never_reads_the_future(self):
        # 2999 must not see the 3000 change, however close it is.
        assert self.s.price_at(2999) == 0.20

    def test_holds_forward_to_the_end_of_coverage(self):
        assert self.s.price_at(4000) == 0.30

    def test_before_coverage_is_none(self):
        assert self.s.price_at(999) is None

    def test_beyond_coverage_plus_staleness_is_none(self):
        assert self.s.price_at(4000 + 7200, max_staleness=3600) is None

    def test_staleness_boundary_is_inclusive(self):
        assert self.s.price_at(4000 + 3600, max_staleness=3600) == 0.30

    def test_empty_series(self):
        assert OutcomeSeries("l", "t", ()).price_at(1000) is None

    def test_falls_back_to_point_extent_without_explicit_bounds(self):
        bare = OutcomeSeries("l", "t", (PricePoint(5, 0.5),))
        assert bare.price_at(5) == 0.5
        assert bare.price_at(4) is None


class TestCountRevisions:
    def test_identical_data_reports_nothing(self):
        stored = {
            "schema_version": SCHEMA_VERSION,
            "series": [{"token_id": "a", "points": [[1, 0.5], [2, 0.6]]}],
        }
        assert count_revisions(stored, [series("a", [(1, 0.5), (2, 0.6)])]) == 0

    def test_a_changed_price_at_a_stored_timestamp_is_a_revision(self):
        stored = {
            "schema_version": SCHEMA_VERSION,
            "series": [{"token_id": "a", "points": [[1, 0.5]]}],
        }
        assert count_revisions(stored, [series("a", [(1, 0.9)])]) == 1

    def test_new_timestamps_are_not_revisions(self):
        stored = {
            "schema_version": SCHEMA_VERSION,
            "series": [{"token_id": "a", "points": [[1, 0.5]]}],
        }
        assert count_revisions(stored, [series("a", [(1, 0.5), (2, 0.6)])]) == 0

    def test_an_older_schema_is_not_compared(self):
        # Schema 1 sampled a different grid; comparing would report noise.
        stored = {"schema_version": 1, "series": [{"token_id": "a", "points": [[1, 0.9]]}]}
        assert count_revisions(stored, [series("a", [(1, 0.5)])]) == 0

    def test_no_previous_record(self):
        assert count_revisions(None, [series("a", [(1, 0.5)])]) == 0


class TestArchivePath:
    def test_is_keyed_by_market_day_under_its_year(self, tmp_path):
        path = archive_path(date(2026, 7, 15), root=tmp_path)
        assert path == tmp_path / "2026" / "2026-07-15.json.gz"


class TestRoundTrip:
    def test_write_then_load_preserves_the_series_and_its_bounds(self, tmp_path):
        from weatherbot.priceharvest import load_day, write_record

        write_record(
            {
                "schema_version": SCHEMA_VERSION,
                "day": "2026-07-15",
                "series": [
                    {
                        "bucket_label": "22C",
                        "token_id": "a",
                        "first_ts": 0,
                        "last_ts": 99,
                        "points": [[1, 0.5], [2, 0.6]],
                    }
                ],
            },
            archive_path(date(2026, 7, 15), root=tmp_path),
        )
        loaded = load_day(date(2026, 7, 15), root=tmp_path)
        assert len(loaded) == 1
        assert loaded[0].bucket_label == "22C"
        assert loaded[0].covers == (0, 99)
        assert loaded[0].price_at(50) == 0.6

    def test_missing_day_loads_as_empty(self, tmp_path):
        from weatherbot.priceharvest import load_day

        assert load_day(date(2026, 1, 1), root=tmp_path) == []


class TestRealArchive:
    """Sanity checks against the committed archive, not fixtures."""

    def test_archived_days_are_readable_and_well_formed(self):
        from weatherbot.priceharvest import archived_days, load_day

        days = archived_days()
        if not days:
            pytest.skip("price archive not populated in this checkout")
        sample = load_day(days[len(days) // 2])
        assert len(sample) >= 7, "a London market has 7-11 buckets"
        for s in sample:
            assert s.points, f"{s.bucket_label} archived with no points"
            timestamps = [p.ts for p in s.points]
            assert timestamps == sorted(timestamps), "points must be time-ordered"
            assert len(timestamps) == len(set(timestamps)), "duplicate timestamps"
            assert all(0.0 <= p.price <= 1.0 for p in s.points)

    def test_consecutive_change_points_actually_differ(self):
        """Only meaningful at schema 2 -- schema 1 stored raw samples."""
        from weatherbot.priceharvest import (
            archive_path,
            archived_days,
            load_day,
            read_record,
        )

        checked = 0
        for day in archived_days():
            if read_record(archive_path(day)).get("schema_version") != SCHEMA_VERSION:
                continue
            for s in load_day(day):
                prices = [p.price for p in s.points]
                assert all(a != b for a, b in zip(prices, prices[1:])), (
                    f"{day} {s.bucket_label}: repeated price means the "
                    f"change-point encoding is not minimal"
                )
            checked += 1
            if checked >= 20:
                break
        if not checked:
            pytest.skip("no schema-2 records archived in this checkout")
