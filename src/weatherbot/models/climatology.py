"""Model A — climatology. The benchmark every later model must beat.

This is deliberately the simplest thing that is not stupid: what does the
distribution of daily maximum temperature at EGLC look like at this time of
year, adjusted for warming? It uses no forecast information at all.

Two reasons it earns its place rather than being a throwaway:

  1. **It is the honest benchmark.** "Beats uniform" is meaningless. A model
     that cannot beat climatology has learned nothing about the weather.
  2. **It is genuinely tradeable** on far-dated markets, where NWP has no skill,
     and it is the fallback when a live feed dies.

Homogeneity: the EGLC record contains two discontinuities, and training starts
after the later one.

**2007 -- coverage, not cadence (verified).** The reporting cadence has been
:20/:50 for the entire archive; 0.000% of 354,009 observations fall on any
other minute, in every era. What changed is *coverage hours*: 2005-2006 report
only ~04:00-21:00 UTC (~26 obs/day), and full 24-hour reporting begins in late
2007 (~48/day). This still matters, because the settlement variable is a
maximum over samples: restricting modern data to 05:00-20:00 UTC lowers the
observed daily maximum by 0.081 C on average, and the true maximum falls
outside that window on 10.15% of days -- overwhelmingly the just-after-local-
midnight case.

**2013 -- an undocumented instrument change (corroborated, not independently
reproduced).** External analysis differencing EGLC against Heathrow, Stansted
and Gatwick places a step of roughly +0.47 C around October 2013, affecting
dry-bulb but not dewpoint, uniform across seasons and across the temperature
range -- the signature of a sensor or screen replacement rather than a
relocation. It appears in no AIP, Met Office or airport publication.

Corroborating evidence from this repository: fitting the trend from 2008
gives +1.19 C/decade, whereas fitting from 2014 gives +0.415 C/decade. A step
part-way through the window inflates a trend fitted across it, and +0.415 sits
much closer to the regional airport composite. The +1.19 figure should not be
reported as a warming estimate.

Training therefore starts 2014-01-01: one homogeneous instrument generation,
full 24-hour coverage, and consistent automated reporting. The caveat is that
the neighbour-station differencing behind the 2013 date has not been reproduced
here, so `COVERAGE_BREAK_2007` is exposed for sensitivity testing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Mapping

from weatherbot.models.distribution import (
    DEFAULT_SUPPORT_HIGH,
    DEFAULT_SUPPORT_LOW,
    TemperatureDistribution,
)

# First full year of 24-hour coverage. Use this to trade a suspected instrument
# break for six extra years of data when testing sensitivity.
COVERAGE_BREAK_2007 = date(2008, 1, 1)

# Default training start: after the suspected October 2013 instrument change.
# See the module docstring for the evidence and its limits.
HOMOGENEOUS_START = date(2014, 1, 1)

DAYS_PER_YEAR = 365.25


@dataclass(frozen=True)
class ClimatologyConfig:
    """Tuning for the seasonal kernel and the discretisation."""

    # Only observations within this many days of the target day-of-year count.
    window_days: int = 25
    # Gaussian bandwidth for the seasonal weighting, in days.
    doy_bandwidth: float = 10.0
    # Width of the kernel used to spread each sample onto the integer lattice.
    # Around 0.8 C keeps the PMF smooth without erasing the seasonal signal.
    kde_sigma: float = 0.8
    # Uniform mass added to every bin. A bin at exactly zero makes log loss
    # infinite the first time reality lands there, so this is a correctness
    # requirement rather than a nicety.
    floor: float = 1e-6
    apply_trend: bool = True
    support_low: int = DEFAULT_SUPPORT_LOW
    support_high: int = DEFAULT_SUPPORT_HIGH


def year_fraction(day: date) -> float:
    """Position within the year in [0, 1), leap-year safe."""
    start = date(day.year, 1, 1)
    end = date(day.year + 1, 1, 1)
    return (day - start).days / (end - start).days


def decimal_year(day: date) -> float:
    return day.year + year_fraction(day)


def circular_day_distance(a: date, b: date) -> float:
    """Distance between two dates' seasonal positions, in days."""
    delta = abs(year_fraction(a) - year_fraction(b))
    return min(delta, 1.0 - delta) * DAYS_PER_YEAR


class ClimatologyModel:
    """Seasonal, trend-adjusted climatology of the settlement variable."""

    def __init__(self, config: ClimatologyConfig | None = None) -> None:
        self.config = config or ClimatologyConfig()
        self._days: list[date] = []
        self._values: list[int] = []
        self._decimal_years: list[float] = []
        self.trend_c_per_year: float = 0.0
        self.n_training_days: int = 0

    def fit(
        self,
        daily_max: Mapping[date, int],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> "ClimatologyModel":
        """Fit on observed daily maxima.

        `end` should be set to the start of any evaluation period. Fitting on
        days you later score against is leakage, and the trend term makes that
        leakage real rather than theoretical.
        """
        start = start or HOMOGENEOUS_START

        selected = sorted(
            (day, value)
            for day, value in daily_max.items()
            if day >= start and (end is None or day < end)
        )
        if len(selected) < 365:
            raise ValueError(
                f"need at least a year of training data, got {len(selected)} days"
            )

        self._days = [d for d, _ in selected]
        self._values = [v for _, v in selected]
        self._decimal_years = [decimal_year(d) for d in self._days]
        self.n_training_days = len(selected)
        self.trend_c_per_year = (
            self._fit_trend() if self.config.apply_trend else 0.0
        )
        return self

    def _fit_trend(self) -> float:
        """OLS slope of daily maximum on decimal year, in C per year."""
        n = len(self._values)
        mean_x = sum(self._decimal_years) / n
        mean_y = sum(self._values) / n
        numerator = sum(
            (x - mean_x) * (y - mean_y)
            for x, y in zip(self._decimal_years, self._values)
        )
        denominator = sum((x - mean_x) ** 2 for x in self._decimal_years)
        return numerator / denominator if denominator else 0.0

    @property
    def trend_c_per_decade(self) -> float:
        return self.trend_c_per_year * 10.0

    def predict(self, target: date) -> TemperatureDistribution:
        """Distribution of the settlement value for `target`.

        Uses only day-of-year and the fitted trend -- no forecast information,
        by design.
        """
        if not self._values:
            raise RuntimeError("model has not been fit")

        cfg = self.config
        target_year = decimal_year(target)

        samples: list[float] = []
        weights: list[float] = []
        for day, value, obs_year in zip(
            self._days, self._values, self._decimal_years
        ):
            distance = circular_day_distance(target, day)
            if distance > cfg.window_days:
                continue
            weight = math.exp(-0.5 * (distance / cfg.doy_bandwidth) ** 2)
            adjusted = value + self.trend_c_per_year * (target_year - obs_year)
            samples.append(adjusted)
            weights.append(weight)

        if not samples:
            raise RuntimeError(f"no training days within the window for {target}")

        # Spread each weighted sample onto the integer lattice with a Gaussian
        # kernel: a raw empirical histogram of integers would be spiky and full
        # of zeros.
        low, high = cfg.support_low, cfg.support_high
        norm = 1.0 / (2.0 * cfg.kde_sigma**2)
        bins = []
        for k in range(low, high + 1):
            total = 0.0
            for sample, weight in zip(samples, weights):
                delta = k - sample
                if abs(delta) <= 6 * cfg.kde_sigma:
                    total += weight * math.exp(-(delta**2) * norm)
            bins.append(total)

        return TemperatureDistribution.from_weights(low, bins, floor=cfg.floor)
