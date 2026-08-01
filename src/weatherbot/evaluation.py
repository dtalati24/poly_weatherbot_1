"""Scoring for probabilistic forecasts over ordered temperature buckets.

**Ranked Probability Score is the primary metric, not log loss.** These buckets
are ordered: predicting 27C when the answer is 28C is a near miss, predicting
22C is a disaster. Log loss cannot tell those apart because it only looks at the
probability assigned to the one true bucket. RPS penalises by distance along the
ordering, which is what a trader actually cares about -- a one-bucket error
costs a little, a five-bucket error costs a lot.

**RPS is normalised by (K-1) by default, and that is not cosmetic.** Bucket
counts vary across market eras (7, 9 and 11 outcomes all appear in the settled
history). Raw RPS grows with K, so comparing an unnormalised score from a
7-bucket market against an 11-bucket one would be meaningless.

Skill scores are always reported against climatology, never against uniform.
"Beats uniform" says nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

EPSILON = 1e-15


def _validate(probabilities: Sequence[float], observed_index: int) -> None:
    if not probabilities:
        raise ValueError("probabilities must be non-empty")
    if not 0 <= observed_index < len(probabilities):
        raise ValueError(
            f"observed_index {observed_index} outside 0..{len(probabilities) - 1}"
        )
    total = sum(probabilities)
    if not math.isclose(total, 1.0, abs_tol=1e-6):
        raise ValueError(f"probabilities must sum to 1, got {total}")
    if any(p < 0 for p in probabilities):
        raise ValueError("probabilities must be non-negative")


def ranked_probability_score(
    probabilities: Sequence[float],
    observed_index: int,
    *,
    normalise: bool = True,
) -> float:
    """RPS for one forecast. Lower is better; 0 is perfect.

    RPS = sum_k (F_k - O_k)^2 over k = 0..K-2, where F is the forecast CDF and
    O is the observed step function. Normalised by (K-1) so scores from markets
    with different bucket counts are comparable.
    """
    _validate(probabilities, observed_index)
    n = len(probabilities)
    if n == 1:
        return 0.0

    score = 0.0
    forecast_cdf = 0.0
    for k in range(n - 1):
        forecast_cdf += probabilities[k]
        observed_cdf = 1.0 if k >= observed_index else 0.0
        score += (forecast_cdf - observed_cdf) ** 2

    return score / (n - 1) if normalise else score


def log_loss(probabilities: Sequence[float], observed_index: int) -> float:
    """Negative log probability of the realised bucket. Lower is better."""
    _validate(probabilities, observed_index)
    return -math.log(max(probabilities[observed_index], EPSILON))


def brier_score(probabilities: Sequence[float], observed_index: int) -> float:
    """Multi-category Brier score. Ignores ordering; reported for reference."""
    _validate(probabilities, observed_index)
    return sum(
        (p - (1.0 if i == observed_index else 0.0)) ** 2
        for i, p in enumerate(probabilities)
    )


def skill_score(score: float, reference: float) -> float:
    """Fractional improvement over a reference. 1 is perfect, 0 is no better.

    Negative means the model is worse than the reference, which for a model
    scored against climatology means it has learned nothing useful.
    """
    if reference <= 0:
        raise ValueError("reference score must be positive")
    return 1.0 - score / reference


@dataclass
class ScoreSummary:
    """Aggregate scores over a set of forecasts."""

    n: int
    rps: float
    log_loss: float
    brier: float

    def skill_against(self, reference: "ScoreSummary") -> dict[str, float]:
        return {
            "rps_skill": skill_score(self.rps, reference.rps),
            "log_loss_skill": skill_score(self.log_loss, reference.log_loss),
            "brier_skill": skill_score(self.brier, reference.brier),
        }

    def __str__(self) -> str:
        return (
            f"n={self.n}  RPS={self.rps:.5f}  "
            f"logloss={self.log_loss:.4f}  brier={self.brier:.4f}"
        )


def summarise(
    forecasts: Sequence[Sequence[float]],
    observed_indices: Sequence[int],
    *,
    normalise_rps: bool = True,
) -> ScoreSummary:
    """Mean scores across a set of forecasts."""
    if len(forecasts) != len(observed_indices):
        raise ValueError(
            f"got {len(forecasts)} forecasts and {len(observed_indices)} outcomes"
        )
    if not forecasts:
        raise ValueError("nothing to score")

    n = len(forecasts)
    rps = sum(
        ranked_probability_score(p, i, normalise=normalise_rps)
        for p, i in zip(forecasts, observed_indices)
    ) / n
    ll = sum(log_loss(p, i) for p, i in zip(forecasts, observed_indices)) / n
    brier = sum(brier_score(p, i) for p, i in zip(forecasts, observed_indices)) / n
    return ScoreSummary(n=n, rps=rps, log_loss=ll, brier=brier)


def uniform_forecast(n_buckets: int) -> tuple[float, ...]:
    """Uninformative baseline. A floor, not a benchmark."""
    if n_buckets < 1:
        raise ValueError("need at least one bucket")
    return tuple([1.0 / n_buckets] * n_buckets)


@dataclass
class ReliabilityBin:
    """One bin of a reliability diagram."""

    lower: float
    upper: float
    count: int
    mean_forecast: float
    observed_frequency: float

    @property
    def gap(self) -> float:
        """Positive means overconfident: forecast exceeded reality."""
        return self.mean_forecast - self.observed_frequency


def reliability(
    forecasts: Sequence[Sequence[float]],
    observed_indices: Sequence[int],
    *,
    n_bins: int = 10,
) -> list[ReliabilityBin]:
    """Reliability of the individual bucket probabilities.

    Every (bucket, probability) pair is pooled and binned by forecast
    probability; within each bin the realised frequency is compared against the
    mean forecast. A calibrated model sits on the diagonal. This is what catches
    a model that is systematically overconfident at 5% -- exactly the region
    where these markets pay out and where miscalibration is most expensive.
    """
    if n_bins < 1:
        raise ValueError("need at least one bin")

    edges = [i / n_bins for i in range(n_bins + 1)]
    sums = [0.0] * n_bins
    hits = [0] * n_bins
    counts = [0] * n_bins

    for probabilities, observed in zip(forecasts, observed_indices):
        for index, probability in enumerate(probabilities):
            bin_index = min(int(probability * n_bins), n_bins - 1)
            counts[bin_index] += 1
            sums[bin_index] += probability
            if index == observed:
                hits[bin_index] += 1

    return [
        ReliabilityBin(
            lower=edges[b],
            upper=edges[b + 1],
            count=counts[b],
            mean_forecast=sums[b] / counts[b] if counts[b] else 0.0,
            observed_frequency=hits[b] / counts[b] if counts[b] else 0.0,
        )
        for b in range(n_bins)
    ]
