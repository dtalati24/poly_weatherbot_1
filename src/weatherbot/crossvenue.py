"""Map Kalshi's distribution onto Polymarket's buckets.

The premise of the cross-venue idea is that Kalshi is the sharper venue, so its
prices are a better fair value for the *same underlying* than Polymarket's own.
For Los Angeles the underlying really is nearly the same — both settle on KLAX —
so the remaining work is a change of basis.

That change of basis is not free, and the reason is measured rather than
assumed. Kalshi's bucket ladder is re-centred daily on its own forecast, exactly
as Polymarket's is, and the two do not agree on where the edges sit:

    aligned (same parity)         35 / 69 days
    offset by 1 degree F          34 / 69 days

On an aligned day the mapping is exact — Kalshi's `80-81` is Polymarket's
`80-81`, and the coarse tails aggregate cleanly. On an offset day Kalshi's
`65-66` straddles Polymarket's `64-65` and `66-67`, and its probability has to
be **split between them**. There is no way to do that from Kalshi's prices
alone: the venue simply never expressed an opinion at 1-degree resolution.

So splitting needs a shape. Two are offered and they bracket the honest range:

  - `uniform` — every integer inside a Kalshi bucket gets equal weight. Assumes
    nothing, and is wrong in a knowable direction, because temperature density
    is not flat across a bucket that sits on the shoulder of the distribution.
  - `prior` — weight by a model distribution over integer Fahrenheit. Uses the
    forecast model for *shape only*, never for level, so a bad prior degrades
    the split rather than injecting its own view of where the temperature is.

Censored end buckets ("64 or below") need a tail shape too, and there the prior
matters more, because the bucket can cover ten degrees rather than two.
"""

from __future__ import annotations

from dataclasses import dataclass

from weatherbot.models.distribution import TemperatureDistribution
from weatherbot.sources.kalshi import KalshiSnapshot
from weatherbot.sources.polymarket import Bucket, BucketKind

# Support for Fahrenheit distributions. Wide enough for any US city market.
F_SUPPORT_LOW = -40
F_SUPPORT_HIGH = 140

# How far an open-ended end bucket is assumed to extend when spreading its mass.
# Kalshi sets its tails outside the plausible range, so this only needs to be
# wide enough that the prior's own decay does the work.
TAIL_SPAN = 15


@dataclass(frozen=True)
class Alignment:
    """How well two bucket ladders line up on a given day."""

    aligned: bool
    kalshi_edges: tuple[int, ...]
    poly_edges: tuple[int, ...]

    @property
    def description(self) -> str:
        return "aligned" if self.aligned else "offset"


def interior_edges(buckets: tuple[Bucket, ...]) -> tuple[int, ...]:
    """Low edges of the closed buckets, ignoring open-ended tails."""
    return tuple(b.low for b in buckets if b.kind is BucketKind.RANGE)


def alignment(
    kalshi_buckets: tuple[Bucket, ...], poly_buckets: tuple[Bucket, ...]
) -> Alignment:
    """Whether the two ladders sit on the same 2-degree lattice."""
    ke = interior_edges(kalshi_buckets)
    pe = interior_edges(poly_buckets)
    if not ke or not pe:
        return Alignment(False, ke, pe)
    same_parity = all((edge - pe[0]) % 2 == 0 for edge in ke)
    return Alignment(same_parity, ke, pe)


def _integers_for(bucket: Bucket) -> list[int]:
    """Integer Fahrenheit values a bucket covers, tails truncated to TAIL_SPAN."""
    if bucket.kind is BucketKind.RANGE:
        return list(range(bucket.low, bucket.high + 1))
    if bucket.kind is BucketKind.EXACT:
        return [bucket.low]
    if bucket.kind is BucketKind.AT_OR_BELOW:
        return list(range(bucket.low - TAIL_SPAN, bucket.low + 1))
    return list(range(bucket.low, bucket.low + TAIL_SPAN + 1))


