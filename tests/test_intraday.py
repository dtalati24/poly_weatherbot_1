"""Tests for intraday state and the nowcast models.

The leakage tests here are the ones that matter most. A forecast model that
leaks is usually obvious because its score becomes implausible; an intraday
model that leaks by one observation just looks very good, because seeing one
extra METAR is exactly the kind of small advantage a nowcast is supposed to
have. So the boundary is tested explicitly rather than trusted.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from weatherbot.config import LOCAL_TZ  # noqa: E402
from weatherbot.dataset import remaining_forecast_max  # noqa: E402
from weatherbot.intraday import (  # noqa: E402
    build_nowcast_samples,
    group_by_local_day,
    local_instant,
    local_midnight,
    remaining_max,
    state_at,
)
from weatherbot.models.nowcast import (  # noqa: E402
    IntradayNowcast,
    NowcastConfig,
    RemainingMaxNowcast,
)
from weatherbot.observation import Observation


def obs(hour: int, minute: int, tmpc: float, day: date = date(2026, 7, 15)):
    """An observation at a London-local wall-clock time."""
    moment = datetime.combine(day, datetime.min.time()).replace(
        hour=hour, minute=minute, tzinfo=LOCAL_TZ
    )
    return Observation(
        station="EGLC",
        valid_utc=moment.astimezone(timezone.utc),
        tmpc=tmpc,
        dwpc=None, drct=None, sknt=None, gust=None,
        skyc1=None, vsby=None, metar="", source="test",
    )


DAY = date(2026, 7, 15)
SERIES = [obs(2, 20, 14.0), obs(9, 20, 18.0), obs(13, 20, 24.0),
          obs(16, 20, 22.0), obs(21, 20, 17.0)]


class TestLocalDayBoundaries:
    def test_local_midnight_is_an_instant_not_a_wall_clock(self):
        assert local_midnight(DAY).astimezone(LOCAL_TZ).hour == 0
        assert local_midnight(DAY).utcoffset() == timedelta(0), (
            "returning UTC is what stops callers doing wall-clock arithmetic"
        )

    def test_local_instant_offsets_from_midnight(self):
        assert (local_instant(DAY, 13) - local_midnight(DAY)) == timedelta(hours=13)

    def test_spring_forward_day_has_23_hours(self):
        # 2026-03-29 is the UK spring-forward date; 01:00 local never happens.
        start = local_midnight(date(2026, 3, 29))
        end = local_midnight(date(2026, 3, 30))
        assert (end - start) == timedelta(hours=23)

    def test_autumn_back_day_has_25_hours(self):
        start = local_midnight(date(2026, 10, 25))
        end = local_midnight(date(2026, 10, 26))
        assert (end - start) == timedelta(hours=25)

    def test_hour_offsets_are_elapsed_time_not_wall_clock(self):
        """On the spring-forward day, hour 1 is 02:00 local because 01:00 does
        not exist. Elapsed time always has an answer; wall clock does not."""
        moment = local_instant(date(2026, 3, 29), 1)
        assert moment.astimezone(LOCAL_TZ).hour == 2
        assert local_instant(date(2026, 3, 29), 23) == local_midnight(date(2026, 3, 30))

    def test_grouping_uses_the_local_day(self):
        # 23:30 UTC on 14 July is 00:30 local on 15 July in summer.
        late = Observation(
            station="EGLC",
            valid_utc=datetime(2026, 7, 14, 23, 30, tzinfo=timezone.utc),
            tmpc=15.0, dwpc=None, drct=None, sknt=None, gust=None,
            skyc1=None, vsby=None, metar="", source="test",
        )
        assert list(group_by_local_day([late])) == [date(2026, 7, 15)]


class TestStateAtIsLeakageFree:
    def test_sees_only_the_past(self):
        state = state_at(SERIES, DAY, local_instant(DAY, 12))
        assert state.running_max == 18, "the 13:20 peak must not be visible at 12:00"
        assert state.n_obs == 2

    def test_the_boundary_is_inclusive_of_its_own_instant(self):
        exact = SERIES[2].valid_utc  # 13:20 local
        assert state_at(SERIES, DAY, exact).running_max == 24
        assert state_at(SERIES, DAY, exact - timedelta(seconds=1)).running_max == 18

    def test_running_max_is_monotone_through_the_day(self):
        seen = [
            state_at(SERIES, DAY, local_instant(DAY, h)).running_max for h in range(1, 24)
        ]
        present = [m for m in seen if m is not None]
        assert present == sorted(present)

    def test_before_the_first_observation_there_is_no_bound(self):
        assert state_at(SERIES, DAY, local_instant(DAY, 0)).running_max is None

    def test_end_of_day_running_max_equals_the_settled_value(self):
        assert state_at(SERIES, DAY, local_instant(DAY, 24)).running_max == 24

    def test_staleness_is_flagged(self):
        # Latest observation 09:20 local; fresh at 10:00, stale by 12:00.
        assert state_at(SERIES, DAY, local_instant(DAY, 10)).is_stale is False
        assert state_at(SERIES, DAY, local_instant(DAY, 12)).is_stale is True

    def test_no_observations_counts_as_stale(self):
        assert state_at(SERIES, DAY, local_instant(DAY, 0)).is_stale is True

    def test_local_hour_reads_off_the_london_clock(self):
        assert state_at(SERIES, DAY, local_instant(DAY, 13.5)).local_hour == 13.5


class TestRemainingMaxPartitionsTheDay:
    def test_is_strictly_after_the_instant(self):
        assert remaining_max(SERIES, SERIES[2].valid_utc) == 22

    def test_max_of_both_halves_reconstructs_the_settled_value(self):
        """The identity Y = max(M, X) that Model D'' is built on."""
        for hour in range(0, 24):
            moment = local_instant(DAY, hour)
            running = state_at(SERIES, DAY, moment).running_max
            rest = remaining_max(SERIES, moment)
            parts = [p for p in (running, rest) if p is not None]
            assert max(parts) == 24, f"partition broken at hour {hour}"

    def test_after_the_last_observation_nothing_remains(self):
        assert remaining_max(SERIES, local_instant(DAY, 23.9)) is None


class TestRemainingForecastMax:
    hourly = {f"2026-07-15T{h:02d}:00": 10.0 + h for h in range(24)}

    def test_takes_only_hours_still_to_come(self):
        assert remaining_forecast_max(self.hourly, DAY, 20) == 33.0

    def test_includes_the_current_hour(self):
        assert remaining_forecast_max(self.hourly, DAY, 23) == 33.0

    def test_none_when_the_day_is_over(self):
        assert remaining_forecast_max(self.hourly, DAY, 24) is None

    def test_ignores_other_days(self):
        assert remaining_forecast_max(self.hourly, date(2026, 7, 16), 0) is None


class TestBuildNowcastSamples:
    by_day = {DAY: SERIES}

    def test_end_is_exclusive(self):
        rows = build_nowcast_samples(
            self.by_day, {DAY: 24}, (12.0,), end=DAY
        )
        assert rows == [], "the evaluation day must not leak into training"

    def test_rise_is_settled_minus_running_max(self):
        rows = build_nowcast_samples(self.by_day, {DAY: 24}, (12.0,))
        assert rows == [(12.0, 0.0, 6)]

    def test_a_settled_value_below_the_running_max_is_clipped_not_dropped(self):
        # WU dropping the peak METAR produces exactly this shape.
        rows = build_nowcast_samples(self.by_day, {DAY: 23}, (16.0,))
        assert rows and rows[0][2] == 0

    def test_it_can_be_dropped_instead(self):
        rows = build_nowcast_samples(
            self.by_day, {DAY: 23}, (16.0,), clip_negative=False
        )
        assert rows == []


def rise_samples(n_days: int = 400):
    """Synthetic days that lock in progressively through the afternoon."""
    rows = []
    for i in range(n_days):
        for hour in range(24):
            # Rise falls to zero after ~14:00, with deterministic variation.
            base = max(0, 14 - hour)
            rise = max(0, int(base * (0.5 + 0.5 * ((i % 7) / 6))))
            rows.append((float(hour), rise))
    return rows


class TestIntradayNowcast:
    model = IntradayNowcast().fit(rise_samples())

    def test_rejects_a_thin_sample(self):
        with pytest.raises(ValueError, match="at least 100"):
            IntradayNowcast().fit([(12.0, 0)] * 10)

    def test_rejects_a_negative_rise(self):
        with pytest.raises(ValueError, match="negative rise"):
            IntradayNowcast().fit(rise_samples() + [(12.0, -1)])

    def test_rise_distribution_is_normalised(self):
        assert sum(self.model.rise_distribution(12.0)) == pytest.approx(1.0)

    def test_lock_in_probability_increases_through_the_day(self):
        curve = [self.model.probability_locked(float(h)) for h in range(6, 23)]
        assert curve == sorted(curve)
        assert curve[-1] > 0.9

    def test_expected_rise_decreases_through_the_day(self):
        assert self.model.expected_rise(8.0) > self.model.expected_rise(16.0)

    def test_prediction_is_a_valid_distribution(self):
        dist = self.model.predict(12.0, 20)
        assert sum(dist.probabilities) == pytest.approx(1.0)

    def test_almost_no_mass_sits_below_the_running_maximum(self):
        dist = self.model.predict(12.0, 20)
        assert dist.cdf(19) < 0.01

    def test_but_the_bound_is_soft_not_absolute(self):
        # A hard zero would score a WU-dropped peak as impossible.
        dist = self.model.predict(12.0, 20)
        assert dist.probability(19) > 0.0

    def test_a_zero_floor_makes_the_bound_absolute(self):
        model = IntradayNowcast(
            NowcastConfig(below_bound_mass=0.0, floor=0.0)
        ).fit(rise_samples())
        assert model.predict(12.0, 20).probability(19) == 0.0

    def test_the_below_bound_mass_is_what_was_asked_for(self):
        dist = self.model.predict(12.0, 20)
        assert dist.cdf(19) == pytest.approx(0.005, abs=5e-4)

    def test_the_floor_is_concentrated_just_below_the_bound(self):
        """Every observed violation was exactly -1 C, so that is where it goes."""
        dist = self.model.predict(12.0, 20)
        assert dist.probability(19) / dist.probability(18) == pytest.approx(10, rel=0.02)
        assert dist.probability(18) > dist.probability(10)
        assert dist.probability(19) == pytest.approx(0.005 * 0.90, abs=2e-4)

    def test_late_in_the_day_it_is_nearly_a_point_mass(self):
        assert self.model.predict(22.0, 20).probability(20) > 0.95

    def test_the_distribution_tracks_the_running_maximum(self):
        assert self.model.predict(12.0, 25).mode() > self.model.predict(12.0, 20).mode()

    def test_predict_before_fit_raises(self):
        with pytest.raises(RuntimeError, match="not been fit"):
            IntradayNowcast().predict(12.0, 20)


def remaining_samples(n_days: int = 400):
    """(hour, forecast remaining max, actual remaining max) with a small bias."""
    rows = []
    for i in range(n_days):
        for hour in range(0, 22):
            forecast = 20.0 + (i % 5)
            actual = int(round(forecast + ((i % 3) - 1) * 0.5))
            rows.append((float(hour), forecast, actual))
    return rows


class TestRemainingMaxNowcast:
    model = RemainingMaxNowcast().fit(remaining_samples())

    def test_rejects_a_thin_sample(self):
        with pytest.raises(ValueError, match="at least 100"):
            RemainingMaxNowcast().fit([(12.0, 20.0, 20)] * 10)

    def test_pmf_is_normalised(self):
        assert sum(self.model.remaining_max_pmf(12.0, 22.0)) == pytest.approx(1.0)

    def test_collapses_onto_the_bound_when_the_forecast_is_below_it(self):
        """If nothing to come beats M, essentially all mass sits on M."""
        assert self.model.predict(15.0, 30, 20.0).probability(30) > 0.95

    def test_follows_the_forecast_when_it_is_above_the_bound(self):
        dist = self.model.predict(9.0, 15, 22.0)
        assert dist.mode() == 22

    def test_no_remaining_forecast_pins_the_answer_to_the_bound(self):
        dist = self.model.predict(23.0, 24, None)
        assert dist.probability(24) > 0.99

    def test_never_puts_real_mass_below_the_bound(self):
        assert self.model.predict(12.0, 25, 27.0).cdf(24) < 0.01

    def test_prediction_is_a_valid_distribution(self):
        assert sum(self.model.predict(12.0, 20, 23.0).probabilities) == pytest.approx(1.0)

    def test_pmf_before_fit_raises(self):
        with pytest.raises(RuntimeError, match="not been fit"):
            RemainingMaxNowcast().remaining_max_pmf(12.0, 20.0)
