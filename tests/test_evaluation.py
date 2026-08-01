"""Tests for the scoring metrics.

The property that matters most is that RPS is sensitive to *ordering* — a
one-bucket miss must cost less than a five-bucket miss. Log loss cannot see
that, which is why RPS is the primary metric.
"""

from __future__ import annotations

import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from weatherbot.evaluation import (  # noqa: E402
    brier_score,
    log_loss,
    ranked_probability_score,
    reliability,
    skill_score,
    summarise,
    uniform_forecast,
)


class TestRankedProbabilityScore:
    def test_perfect_forecast_scores_zero(self):
        assert ranked_probability_score([1.0, 0.0, 0.0], 0) == 0.0
        assert ranked_probability_score([0.0, 0.0, 1.0], 2) == 0.0

    def test_worst_forecast_scores_one_when_normalised(self):
        assert ranked_probability_score([0.0, 0.0, 1.0], 0) == pytest.approx(1.0)

    def test_penalises_distance_along_the_ordering(self):
        """The whole reason RPS is the primary metric."""
        near = ranked_probability_score([0.0, 1.0, 0.0, 0.0, 0.0], 0)
        far = ranked_probability_score([0.0, 0.0, 0.0, 0.0, 1.0], 0)
        assert near < far

    def test_log_loss_cannot_distinguish_near_from_far(self):
        """Contrast with the above — this is why log loss alone is unsafe."""
        near = log_loss([0.0, 1.0, 0.0, 0.0, 0.0], 0)
        far = log_loss([0.0, 0.0, 0.0, 0.0, 1.0], 0)
        assert near == far

    def test_normalisation_makes_bucket_counts_comparable(self):
        """Bucket counts vary by era (7, 9, 11), so raw RPS is not comparable."""
        worst_3 = ranked_probability_score([0.0, 0.0, 1.0], 0)
        worst_11 = ranked_probability_score([0.0] * 10 + [1.0], 0)
        assert worst_3 == pytest.approx(worst_11)

        raw_3 = ranked_probability_score([0.0, 0.0, 1.0], 0, normalise=False)
        raw_11 = ranked_probability_score([0.0] * 10 + [1.0], 0, normalise=False)
        assert raw_11 > raw_3

    def test_single_bucket_is_trivially_perfect(self):
        assert ranked_probability_score([1.0], 0) == 0.0

    def test_uniform_is_between_perfect_and_worst(self):
        uniform = ranked_probability_score(uniform_forecast(5), 2)
        assert 0.0 < uniform < 1.0

    def test_rejects_unnormalised_input(self):
        with pytest.raises(ValueError, match="sum to 1"):
            ranked_probability_score([0.5, 0.2], 0)

    def test_rejects_out_of_range_index(self):
        with pytest.raises(ValueError, match="outside"):
            ranked_probability_score([0.5, 0.5], 5)

    def test_rejects_negative_probability(self):
        with pytest.raises(ValueError):
            ranked_probability_score([1.5, -0.5], 0)


class TestLogLoss:
    def test_certain_and_correct_is_zero(self):
        assert log_loss([1.0, 0.0], 0) == pytest.approx(0.0)

    def test_zero_probability_is_finite_not_infinite(self):
        """A zero bin must not produce inf, or one bad day destroys the mean."""
        assert math.isfinite(log_loss([1.0, 0.0], 1))

    def test_uniform_equals_log_k(self):
        assert log_loss(uniform_forecast(4), 2) == pytest.approx(math.log(4))


class TestBrier:
    def test_perfect_is_zero(self):
        assert brier_score([1.0, 0.0, 0.0], 0) == pytest.approx(0.0)

    def test_worst_is_two(self):
        assert brier_score([0.0, 1.0], 0) == pytest.approx(2.0)


class TestSkillScore:
    def test_equal_scores_give_zero_skill(self):
        assert skill_score(0.2, 0.2) == pytest.approx(0.0)

    def test_better_than_reference_is_positive(self):
        assert skill_score(0.1, 0.2) == pytest.approx(0.5)

    def test_worse_than_reference_is_negative(self):
        assert skill_score(0.4, 0.2) == pytest.approx(-1.0)

    def test_rejects_non_positive_reference(self):
        with pytest.raises(ValueError):
            skill_score(0.1, 0.0)


class TestSummarise:
    def test_averages_across_forecasts(self):
        s = summarise([[1.0, 0.0], [0.0, 1.0]], [0, 1])
        assert s.n == 2
        assert s.rps == pytest.approx(0.0)

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(ValueError, match="forecasts"):
            summarise([[1.0, 0.0]], [0, 1])

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="nothing to score"):
            summarise([], [])

    def test_skill_against_reference(self):
        good = summarise([[0.9, 0.1]], [0])
        poor = summarise([[0.5, 0.5]], [0])
        assert good.skill_against(poor)["rps_skill"] > 0


class TestReliability:
    def test_perfectly_calibrated_model_has_small_gaps(self):
        # Half the cases land in bucket 0, half in bucket 1, always forecast 50/50.
        forecasts = [[0.5, 0.5]] * 100
        outcomes = [0] * 50 + [1] * 50
        bins = [b for b in reliability(forecasts, outcomes) if b.count]
        assert all(abs(b.gap) < 0.01 for b in bins)

    def test_overconfident_model_shows_positive_gap(self):
        forecasts = [[0.9, 0.1]] * 100
        outcomes = [0] * 50 + [1] * 50  # only right half the time
        top = [b for b in reliability(forecasts, outcomes) if b.count and b.lower >= 0.8]
        assert top and top[0].gap > 0.3

    def test_counts_cover_every_probability(self):
        forecasts = [[0.3, 0.3, 0.4]] * 10
        bins = reliability(forecasts, [0] * 10)
        assert sum(b.count for b in bins) == 30


def test_uniform_forecast_sums_to_one():
    for k in (1, 2, 7, 11):
        assert sum(uniform_forecast(k)) == pytest.approx(1.0)
        assert len(uniform_forecast(k)) == k