def to_distribution(
    snapshot: KalshiSnapshot,
    prior: TemperatureDistribution | None = None,
    *,
    floor: float = 1e-9,
) -> TemperatureDistribution:
    """Spread Kalshi's bucket probabilities onto integer Fahrenheit.

    Each Kalshi bucket keeps exactly its own probability; the prior only decides
    how that probability is distributed *within* the bucket. So the mapping
    preserves Kalshi's view at Kalshi's resolution and borrows shape only where
    Kalshi is silent, which is the whole point.
    """
    weights = [0.0] * (F_SUPPORT_HIGH - F_SUPPORT_LOW + 1)

    for bucket, probability in zip(snapshot.buckets, snapshot.probabilities):
        degrees = [
            d for d in _integers_for(bucket) if F_SUPPORT_LOW <= d <= F_SUPPORT_HIGH
        ]
        if not degrees:
            continue

        if prior is None:
            shares = [1.0] * len(degrees)
        else:
            shares = [prior.probability(d) for d in degrees]
            # A prior that puts no mass anywhere in this bucket carries no
            # information about the split, so fall back rather than divide by
            # zero or, worse, silently drop Kalshi's probability.
            if sum(shares) <= 0:
                shares = [1.0] * len(degrees)

        total = sum(shares)
        for degree, share in zip(degrees, shares):
            weights[degree - F_SUPPORT_LOW] += probability * share / total

    return TemperatureDistribution.from_weights(F_SUPPORT_LOW, weights, floor=floor)


def to_poly_buckets(
    snapshot: KalshiSnapshot,
    poly_buckets: tuple[Bucket, ...],
    prior: TemperatureDistribution | None = None,
    offset: "SourceOffset | None" = None,
) -> tuple[float, ...]:
    """Kalshi's view expressed in Polymarket's buckets.

    `offset` translates from Kalshi's settlement variable to Polymarket's. It is
    not optional in practice -- see SourceOffset -- but is left injectable so the
    uncorrected case can be scored for comparison.
    """
    distribution = to_distribution(snapshot, prior)
    if offset is not None:
        distribution = offset.apply(distribution)
    out = [
        sum(
            p
            for k, p in zip(distribution.support, distribution.probabilities)
            if bucket.holds(k)
        )
        for bucket in poly_buckets
    ]
    total = sum(out)
    if total <= 0:
        raise ValueError("Kalshi distribution places no mass in Polymarket's buckets")
    return tuple(p / total for p in out)


@dataclass(frozen=True)
class SourceOffset:
    """Empirical distribution of (Polymarket settled - Kalshi settled), in F.

    The two venues do NOT settle on the same number, and the gap is systematic
    rather than noise. Kalshi reads the NWS Climatological Report, which is
    derived from ASOS 5-minute data; Polymarket reads Weather Underground's
    METAR record, and KLAX transmits hourly at :53. So CLI sees peaks that fall
    between METARs and is never lower:

        exact agreement   60%
        CLI higher by 1   33%
        CLI higher by 2    7%
        CLI lower          0%

    Ignoring this is fatal rather than merely imprecise. Kalshi becomes very
    confident late in the day about a value one bucket above the one Polymarket
    will settle on, so its score stops improving as the day resolves -- which is
    exactly the signature observed before this correction was applied.
    """

    weights: dict[int, float]

    @classmethod
    def fit(cls, pairs: list[tuple[int, int]], smoothing: float = 0.5) -> "SourceOffset":
        """From (kalshi settled F, polymarket settled F) pairs.

        Laplace-smoothed over the observed range so an offset seen zero times in
        a short sample is unlikely rather than impossible.
        """
        counts: dict[int, float] = {}
        for kalshi_value, poly_value in pairs:
            counts[poly_value - kalshi_value] = counts.get(poly_value - kalshi_value, 0) + 1
        if not counts:
            return cls({0: 1.0})
        lo, hi = min(counts), max(counts)
        smoothed = {d: counts.get(d, 0.0) + smoothing for d in range(lo, hi + 1)}
        total = sum(smoothed.values())
        return cls({d: w / total for d, w in smoothed.items()})

    @property
    def mean(self) -> float:
        return sum(d * w for d, w in self.weights.items())

    def apply(self, distribution: TemperatureDistribution) -> TemperatureDistribution:
        """Convolve a Kalshi-basis distribution into the Polymarket basis."""
        weights = [0.0] * len(distribution.probabilities)
        for index, probability in enumerate(distribution.probabilities):
            if probability <= 0:
                continue
            for shift, share in self.weights.items():
                target = index + shift
                if 0 <= target < len(weights):
                    weights[target] += probability * share
        return TemperatureDistribution.from_weights(
            distribution.low, weights, floor=1e-12
        )
