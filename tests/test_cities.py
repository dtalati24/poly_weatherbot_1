"""Tests for multi-city settlement, and for the Fahrenheit rounding trap.

The trap is the point of this file. London's station reports whole degrees
Celsius, so converting an integer Celsius value to Fahrenheit is lossless. US
stations report tenths via the METAR remark T-group, so the same code path
rounds twice and lands in the wrong bucket on roughly a quarter of days.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from weatherbot.cities import CITIES, LONDON, LOS_ANGELES, NEW_YORK, get  # noqa: E402
from weatherbot.observation import Observation  # noqa: E402
from weatherbot.resolve import (  # noqa: E402
    DayBoundary,
    Strategy,
    daily_maxima,
    daily_maxima_fahrenheit,
)
from weatherbot.sources.polymarket import parse_bucket, slug_candidates  # noqa: E402


def obs(when: datetime, tmpc: float, station: str = "KLAX"):
    return Observation(
        station=station, valid_utc=when, tmpc=tmpc,
        dwpc=None, drct=None, sknt=None, gust=None,
        skyc1=None, vsby=None, metar="", source="test",
    )


class TestCityRegistry:
    def test_lookup(self):
        assert get("los-angeles") is LOS_ANGELES

    def test_unknown_city_lists_the_options(self):
        with pytest.raises(KeyError, match="unknown city"):
            get("atlantis")

    def test_la_shares_a_station_with_kalshi(self):
        assert LOS_ANGELES.same_station_as_kalshi is True

    def test_nyc_does_not(self):
        # Kalshi settles Central Park, Polymarket settles LaGuardia.
        assert NEW_YORK.same_station_as_kalshi is False
        assert NEW_YORK.station != NEW_YORK.kalshi_station

    def test_london_has_no_kalshi_counterpart(self):
        assert LONDON.same_station_as_kalshi is False

    def test_units(self):
        assert LONDON.settles_in_fahrenheit is False
        assert LOS_ANGELES.settles_in_fahrenheit is True

    def test_every_city_has_a_distinct_slug_prefix(self):
        prefixes = [c.slug_prefix for c in CITIES.values()]
        assert len(set(prefixes)) == len(prefixes)


class TestSlugPrefix:
    def test_defaults_to_london(self):
        assert slug_candidates(date(2026, 8, 2))[0].startswith(
            "highest-temperature-in-london-on"
        )

    def test_takes_a_city_prefix(self):
        got = slug_candidates(date(2026, 8, 2), LOS_ANGELES.slug_prefix)
        assert got[0] == "highest-temperature-in-los-angeles-on-august-2-2026"

    def test_year_suffixed_form_comes_first(self):
        got = slug_candidates(date(2026, 8, 2), LOS_ANGELES.slug_prefix)
        assert got[1] == "highest-temperature-in-los-angeles-on-august-2"


class TestTimezoneIsPerCity:
    # 07:00 UTC on 2 Aug is 00:00 local in LA, and 08:00 local in London.
    moment = datetime(2026, 8, 2, 7, 0, tzinfo=timezone.utc)

    def test_london_default_attributes_it_to_the_2nd(self):
        got = daily_maxima([obs(self.moment, 20.0)], Strategy())
        assert list(got) == [date(2026, 8, 2)]

    def test_la_attributes_the_same_instant_to_the_2nd_too(self):
        got = daily_maxima([obs(self.moment, 20.0)], Strategy(tz=LOS_ANGELES.tz))
        assert list(got) == [date(2026, 8, 2)]

    def test_an_instant_that_straddles_lands_differently(self):
        # 06:00 UTC on 2 Aug is 23:00 local on 1 Aug in LA, 07:00 in London.
        straddle = datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)
        london = daily_maxima([obs(straddle, 20.0)], Strategy())
        la = daily_maxima([obs(straddle, 20.0)], Strategy(tz=LOS_ANGELES.tz))
        assert list(london) == [date(2026, 8, 2)]
        assert list(la) == [date(2026, 8, 1)]

    def test_utc_boundary_ignores_the_city_timezone(self):
        straddle = datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)
        got = daily_maxima(
            [obs(straddle, 20.0)],
            Strategy(boundary=DayBoundary.UTC, tz=LOS_ANGELES.tz),
        )
        assert list(got) == [date(2026, 8, 2)]


class TestFahrenheitReduction:
    """30.4 C is 86.7 F -> 87. Rounding to 30 C first gives 86 F. Different bucket."""

    series = [obs(datetime(2026, 8, 2, 20, 53, tzinfo=timezone.utc), 30.4)]

    def test_converts_from_the_raw_value_not_the_rounded_one(self):
        got = daily_maxima_fahrenheit(self.series, Strategy(tz=LOS_ANGELES.tz))
        assert got[date(2026, 8, 2)] == 87

    def test_the_celsius_path_would_have_double_rounded(self):
        celsius = daily_maxima(self.series, Strategy(tz=LOS_ANGELES.tz))
        assert celsius[date(2026, 8, 2)].tmax_c == 30
        from weatherbot.sources.polymarket import celsius_to_fahrenheit_int

        assert celsius_to_fahrenheit_int(30) == 86, "the wrong answer, one degree low"

    def test_max_commutes_with_the_conversion(self):
        """Converting per observation must give the same answer as converting once."""
        from weatherbot.resolve import _round_half_up

        values = [12.3, 25.61, 30.44, 29.9, 18.05]
        series = [
            obs(datetime(2026, 8, 2, 10 + i, 53, tzinfo=timezone.utc), v)
            for i, v in enumerate(values)
        ]
        once = daily_maxima_fahrenheit(series, Strategy(tz=LOS_ANGELES.tz))
        per_obs = max(_round_half_up(v * 9 / 5 + 32) for v in values)
        assert once[date(2026, 8, 2)] == per_obs

    def test_empty_input(self):
        assert daily_maxima_fahrenheit([], Strategy()) == {}


class TestBucketHolds:
    def test_holds_takes_the_value_in_the_buckets_own_unit(self):
        b = parse_bucket("80-81°F")
        assert b.unit == "F"
        assert b.holds(80) and b.holds(81)
        assert not b.holds(79) and not b.holds(82)

    def test_holds_does_not_convert(self):
        """contains() would read 80 as Celsius and convert it to 176F."""
        b = parse_bucket("80-81°F")
        assert b.holds(80) is True
        assert b.contains(80) is False

    def test_open_ended_buckets(self):
        low = parse_bucket("77°F or below")
        high = parse_bucket("86°F or above")
        assert low.holds(70) and low.holds(77) and not low.holds(78)
        assert high.holds(90) and high.holds(86) and not high.holds(85)

    def test_celsius_buckets_are_unaffected(self):
        b = parse_bucket("24°C")
        assert b.holds(24) and b.contains(24)
