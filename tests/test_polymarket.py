"""Tests for market parsing.

Two failure modes these guard against, both of which actually bit during
Phase 0:

  1. A naive integer scan reads the range "54-55F" as 54 and -55, because the
     separator looks like a minus sign.
  2. The unsuffixed slug form resolves to whichever year Polymarket happened to
     use it for, silently pairing observations with another year's settlement.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.sources.polymarket import (  # noqa: E402
    BucketKind,
    celsius_to_fahrenheit_int,
    event_target_date,
    parse_bucket,
    parse_resolved_event,
    slug_candidates,
)


class TestParseBucketCelsius:
    def test_exact(self):
        bucket = parse_bucket("28°C")
        assert (bucket.kind, bucket.unit, bucket.low) == (BucketKind.EXACT, "C", 28)

    def test_at_or_below(self):
        bucket = parse_bucket("23°C or below")
        assert bucket.kind is BucketKind.AT_OR_BELOW
        assert bucket.contains(23) and bucket.contains(10)
        assert not bucket.contains(24)

    def test_at_or_above(self):
        bucket = parse_bucket("33°C or higher")
        assert bucket.kind is BucketKind.AT_OR_ABOVE
        assert bucket.contains(33) and bucket.contains(40)
        assert not bucket.contains(32)

    def test_negative_value(self):
        bucket = parse_bucket("-2°C")
        assert (bucket.kind, bucket.low) == (BucketKind.EXACT, -2)


class TestParseBucketFahrenheit:
    def test_hyphen_range_is_not_read_as_negative(self):
        bucket = parse_bucket("54-55°F")
        assert (bucket.kind, bucket.unit) == (BucketKind.RANGE, "F")
        assert (bucket.low, bucket.high) == (54, 55)

    def test_en_dash_range(self):
        bucket = parse_bucket("64–65°F")
        assert (bucket.low, bucket.high) == (64, 65)

    def test_fahrenheit_tail(self):
        bucket = parse_bucket("63°F or higher")
        assert bucket.kind is BucketKind.AT_OR_ABOVE
        assert bucket.unit == "F"

    def test_range_containment_converts_from_celsius(self):
        # 18C -> 64.4F -> 64F, which falls inside 64-65F.
        assert parse_bucket("64–65°F").contains(18)
        assert not parse_bucket("64–65°F").contains(17)

    def test_real_settlements(self):
        # Both verified against settled markets during Phase 0.
        assert parse_bucket("63°F or higher").contains(18)   # 2025-10-04
        assert parse_bucket("56-57°F").contains(14)          # 2025-10-14
        assert not parse_bucket("56-57°F").contains(13)

    def test_empty_and_garbage(self):
        assert parse_bucket("") is None
        assert parse_bucket("   ") is None
        assert parse_bucket("no digits here") is None


class TestCelsiusToFahrenheit:
    def test_known_conversions(self):
        assert celsius_to_fahrenheit_int(18) == 64   # 64.4
        assert celsius_to_fahrenheit_int(14) == 57   # 57.2
        assert celsius_to_fahrenheit_int(13) == 55   # 55.4
        assert celsius_to_fahrenheit_int(0) == 32


class TestSlugAndDateGuard:
    def test_year_suffixed_slug_is_tried_first(self):
        slugs = slug_candidates(date(2026, 7, 30))
        assert slugs[0] == "highest-temperature-in-london-on-july-30-2026"
        assert slugs[1] == "highest-temperature-in-london-on-july-30"

    def test_target_date_prefers_event_date(self):
        event = {"eventDate": "2026-07-30", "endDate": "2026-07-30T12:00:00Z"}
        assert event_target_date(event) == date(2026, 7, 30)

    def test_target_date_falls_back_to_end_date(self):
        # The real 'may-17' event: unsuffixed slug, but a 2025 event.
        assert event_target_date({"endDate": "2025-05-17T12:00:00Z"}) == date(2025, 5, 17)

    def test_target_date_missing(self):
        assert event_target_date({}) is None


class TestParseResolvedEvent:
    def _event(self, winner_index: int):
        labels = ["23°C or below", "24°C", "25°C"]
        return {
            "slug": "test",
            "markets": [
                {
                    "groupItemTitle": label,
                    "outcomePrices": '["1", "0"]' if i == winner_index else '["0", "1"]',
                }
                for i, label in enumerate(labels)
            ],
        }

    def test_identifies_winner(self):
        market = parse_resolved_event(self._event(1), date(2026, 5, 27))
        assert market.winner.low == 24
        assert not market.is_censored
        assert market.agrees_with(24)
        assert not market.agrees_with(25)

    def test_censored_tail_winner(self):
        market = parse_resolved_event(self._event(0), date(2026, 5, 27))
        assert market.is_censored
        assert market.agrees_with(10)

    def test_unresolved_event_returns_none(self):
        event = {
            "markets": [
                {"groupItemTitle": "24°C", "outcomePrices": '["0.5", "0.5"]'},
            ]
        }
        assert parse_resolved_event(event, date(2026, 5, 27)) is None

    def test_ambiguous_double_winner_returns_none(self):
        event = {
            "markets": [
                {"groupItemTitle": "24°C", "outcomePrices": '["1", "0"]'},
                {"groupItemTitle": "25°C", "outcomePrices": '["1", "0"]'},
            ]
        }
        assert parse_resolved_event(event, date(2026, 5, 27)) is None

    def test_agrees_with_none_is_false(self):
        market = parse_resolved_event(self._event(1), date(2026, 5, 27))
        assert not market.agrees_with(None)
