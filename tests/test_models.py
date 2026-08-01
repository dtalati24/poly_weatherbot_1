"""Tests for the distribution type and the two baseline models."""

from __future__ import annotations

import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from weatherbot.models.climatology import (  # noqa: E402
    ClimatologyConfig,
    ClimatologyModel,
    circular_day_distance,
)
from weatherbot.models.distribution import TemperatureDistribution  # noqa: E402
from weatherbot.models.positional import PositionalClimatology  # noqa: E402
from weatherbot.sources.polymarket import parse_bucket  # noqa: E402


class TestTemperatureDistribution:
    def test_rejects_unnormalised(self):
        with pytest.raises(ValueError, match="sum to 1"):
            TemperatureDistribution(low=0, probabilities=(0.5, 0.2))

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            TemperatureDistribution(low=0, probabilities=(1.5, -0.5))

    def test_probability_outside_support_is_zero(self):
        d = TemperatureDistribution(low=10, probabilities=(0.5, 0.5))
        assert d.probability(9) == 0.0
        assert d.probability(12) == 0.0
        assert d.probability(10) == 0.5

    def test_cdf_is_monotonic_and_bounded(self):
        d = TemperatureDistribution(low=0, probabilities=(0.2, 0.3, 0.5))
        assert d.cdf(-1) == 0.0
        assert d.cdf(0) == pytest.approx(0.2)
        assert d.cdf(1) == pytest.approx(0.5)
        assert d.cdf(2) == 1.0
        assert d.cdf(99) == 1.0

    def test_quantile_and_mode_and_mean(self):
        d = TemperatureDistribution(low=10, probabilities=(0.1, 0.8, 0.1))
        assert d.quantile(0.5) == 11
        assert d.mode() == 11
        assert d.mean() == pytest.approx(11.0)

    def test_from_weights_floor_removes_zeros(self):
        d = TemperatureDistribution.from_weights(0, [1.0, 0.0, 0.0], floor=1e-6)
        assert all(p > 0 for p in d.probabilities)

    def test_from_weights_rejects_all_zero(self):
        with pytest.raises(ValueError, match="sum to zero"):
            TemperatureDistribution.from_weights(0, [0.0, 0.0])

    def test_point_mass(self):
        d = TemperatureDistribution.point_mass(25)
        assert d.probability(25) == 1.0


class TestToBuckets:
    def _celsius_buckets(self):
        return [
            parse_bucket("23°C or below"),
            *[parse_bucket(f"{c}°C") for c in range(24, 33)],
            parse_bucket("33°C or higher"),
        ]

    def test_celsius_buckets_capture_all_mass(self):
        d = TemperatureDistribution.from_weights(-15, [1.0] * 61)
        probs = d.to_buckets(self._celsius_buckets())
        assert len(probs) == 11
        assert sum(probs) == pytest.approx(1.0)

    def test_tail_buckets_absorb_the_extremes(self):
        d = TemperatureDistribution.point_mass(-5)
        probs = d.to_buckets(self._celsius_buckets())
        assert probs[0] == pytest.approx(1.0)

    def test_fahrenheit_buckets_convert_correctly(self):
        # 18C -> 64.4F -> 64F, which sits inside 64-65F.
        buckets = [
            parse_bucket("63°F or below"),
            parse_bucket("64–65°F"),
            parse_bucket("66°F or higher"),
        ]
        probs = TemperatureDistribution.point_mass(18).to_buckets(buckets)
        assert probs[1] == pytest.approx(1.0)

    def test_non_exhaustive_buckets_raise_rather_than_silently_drop(self):
        """Dropping mass would corrupt every downstream metric."""
        d = TemperatureDistribution.from_weights(0, [1.0] * 30)
        with pytest.raises(ValueError, match="not exhaustive"):
            d.to_buckets([parse_bucket("5°C"), parse_bucket("6°C")])


def synthetic_history(
    start_year: int, end_year: int, trend_per_year: float = 0.0
) -> dict[date, int]:
    """Seasonal sinusoid plus optional linear trend, rounded to integers."""
    import math

    out: dict[date, int] = {}
    for year in range(start_year, end_year):
        day = date(year, 1, 1)
        while day.year == year:
            doy = (day - date(year, 1, 1)).days
            seasonal = 14 + 9 * math.sin(2 * math.pi * (doy - 100) / 365.0)
            wobble = 2.0 * math.sin(doy * 7.3)
            value = seasonal + wobble + trend_per_year * (year - start_year)
            out[day] = int(round(value))
            day += timedelta(days=1)
    return out


