"""Model D — the intraday nowcast.

Every model before this one predicts all of Y. This one predicts only what is
left of it. Part-way through the day the running maximum M is not an estimate of
the answer, it is a piece of the answer already in hand:

    Y = M + R,     R >= 0

so the modelling problem collapses from "what will the maximum be" to "how much
higher will it still go", and the second question has a far tighter answer. Late
in the day R is almost always 0 and the distribution is nearly a point mass --
a degree of confidence no forecast model can reach at any lead.

The conditioning variable is the local hour, smoothed with a Gaussian kernel so
neighbouring hours pool rather than each hour being estimated alone. Rise
behaviour varies continuously through the day (nothing special happens between
13:00 and 14:00), so hard hour bins would add variance for no structure.

**On the lower bound.** `Y >= M` is what gives this model its power, and it is
the one assumption that could make it catastrophically overconfident: mass moved
entirely off the buckets below M means a settled value below M scores as
impossible. The bound is not quite hard in practice, because settlement follows
Weather Underground's ingested subset of METARs and WU occasionally drops the
observation carrying a peak, so the settled value can land below the raw running
maximum. `below_bound_floor` is the deliberate concession to that: a small
non-zero mass per bin beneath M, sized to the measured violation rate rather
than chosen for comfort. Setting it to 0 asserts the bound is exact, which the
data does not support.

What this model deliberately does not do: use the forecast for the remaining
hours. It knows how much days like this usually rise from here, not how much
*today* will. `ForecastConditionedNowcast` adds exactly that one thing, so the
value of the forecast can be measured rather than assumed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from weatherbot.models.distribution import (
    DEFAULT_SUPPORT_HIGH,
    DEFAULT_SUPPORT_LOW,
    TemperatureDistribution,
)


@dataclass(frozen=True)
class NowcastConfig:
    """Smoothing and the treatment of the lower bound."""

    # Kernel width in hours. The lock-in curve is very steep through the middle
    # of the day -- P(locked) runs 19.7% at 11:00 to 96.3% at 17:00 -- so a wide
    # kernel does not stabilise the estimate, it destroys the signal. 0.6 keeps
    # the transition sharp; the training set is ~4,500 days x 24 hours, so there
    # is no sample-size reason to smooth harder.
    hour_sigma: float = 0.6

    # TOTAL probability placed below the running maximum, and how it is shared
    # out. Non-zero because the bound is empirical, not logical.
    #
    # Sizing: the bound was violated on 1 of 535 settled markets (0.19%, Wilson
    # 95% upper 1.05%). Expected log loss from a floor e is minimised at e = the
    # true violation rate, and the penalty is asymmetric -- too large costs
    # ~e nats linearly, too small costs infinity the first time it bites. The
    # log-scale midpoint of the estimate and its upper bound is 0.45%, rounded
    # to 0.5%. That costs 0.005 nats on the 99.8% of days the bound holds,
    # against ~1.0-1.5 nats typical for an eleven-bucket market.
    below_bound_mass: float = 0.005

    # Share going to M-1, M-2, and everything below that. Placement matters more
    # than size: spreading the mass uniformly down to the support floor spends
    # almost all of it on temperatures with no historical support. Every
    # observed violation was exactly -1 C, and conditional on the peak
    # observation vanishing the maximum falls by 1 C with ~99.7% probability.
    below_bound_shape: tuple[float, float, float] = (0.90, 0.09, 0.01)

    # Floor on bins at or above the bound, so log loss stays finite.
    floor: float = 1e-6

    def below_bound_weights(self, running_max: int) -> dict[int, float]:
        """Mass for each integer strictly below `running_max`."""
        near, next_, rest = self.below_bound_shape
        out: dict[int, float] = {}
        if running_max - 1 >= self.support_low:
            out[running_max - 1] = self.below_bound_mass * near
        if running_max - 2 >= self.support_low:
            out[running_max - 2] = self.below_bound_mass * next_
        remaining = [k for k in range(self.support_low, running_max - 2)]
        if remaining:
            share = self.below_bound_mass * rest / len(remaining)
            for k in remaining:
                out[k] = share
        return out

    # Rises beyond this are pooled into the top bin. EGLC has never risen
    # anything like this far from a mid-morning running maximum.
    max_rise: int = 20

    support_low: int = DEFAULT_SUPPORT_LOW
    support_high: int = DEFAULT_SUPPORT_HIGH


class IntradayNowcast:
    """Learns P(R = r | local hour), where R is the remaining rise."""

    def __init__(self, config: NowcastConfig | None = None) -> None:
        self.config = config or NowcastConfig()
        self._samples: list[tuple[float, int]] = []

    @property
    def n_training_samples(self) -> int:
        return len(self._samples)

    def fit(self, samples: Sequence[tuple[float, int]]) -> "IntradayNowcast":
        """Fit from (local hour, remaining rise) pairs.

        The caller is responsible for computing the rise as
        `settled - running_max_at_that_hour`, using only observations at or
        before the hour in question.
        """
        if len(samples) < 100:
            raise ValueError(
                f"need at least 100 samples for a stable rise distribution, "
                f"got {len(samples)}"
            )
        bad = [r for _, r in samples if r < 0]
        if bad:
            raise ValueError(
                f"{len(bad)} samples have a negative rise, which means the "
                f"running maximum exceeded the settled value; pass them through "
                f"only if that is intended and clip them first"
            )
        self._samples = [(float(h), int(r)) for h, r in samples]
        return self

    def rise_distribution(self, hour: float) -> list[float]:
        """Kernel-weighted P(R = r) at `hour`, indexed from r = 0."""
        if not self._samples:
            raise RuntimeError("model has not been fit")

        cfg = self.config
        norm = 1.0 / (2.0 * cfg.hour_sigma * cfg.hour_sigma)
        cutoff = 4 * cfg.hour_sigma

        weights = [0.0] * (cfg.max_rise + 1)
        for sample_hour, rise in self._samples:
            delta = sample_hour - hour
            if abs(delta) > cutoff:
                continue
            weights[min(rise, cfg.max_rise)] += math.exp(-(delta**2) * norm)

        total = sum(weights)
        if total <= 0:
            raise ValueError(f"no training samples within {cutoff:.1f}h of {hour:.1f}")
        return [w / total for w in weights]

    def expected_rise(self, hour: float) -> float:
        return sum(r * p for r, p in enumerate(self.rise_distribution(hour)))

    def probability_locked(self, hour: float) -> float:
        """P(the maximum is already set) at `hour`. The lock-in curve."""
        return self.rise_distribution(hour)[0]

    def predict(self, hour: float, running_max: int) -> TemperatureDistribution:
        """Distribution over the settled value given the running maximum."""
        cfg = self.config
        rise = self.rise_distribution(hour)
        below = cfg.below_bound_weights(running_max)
        keep = 1.0 - cfg.below_bound_mass

        weights = []
        for k in range(cfg.support_low, cfg.support_high + 1):
            if k < running_max:
                weights.append(below.get(k, 0.0) + cfg.floor)
            else:
                offset = k - running_max
                mass = rise[offset] if offset <= cfg.max_rise else 0.0
                weights.append(mass * keep + cfg.floor)

        return TemperatureDistribution.from_weights(cfg.support_low, weights)


class ForecastConditionedNowcast:
    """Model D′ — the same, but conditioned on how today compares to forecast.

    Splits the rise distribution on `gap = running_max - forecast_daily_max`,
    rounded to whole degrees and clipped. The intuition is that a day whose
    running maximum has already overshot its forecast has less room left than
    one still well short of it, and the forecast is the only thing available
    that distinguishes them.

    Conditioning costs sample size, so gaps are clipped into a small number of
    buckets and a bucket that ends up too thin falls back to the unconditional
    model rather than being fitted on noise.
    """

    def __init__(
        self,
        config: NowcastConfig | None = None,
        gap_low: int = -3,
        gap_high: int = 2,
        min_per_bucket: int = 150,
    ) -> None:
        self.config = config or NowcastConfig()
        self.gap_low = gap_low
        self.gap_high = gap_high
        self.min_per_bucket = min_per_bucket
        self._by_gap: dict[int, IntradayNowcast] = {}
        self._fallback = IntradayNowcast(self.config)

    def gap_bucket(self, gap: float) -> int:
        """Clip a forecast gap into the modelled range."""
        return max(self.gap_low, min(self.gap_high, int(round(gap))))

    @property
    def fitted_gaps(self) -> list[int]:
        return sorted(self._by_gap)

    def fit(self, samples: Sequence[tuple[float, float, int]]) -> "ForecastConditionedNowcast":
        """Fit from (local hour, forecast gap, remaining rise) triples."""
        self._fallback.fit([(h, r) for h, _, r in samples])

        grouped: dict[int, list[tuple[float, int]]] = {}
        for hour, gap, rise in samples:
            grouped.setdefault(self.gap_bucket(gap), []).append((hour, rise))

        for bucket, rows in grouped.items():
            if len(rows) < self.min_per_bucket:
                continue
            try:
                self._by_gap[bucket] = IntradayNowcast(self.config).fit(rows)
            except ValueError:
                continue
        return self

    def model_for(self, gap: float) -> IntradayNowcast:
        return self._by_gap.get(self.gap_bucket(gap), self._fallback)

    def predict(
        self, hour: float, running_max: int, forecast_max: float
    ) -> TemperatureDistribution:
        return self.model_for(running_max - forecast_max).predict(hour, running_max)


class RemainingMaxNowcast:
    """Model D″ — the nowcast done properly, as a maximum of two things.

    `IntradayNowcast` loses to the market early in the day for a structural
    reason, not a tuning one: it uses only the running maximum and the hour, so
    at 06:00 it knows the overnight low and the *typical* rise from there, while
    the market knows today's forecast. Throwing the forecast away cannot be
    fixed by smoothing it better.

    The correct decomposition keeps both:

        Y = max(M, X)      M = running maximum, a fact
                           X = maximum over the hours still to come, a forecast

    which gives, for a running maximum M,

        P(Y = k) = P(X = k)      for k > M
        P(Y = M) = P(X <= M)     the day peaks no higher than it already has
        P(Y < M) = 0             (softened; see NowcastConfig.below_bound_floor)

    X is predicted MOS-style from the forecast's own maximum over the remaining
    hours, learning the error distribution of that quantity per hour. This is
    the same mechanism as Model B, applied to a shrinking window instead of a
    whole day -- so as the day advances the window narrows, the error
    distribution tightens, and `P(X <= M)` climbs toward 1 on its own. The
    lock-in behaviour falls out of the structure rather than being fitted.
    """

    def __init__(
        self, config: NowcastConfig | None = None, degree_sigma: float = 0.7
    ) -> None:
        self.config = config or NowcastConfig()
        self.degree_sigma = degree_sigma
        self._samples: list[tuple[float, float]] = []

    @property
    def n_training_samples(self) -> int:
        return len(self._samples)

    def fit(self, samples: Sequence[tuple[float, float, int]]) -> "RemainingMaxNowcast":
        """Fit from (local hour, forecast remaining max, actual remaining max)."""
        if len(samples) < 100:
            raise ValueError(f"need at least 100 samples, got {len(samples)}")
        self._samples = [
            (float(hour), float(actual) - float(forecast))
            for hour, forecast, actual in samples
        ]
        return self

    def mean_error(self, hour: float) -> float:
        """Mean (actual - forecast) remaining maximum at `hour`."""
        weighted = self._kernel_weights(hour)
        total = sum(w for _, w in weighted)
        if total <= 0:
            raise ValueError(f"no training samples near hour {hour}")
        return sum(e * w for e, w in weighted) / total

    def _kernel_weights(self, hour: float) -> list[tuple[float, float]]:
        cfg = self.config
        norm = 1.0 / (2.0 * cfg.hour_sigma * cfg.hour_sigma)
        cutoff = 4 * cfg.hour_sigma
        return [
            (error, math.exp(-((sample_hour - hour) ** 2) * norm))
            for sample_hour, error in self._samples
            if abs(sample_hour - hour) <= cutoff
        ]

    def remaining_max_pmf(self, hour: float, forecast_remaining: float) -> list[float]:
        """P(X = k) over the support, indexed from `config.support_low`."""
        if not self._samples:
            raise RuntimeError("model has not been fit")

        weighted = self._kernel_weights(hour)
        if not weighted:
            raise ValueError(f"no training samples near hour {hour}")

        cfg = self.config
        norm = 1.0 / (2.0 * self.degree_sigma * self.degree_sigma)
        cutoff = 6 * self.degree_sigma

        weights = []
        for k in range(cfg.support_low, cfg.support_high + 1):
            target = k - forecast_remaining
            total = 0.0
            for error, hour_weight in weighted:
                delta = target - error
                if abs(delta) <= cutoff:
                    total += hour_weight * math.exp(-(delta**2) * norm)
            weights.append(total)

        grand = sum(weights)
        if grand <= 0:
            raise ValueError("no probability mass; forecast far outside training range")
        return [w / grand for w in weights]

    def predict(
        self,
        hour: float,
        running_max: int,
        forecast_remaining: float | None,
    ) -> TemperatureDistribution:
        """Distribution over Y = max(M, X).

        `forecast_remaining` of None means no forecast hours are left, so
        nothing can still exceed the running maximum and Y is pinned to it
        except for the softening floor.
        """
        cfg = self.config

        below = cfg.below_bound_weights(running_max)
        keep = 1.0 - cfg.below_bound_mass

        if forecast_remaining is None:
            weights = [
                below.get(k, 0.0) + cfg.floor
                if k < running_max
                else (keep if k == running_max else 0.0) + cfg.floor
                for k in range(cfg.support_low, cfg.support_high + 1)
            ]
            return TemperatureDistribution.from_weights(cfg.support_low, weights)

        pmf = self.remaining_max_pmf(hour, forecast_remaining)
        index_of = lambda k: k - cfg.support_low  # noqa: E731

        weights = []
        for k in range(cfg.support_low, cfg.support_high + 1):
            if k < running_max:
                weights.append(below.get(k, 0.0) + cfg.floor)
            elif k == running_max:
                # Everything at or below the bound collapses onto it.
                weights.append(sum(pmf[: index_of(k) + 1]) * keep + cfg.floor)
            else:
                weights.append(pmf[index_of(k)] * keep + cfg.floor)

        return TemperatureDistribution.from_weights(cfg.support_low, weights)
