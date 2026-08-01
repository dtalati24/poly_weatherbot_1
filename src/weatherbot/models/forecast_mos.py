"""Model B — forecast MOS. The first model that actually looks at the weather.

Takes a numerical forecast of the daily maximum and turns it into a calibrated
probability distribution over integer degrees Celsius, by learning the
historical distribution of forecast error at that lead time:

    Y = forecast + e,    e ~ empirical error distribution for this lead

Deliberately simple, and the simplicity is the point. It learns **one thing**:
how wrong this model's daily maximum tends to be, and in which direction. That
single mechanism absorbs several effects at once, which is exactly the C2
architecture in PLAN.md:

  - **Grid-vs-station bias.** Open-Meteo's grid point is not EGLC. Whatever
    systematic offset that produces is folded into the mean error.
  - **The settlement operator.** The target is a maximum over discrete
    half-hourly integer observations, whereas the forecast is a maximum over
    smooth hourly values. That mismatch is a fixed distortion, and it is
    learned rather than derived.
  - **Lead-dependent spread.** Error variance grows with lead time, so a
    separate error distribution is fitted per lead.

What it does NOT do: use ensemble spread, condition on the synoptic situation,
or exploit that the market's bucket window is itself a forecast. Those are
Models C and D. Model B exists to answer one question — does using a forecast
at all beat the structural baseline?
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

from weatherbot.models.distribution import (
    DEFAULT_SUPPORT_HIGH,
    DEFAULT_SUPPORT_LOW,
    TemperatureDistribution,
)


@dataclass(frozen=True)
class MOSConfig:
    """Smoothing for the empirical error distribution."""

    # Kernel width in degrees when spreading errors onto the integer lattice.
    # Errors are continuous, the target is integer, so some smoothing is
    # required; 0.7 C keeps the shape without over-flattening it.
    kernel_sigma: float = 0.7
    floor: float = 1e-6
    support_low: int = DEFAULT_SUPPORT_LOW
    support_high: int = DEFAULT_SUPPORT_HIGH


class ForecastMOS:
    """Maps a forecast daily maximum to a distribution over the settled value."""

    def __init__(self, config: MOSConfig | None = None) -> None:
        self.config = config or MOSConfig()
        self._errors: list[float] = []

    @property
    def n_training_pairs(self) -> int:
        return len(self._errors)

    @property
    def mean_error(self) -> float:
        """Positive means the forecast runs cold relative to settlement."""
        if not self._errors:
            raise RuntimeError("model has not been fit")
        return sum(self._errors) / len(self._errors)

    @property
    def error_stdev(self) -> float:
        if len(self._errors) < 2:
            raise RuntimeError("need at least two training pairs")
        mean = self.mean_error
        variance = sum((e - mean) ** 2 for e in self._errors) / (len(self._errors) - 1)
        return math.sqrt(variance)

    def fit(self, pairs: Sequence[tuple[float, int]]) -> "ForecastMOS":
        """Fit from (forecast daily max, observed settled value) pairs."""
        if len(pairs) < 30:
            raise ValueError(
                f"need at least 30 training pairs for a stable error "
                f"distribution, got {len(pairs)}"
            )
        self._errors = [float(observed) - float(forecast) for forecast, observed in pairs]
        return self

    def predict(self, forecast_max: float) -> TemperatureDistribution:
        """Distribution over the settled value given a forecast daily maximum."""
        if not self._errors:
            raise RuntimeError("model has not been fit")

        cfg = self.config
        sigma = cfg.kernel_sigma
        norm = 1.0 / (2.0 * sigma * sigma)
        cutoff = 6 * sigma

        weights = []
        for k in range(cfg.support_low, cfg.support_high + 1):
            target_error = k - forecast_max
            total = 0.0
            for error in self._errors:
                delta = target_error - error
                if abs(delta) <= cutoff:
                    total += math.exp(-(delta**2) * norm)
            weights.append(total)

        return TemperatureDistribution.from_weights(
            cfg.support_low, weights, floor=cfg.floor
        )


class LeadIndexedMOS:
    """One ForecastMOS per lead time.

    Error spread grows with lead, so a single pooled distribution would be too
    wide at short leads and too narrow at long ones — miscalibrated in both
    directions at once.
    """

    def __init__(self, config: MOSConfig | None = None) -> None:
        self.config = config or MOSConfig()
        self._models: dict[int, ForecastMOS] = {}

    @property
    def leads(self) -> list[int]:
        return sorted(self._models)

    def fit_lead(self, lead: int, pairs: Sequence[tuple[float, int]]) -> "LeadIndexedMOS":
        self._models[lead] = ForecastMOS(self.config).fit(pairs)
        return self

    def model_for(self, lead: int) -> ForecastMOS:
        if lead not in self._models:
            raise KeyError(f"no model fitted for lead {lead}; have {self.leads}")
        return self._models[lead]

    def predict(self, lead: int, forecast_max: float) -> TemperatureDistribution:
        return self.model_for(lead).predict(forecast_max)


class RollingMOS:
    """Model B', refitting the error distribution on a trailing window.

    Model B fits one error distribution over all available history and reports
    a bias of about +0.5 C, stable across leads. The stability across *lead* is
    real; the implied stability across *time* is not, and taking it for a fixed
    grid-vs-station offset was wrong. Mean (observed - forecast) at lead 1 for
    ECMWF in July:

        July 2024   +0.968
        July 2025   +0.165
        July 2026   -0.490

    A swing of nearly 1.5 C -- and it is directional, not noise. So an
    all-history fit is not estimating a constant, it is averaging a moving
    quantity and reporting a number that describes no particular year. Applied
    to July 2026 it corrects by +0.5 C when the truth wants -0.5 C, an error of
    about one full market bucket.

    This refits on the `window_days` immediately before the day being predicted,
    so the correction tracks the current regime. The cost is variance: a shorter
    window is more current and noisier. `window_days` is the whole trade-off and
    should be chosen by held-out score, not by taste.

    The window is strictly *before* the target day, so this cannot see the day
    it is predicting.
    """

    def __init__(
        self,
        window_days: int = 180,
        config: MOSConfig | None = None,
        min_pairs: int = 45,
    ) -> None:
        self.window_days = window_days
        self.config = config or MOSConfig()
        self.min_pairs = min_pairs
        self._history: list[tuple[date, float, int]] = []
        self._cache: dict[date, ForecastMOS] = {}

    def fit_history(
        self, dated_pairs: Sequence[tuple[date, float, int]]
    ) -> "RollingMOS":
        """Supply (day, forecast daily max, observed settled value) triples."""
        self._history = sorted(dated_pairs)
        self._cache.clear()
        return self

    def model_as_of(self, as_of: date) -> ForecastMOS | None:
        """Fit on the trailing window ending the day before `as_of`.

        Returns None when the window is too thin to fit, which the caller must
        handle -- silently widening the window would reintroduce exactly the
        stale-regime problem this class exists to avoid.
        """
        if as_of in self._cache:
            return self._cache[as_of]

        start = as_of - timedelta(days=self.window_days)
        pairs = [
            (forecast, observed)
            for day, forecast, observed in self._history
            if start <= day < as_of
        ]
        if len(pairs) < self.min_pairs:
            return None

        model = ForecastMOS(self.config)
        # ForecastMOS enforces its own 30-pair floor; min_pairs may be higher.
        model.fit(pairs)
        self._cache[as_of] = model
        return model

    def predict(
        self, as_of: date, forecast_max: float
    ) -> TemperatureDistribution | None:
        model = self.model_as_of(as_of)
        return None if model is None else model.predict(forecast_max)