class TestClimatology:
    def test_requires_a_year_of_data(self):
        with pytest.raises(ValueError, match="at least a year"):
            ClimatologyModel().fit({date(2020, 1, 1): 10})

    def test_recovers_a_known_trend(self):
        history = synthetic_history(2008, 2024, trend_per_year=0.1)
        model = ClimatologyModel().fit(history)
        assert model.trend_c_per_decade == pytest.approx(1.0, abs=0.15)

    def test_no_trend_in_stationary_data(self):
        model = ClimatologyModel().fit(synthetic_history(2008, 2024))
        assert abs(model.trend_c_per_decade) < 0.2

    def test_trend_can_be_disabled(self):
        model = ClimatologyModel(ClimatologyConfig(apply_trend=False)).fit(
            synthetic_history(2008, 2024, trend_per_year=0.5)
        )
        assert model.trend_c_per_year == 0.0

    def test_summer_is_warmer_than_winter(self):
        model = ClimatologyModel().fit(synthetic_history(2008, 2024))
        july = model.predict(date(2024, 7, 15)).mean()
        january = model.predict(date(2024, 1, 15)).mean()
        assert july > january + 5

    def test_predict_before_fit_raises(self):
        with pytest.raises(RuntimeError, match="not been fit"):
            ClimatologyModel().predict(date(2024, 1, 1))

    def test_end_bound_excludes_evaluation_data(self):
        """Fitting on days you later score against is leakage."""
        history = synthetic_history(2008, 2024)
        model = ClimatologyModel().fit(history, end=date(2020, 1, 1))
        assert model.n_training_days == sum(1 for d in history if d < date(2020, 1, 1))

    def test_output_has_no_zero_bins(self):
        model = ClimatologyModel().fit(synthetic_history(2008, 2024))
        assert all(p > 0 for p in model.predict(date(2024, 6, 1)).probabilities)


class TestCircularDayDistance:
    def test_same_day_is_zero(self):
        assert circular_day_distance(date(2024, 3, 1), date(2019, 3, 1)) < 1.0

    def test_wraps_around_new_year(self):
        assert circular_day_distance(date(2024, 12, 31), date(2024, 1, 1)) < 3.0

    def test_opposite_season_is_about_half_a_year(self):
        d = circular_day_distance(date(2024, 1, 1), date(2024, 7, 2))
        assert 175 < d < 190


class TestPositionalClimatology:
    def _centred_training(self, n: int = 300):
        """Outcomes concentrated near the middle of a 7-bucket window."""
        pattern = [2, 3, 3, 3, 4, 4, 2, 5, 3, 4]
        return [(pattern[i % len(pattern)], 7) for i in range(n)]

    def test_peaks_near_the_centre(self):
        model = PositionalClimatology().fit(self._centred_training())
        probs = model.predict(7)
        assert probs.index(max(probs)) in (3, 4)

    def test_beats_uniform_at_the_centre(self):
        model = PositionalClimatology().fit(self._centred_training())
        assert model.predict(7)[3] > 1 / 7

    def test_generalises_across_bucket_counts(self):
        """7-bucket training must still produce sane 11-bucket forecasts."""
        model = PositionalClimatology().fit(self._centred_training())
        probs = model.predict(11)
        assert len(probs) == 11
        assert sum(probs) == pytest.approx(1.0)
        assert probs.index(max(probs)) in (4, 5, 6)

    def test_no_zero_probabilities(self):
        model = PositionalClimatology().fit(self._centred_training())
        assert all(p > 0 for p in model.predict(11))

    def test_single_bucket_is_certain(self):
        model = PositionalClimatology().fit(self._centred_training())
        assert model.predict(1) == (1.0,)

    def test_predict_before_fit_raises(self):
        with pytest.raises(RuntimeError, match="not been fit"):
            PositionalClimatology().predict(7)

    def test_rejects_index_outside_range(self):
        with pytest.raises(ValueError, match="outside"):
            PositionalClimatology().fit([(9, 7)])

    def test_single_bucket_markets_are_ignored(self):
        with pytest.raises(ValueError, match="no usable"):
            PositionalClimatology().fit([(0, 1), (0, 1)])

    def test_all_sizes_sum_to_one(self):
        model = PositionalClimatology().fit(self._centred_training())
        for k in (2, 7, 9, 11):
            assert sum(model.predict(k)) == pytest.approx(1.0)
