"""Tests for the settlement reconstruction logic."""

from __future__ import annotations

import pathlib
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.observation import Observation  # noqa: E402
from weatherbot.resolve import (  # noqa: E402
    DayBoundary,
    ReportFilter,
    Rounding,
    Strategy,
    _round_half_up,
    all_strategies,
    daily_maxima,
    resolve,
)


def obs(timestamp: str, tmpc: float | None, metar: str | None = None) -> Observation:
    """Build an observation from a 'YYYY-MM-DD HH:MM' UTC string."""
    return Observation(
        station="EGLC",
        valid_utc=datetime.strptime(timestamp, "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone.utc
        ),
        tmpc=tmpc,
        metar=metar,
        source="test",
    )


class TestRoundHalfUp:
    """Python's built-in round() uses banker's rounding, which we must not."""

    def test_halves_round_away_from_zero(self):
        assert _round_half_up(27.5) == 28
        assert _round_half_up(26.5) == 27  # round() would give 26
        assert _round_half_up(-2.5) == -3

    def test_ordinary_values(self):
        assert _round_half_up(27.4) == 27
        assert _round_half_up(27.6) == 28
        assert _round_half_up(0.0) == 0
        assert _round_half_up(-0.4) == 0


class TestDayBoundary:
    """The real 2026-05-27 case: an observation just after local midnight.

    23:20Z on 26 May is 00:20 BST on 27 May. Under a local-day boundary it
    belongs to the 27th; under a UTC boundary it belongs to the 26th.
    """

    observations = [
        obs("2026-05-26 22:20", 27.0),  # 23:20 local, 26 May
        obs("2026-05-26 23:20", 25.0),  # 00:20 local, 27 May
        obs("2026-05-27 00:20", 24.0),  # 01:20 local, 27 May
        obs("2026-05-27 12:20", 24.0),  # 13:20 local, 27 May
    ]

    def test_local_boundary_includes_post_midnight_observation(self):
        maxima = daily_maxima(self.observations, Strategy(boundary=DayBoundary.LOCAL))
        assert maxima[date(2026, 5, 27)].tmax_c == 25

    def test_utc_boundary_excludes_post_midnight_observation(self):
        maxima = daily_maxima(self.observations, Strategy(boundary=DayBoundary.UTC))
        assert maxima[date(2026, 5, 27)].tmax_c == 24
        assert maxima[date(2026, 5, 26)].tmax_c == 27

    def test_resolve_matches_daily_maxima(self):
        strategy = Strategy(boundary=DayBoundary.LOCAL)
        assert resolve(self.observations, date(2026, 5, 27), strategy) == 25

    def test_resolve_returns_none_for_day_without_data(self):
        assert resolve(self.observations, date(2020, 1, 1)) is None


class TestRounding:
    def test_fahrenheit_round_trip_is_identity_for_integers(self):
        """Integer Celsius survives a C -> F -> C round trip unchanged.

        This is why the via_fahrenheit hypothesis is indistinguishable from
        as_reported at EGLC: METARs are already whole degrees Celsius.
        """
        observations = [obs(f"2026-07-15 {h:02d}:20", float(c))
                        for h, c in enumerate(range(-10, 14))]
        as_reported = daily_maxima(observations, Strategy(rounding=Rounding.AS_REPORTED))
        via_f = daily_maxima(observations, Strategy(rounding=Rounding.VIA_FAHRENHEIT))
        assert {d: m.tmax_c for d, m in as_reported.items()} == {
            d: m.tmax_c for d, m in via_f.items()
        }


class TestMissingAndFiltering:
    def test_missing_temperatures_are_ignored(self):
        observations = [
            obs("2026-07-15 10:20", None),
            obs("2026-07-15 12:20", 24.0),
        ]
        maxima = daily_maxima(observations)
        assert maxima[date(2026, 7, 15)].tmax_c == 24
        assert maxima[date(2026, 7, 15)].n_obs == 1

    def test_day_with_only_missing_data_is_omitted(self):
        maxima = daily_maxima([obs("2026-07-15 10:20", None)])
        assert maxima == {}

    def test_routine_only_filter_drops_specials(self):
        observations = [
            obs("2026-07-15 12:20", 24.0, metar="EGLC 151220Z AUTO 24/12"),
            obs("2026-07-15 12:40", 28.0, metar="SPECI EGLC 151240Z AUTO 28/12"),
        ]
        assert daily_maxima(observations, Strategy(reports=ReportFilter.ALL))[
            date(2026, 7, 15)
        ].tmax_c == 28
        assert daily_maxima(
            observations, Strategy(reports=ReportFilter.ROUTINE_ONLY)
        )[date(2026, 7, 15)].tmax_c == 24


class TestProvenance:
    def test_argmax_reports_first_occurrence_of_the_maximum(self):
        observations = [
            obs("2026-07-15 11:20", 28.0),
            obs("2026-07-15 14:20", 28.0),
        ]
        daily = daily_maxima(observations)[date(2026, 7, 15)]
        assert daily.argmax_utc.hour == 11
        assert daily.n_obs == 2

    def test_local_conversion_applies_bst_offset(self):
        daily = daily_maxima([obs("2026-07-15 11:20", 28.0)])[date(2026, 7, 15)]
        assert daily.argmax_local.hour == 12  # BST = UTC+1


def test_all_strategies_are_unique_and_complete():
    strategies = all_strategies()
    assert len(strategies) == len(set(strategies))
    assert len(strategies) == len(DayBoundary) * len(Rounding) * len(ReportFilter)
