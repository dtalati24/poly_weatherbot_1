"""Model A' — positional climatology. The benchmark that actually applies here.

Plain temperature climatology is the wrong benchmark for these markets, and the
reason is structural rather than a modelling failure.

Polymarket places the bucket window **around its own forecast**. Measured over
535 settled markets:

    outcome landed in an end bucket        5.8%
    mean relative position                 0.551   (0.5 is dead centre)
    observed minus window centre          +0.41 C, sd 1.99 C
    within +/-2 C of the window centre     83.3%

So the window itself encodes a forecast. A model that knows only the day of
year, as climatology does, piles probability onto whichever tail the season
implies and is then punished by RPS for being far away in bucket distance. That
is why raw climatology scores *worse than uniform* on these markets.

This model instead learns where in the window the outcome tends to land. It
still uses no weather information whatsoever -- it is a pure structural
baseline, and any forecast model must beat it to be worth anything.

Relative position `u = index / (n_buckets - 1)` is modelled rather than the raw
index, so markets with 7, 9 and 11 buckets all contribute to one estimate
instead of being fitted separately on thin slices.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class PositionalConfig:
    """Kernel width and smoothing for the relative-position density."""

    # Bandwidth in relative-position units. Around 0.08 keeps the central peak
    # while staying smooth across 7-to-11 bucket layouts.
    bandwidth: float = 0.08
    # Uniform mass mixed in, guarding against a zero at an unseen position.
    floor: float = 1e-3


class PositionalClimatology:
    """P(winning bucket index) learned from settled markets."""

    def __init__(self, config: PositionalConfig | None = None) -> None:
        self.config = config or PositionalConfig()
        self._positions: list[float] = []

    @property
    def n_training_markets(self) -> int:
        return len(self._positions)

    def fit(self, observations: Iterable[tuple[int, int]]) -> "PositionalClimatology":
        """Fit from (winning_index, n_buckets) pairs.

        Markets with a single bucket carry no positional information and are
        skipped rather than treated as position 0.
        """
        positions: list[float] = []
        for index, n_buckets in observations:
            if n_buckets < 2:
                continue
            if not 0 <= index < n_buckets:
                raise ValueError(f"index {index} outside 0..{n_buckets - 1}")
            positions.append(index / (n_buckets - 1))

        if not positions:
            raise ValueError("no usable training markets")

        self._positions = positions
        return self

    def predict(self, n_buckets: int) -> tuple[float, ...]:
        """Probability over bucket indices for a market of this size."""
        if n_buckets < 1:
            raise ValueError("need at least one bucket")
        if not self._positions:
            raise RuntimeError("model has not been fit")
        if n_buckets == 1:
            return (1.0,)

        h = self.config.bandwidth
        norm = 1.0 / (2.0 * h * h)

        weights = []
        for index in range(n_buckets):
            u = index / (n_buckets - 1)
            total = 0.0
            for position in self._positions:
                delta = u - position
                if abs(delta) <= 6 * h:
                    total += math.exp(-(delta**2) * norm)
            weights.append(total)

        total = sum(weights)
        if total <= 0:
            return tuple([1.0 / n_buckets] * n_buckets)

        floor = self.config.floor
        mixed = [(w / total) * (1 - floor * n_buckets) + floor for w in weights]
        scale = sum(mixed)
        return tuple(m / scale for m in mixed)


def observed_index(buckets: Sequence, observed_c: int) -> int | None:
    """Index of the bucket containing an observed integer-Celsius value."""
    for index, bucket in enumerate(buckets):
        if bucket.contains(observed_c):
            return index
    return None
