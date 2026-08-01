"""Probability distributions over the settlement variable.

Every model in this project produces the same object: a probability mass
function over **integer degrees Celsius**. Market buckets are applied only at
the last step.

That separation is deliberate and matters:

  - Market bucket structure is not stable. Across the settled history we see 7,
    9 and 11 outcomes, single-degree Celsius and two-degree Fahrenheit ranges.
    A model that predicted "bucket 4 of 11" would be unusable across eras.
  - It keeps the meteorology (what temperature will it be) separate from the
    settlement mechanics (which box does that fall in), which is the C2
    architecture in PLAN.md.

Bucket probabilities are obtained by summing point masses over the integers each
bucket contains, which reuses `Bucket.contains` and therefore inherits its
Fahrenheit conversion for free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from weatherbot.sources.polymarket import Bucket

# Support bounds for EGLC. The all-time UK record is ~40C and London lows rarely
# reach -10C, so this comfortably covers anything the station can produce while
# keeping the arrays small.
DEFAULT_SUPPORT_LOW = -15
DEFAULT_SUPPORT_HIGH = 45


@dataclass(frozen=True)
class TemperatureDistribution:
    """A normalised PMF over consecutive integer temperatures."""

    low: int
    probabilities: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.probabilities:
            raise ValueError("distribution must have at least one bin")
        total = sum(self.probabilities)
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"probabilities must sum to 1, got {total!r}")
        if any(p < 0 for p in self.probabilities):
            raise ValueError("probabilities must be non-negative")

    @property
    def high(self) -> int:
        return self.low + len(self.probabilities) - 1

    @property
    def support(self) -> range:
        return range(self.low, self.high + 1)

    def probability(self, degrees: int) -> float:
        """P(Y = degrees). Zero outside the support."""
        index = degrees - self.low
        if 0 <= index < len(self.probabilities):
            return self.probabilities[index]
        return 0.0

    def cdf(self, degrees: int) -> float:
        """P(Y <= degrees)."""
        if degrees < self.low:
            return 0.0
        if degrees >= self.high:
            return 1.0
        upto = degrees - self.low + 1
        return sum(self.probabilities[:upto])

    def mean(self) -> float:
        return sum(k * p for k, p in zip(self.support, self.probabilities))

    def quantile(self, q: float) -> int:
        """Smallest integer k with P(Y <= k) >= q."""
        if not 0.0 < q <= 1.0:
            raise ValueError(f"q must be in (0, 1], got {q}")
        cumulative = 0.0
        for k, p in zip(self.support, self.probabilities):
            cumulative += p
            if cumulative >= q - 1e-12:
                return k
        return self.high

    def mode(self) -> int:
        best = max(range(len(self.probabilities)), key=lambda i: self.probabilities[i])
        return self.low + best

    def to_buckets(self, buckets: Sequence["Bucket"]) -> tuple[float, ...]:
        """Collapse to probabilities over an ordered set of market buckets.

        Raises if the buckets do not account for essentially all the mass --
        silently dropping probability would corrupt every downstream metric.
        """
        out = [
            sum(p for k, p in zip(self.support, self.probabilities) if bucket.contains(k))
            for bucket in buckets
        ]
        total = sum(out)
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(
                f"buckets capture {total:.6f} of the distribution, not 1.0 -- "
                f"they are not exhaustive over the support "
                f"[{self.low}, {self.high}]"
            )
        # Renormalise away floating-point drift only.
        return tuple(p / total for p in out)

    @classmethod
    def from_weights(
        cls, low: int, weights: Iterable[float], floor: float = 0.0
    ) -> "TemperatureDistribution":
        """Build from unnormalised non-negative weights.

        `floor` adds a uniform mass to every bin before normalising. Any bin at
        exactly zero makes log loss infinite the first time reality lands there,
        so a small floor is a correctness requirement, not a nicety.
        """
        values = [float(w) for w in weights]
        if not values:
            raise ValueError("need at least one weight")
        if any(w < 0 for w in values):
            raise ValueError("weights must be non-negative")

        if floor > 0:
            values = [w + floor for w in values]

        total = sum(values)
        if total <= 0:
            raise ValueError("weights sum to zero; cannot normalise")
        return cls(low=low, probabilities=tuple(w / total for w in values))

    @classmethod
    def point_mass(cls, degrees: int) -> "TemperatureDistribution":
        """Degenerate distribution, useful in tests and as a sanity check."""
        return cls(low=degrees, probabilities=(1.0,))
