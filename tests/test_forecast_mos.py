"""Tests for the dataset assembly and Model B."""

from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from weatherbot.dataset import (  # noqa: E402
    build_training_pairs,
    daily_completeness,
    daily_maxima_from_hourly,
    lead_variable,
)
from weatherbot.models.forecast_mos import (  # noqa: E402
    ForecastMOS,
    LeadIndexedMOS,
    MOSConfig,
)


class TestLeadVariable:
    def test_lead_zero_is_the_base_series(self):
        assert lead_variable(0) == "temperature_2m"

    def test_positive_leads(self):
        assert lead_variable(3) == "temperature_2m_previous_day3"

    def test_negative_lead_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            lead_variable(-1)


class TestDailyReduction:
    hourly = {
        "2026-07-30T00:00": 18.0,
        "2026-07-30T14:00": 27.5,
        "2026-07-30T23:00": 20.0,
        "2026-07-31T13:00": 24.0,
    }

    def test_takes_the_daily_maximum(self):
        maxima = daily_maxima_from_hourly(self.hourly)
        assert maxima[date(2026, 7, 30)] == 27.5
        assert maxima[date(2026, 7, 31)] == 24.0

    def test_counts_hours_per_day(self):
        counts = daily_completeness(self.hourly)
        assert counts[date(2026, 7, 30)] == 3
        assert counts[date(2026, 7, 31)] == 1

    def test_empty_input(self):
        assert daily_maxima_from_hourly({}) == {}


class TestBuildTrainingPairs:
    forecasts = {date(2026, 1, d): 10.0 + d for d in range(1, 6)}
    observed = {date(2026, 1, d): 10 + d for d in range(1, 6)}

    def test_aligns_on_date(self):
        pairs = build_training_pairs(self.forecasts, self.observed)
        assert len(pairs) == 5
        assert pairs[0] == (11.0, 11)

    def test_end_is_exclusive_and_blocks_leakage(self):
        pairs = build_training_pairs(
            self.forecasts, self.observed, end=date(2026, 1, 3)
        )
        assert len(pairs) == 2

    def test_start_is_inclusive(self):
        pairs = build_training_pairs(
            self.forecasts, self.observed, start=date(2026, 1, 4)
        )
        assert len(pairs) == 2

    def test_unmatched_days_are_dropped(self):
        pairs = build_training_pairs(self.forecasts, {date(2026, 1, 1): 11})
        assert pairs == [(11.0, 11)]


def biased_pairs(n: int = 200, bias: float = 0.5, spread: float = 1.2):
    """Forecasts that run `bias` degrees cold, with deterministic scatter."""
    import math

    out = []
    for i in range(n):
        forecast = 15.0 + 8.0 * math.sin(i / 9.0)
        noise = spread * math.sin(i * 2.7)
        out.append((forecast, int(round(forecast + bias + noise))))
    return out


class TestForecastMOS:
    def test_requires_enough_pairs(self):
        with pytest.raises(ValueError, match="at least 30"):
            ForecastMOS().fit(biased_pairs(10))

    def test_recovers_a_known_cold_bias(self):
        model = ForecastMOS().fit(biased_pairs(bias=0.5))
        assert model.mean_error == pytest.approx(0.5, abs=0.25)

    def test_reports_error_spread(self):
        tight = ForecastMOS().fit(biased_pairs(spread=0.5))
        wide = ForecastMOS().fit(biased_pairs(spread=3.0))
        assert wide.error_stdev > tight.error_stdev

    def test_prediction_is_a_valid_distribution(self):
        model = ForecastMOS().fit(biased_pairs())
        dist = model.predict(22.0)
        assert sum(dist.probabilities) == pytest.approx(1.0)
        assert all(p > 0 for p in dist.probabilities)

    def test_distribution_centres_near_forecast_plus_bias(self):
        model = ForecastMOS().fit(biased_pairs(bias=0.5, spread=0.6))
        assert model.predict(22.0).mode() in (22, 23)

    def test_prediction_shifts_with_the_forecast(self):
        model = ForecastMOS().fit(biased_pairs())
        assert model.predict(28.0).mean() > model.predict(18.0).mean() + 8

    def test_wider_errors_give_a_wider_distribution(self):
        tight = ForecastMOS().fit(biased_pairs(spread=0.4))
        wide = ForecastMOS().fit(biased_pairs(spread=3.5))
        span = lambda m: m.predict(20.0).quantile(0.9) - m.predict(20.0).quantile(0.1)
        assert span(wide) > span(tight)

    def test_predict_before_fit_raises(self):
        with pytest.raises(RuntimeError, match="not been fit"):
            ForecastMOS().predict(20.0)

    def test_mean_error_before_fit_raises(self):
        with pytest.raises(RuntimeError, match="not been fit"):
            _ = ForecastMOS().mean_error


class TestLeadIndexedMOS:
    def test_fits_and_predicts_per_lead(self):
        model = LeadIndexedMOS()
        model.fit_lead(1, biased_pairs(spread=0.8))
        model.fit_lead(5, biased_pairs(spread=3.0))
        assert model.leads == [1, 5]

        narrow = model.predict(1, 20.0)
        wide = model.predict(5, 20.0)
        span = lambda d: d.quantile(0.9) - d.quantile(0.1)
        assert span(wide) > span(narrow), "longer lead must be less confident"

    def test_unknown_lead_raises(self):
        model = LeadIndexedMOS().fit_lead(1, biased_pairs())
        with pytest.raises(KeyError, match="no model fitted for lead 4"):
            model.predict(4, 20.0)

    def test_config_is_propagated(self):
        config = MOSConfig(kernel_sigma=1.5)
        model = LeadIndexedMOS(config).fit_lead(1, biased_pairs())
        assert model.model_for(1).config.kernel_sigma == 1.5
