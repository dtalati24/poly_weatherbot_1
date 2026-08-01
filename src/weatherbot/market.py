"""Turn archived price paths into the market's probability distribution.

Three facts about the raw data shape everything here.

**The buckets are not on a shared clock.** Each token is stored as the instants
at which *its own* midpoint changed, so two buckets essentially never carry the
same timestamp. A cross-section therefore cannot be built by matching
timestamps; it must be built by asking each series for its last price at or
before a chosen instant. `OutcomeSeries.price_at` only ever walks backwards,
which is what keeps a backtest honest.

**The prices do not sum to one.** They sum to slightly more -- typically 1.03 to
1.05. That overround is the market's own cost of trading, and it is retained
rather than normalised away, because for a maker it is not an error term: it is
the width you are being paid to provide.

**A normalised distribution is not a tradeable price.** Normalising is right for
comparing the market's *beliefs* against a model, and wrong for deciding what a
trade earns. Both are exposed, separately and explicitly named, so the two never
get silently interchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from weatherbot.sources.clob import OutcomeSeries
from weatherbot.sources.polymarket import Bucket, parse_bucket

# How far past the end of a series we will still quote it. Inside the covered
# interval a midpoint holds until it changes, so this only bites at the edges --
# principally after settlement, where a stale quote is not evidence of anything.
DEFAULT_MAX_STALENESS = 1800


@dataclass(frozen=True)
class MarketSnapshot:
    """What the market thought, at one instant, across all buckets."""

    ts: int
    buckets: tuple[Bucket, ...]
    raw_prices: tuple[float, ...]

    @property
    def overround(self) -> float:
        """Sum of the raw prices. Above 1 by roughly the cost of trading."""
        return sum(self.raw_prices)

    @property
    def probabilities(self) -> tuple[float, ...]:
        """Raw prices normalised to sum to 1. For belief comparison only."""
        total = self.overround
        if total <= 0:
            raise ValueError("cannot normalise a snapshot with no price mass")
        return tuple(p / total for p in self.raw_prices)

    def implied_mean_c(self) -> float | None:
        """Probability-weighted centre of the bucket ladder, in Celsius.

        Censored end buckets ("23C or below") have no centre, so they are
        excluded and the remaining mass renormalised. When the tails hold real
        mass this is biased toward the middle -- callers comparing it against a
        forecast should check `tail_mass` before trusting it.
        """
        pairs = [
            (b.low, p)
            for b, p in zip(self.buckets, self.probabilities)
            if not b.is_censored and b.unit == "C"
        ]
        weight = sum(p for _, p in pairs)
        if weight <= 0:
            return None
        return sum(low * p for low, p in pairs) / weight

    @property
    def tail_mass(self) -> float:
        """Probability sitting in open-ended end buckets."""
        return sum(
            p for b, p in zip(self.buckets, self.probabilities) if b.is_censored
        )

    def mode_index(self) -> int:
        return max(range(len(self.raw_prices)), key=lambda i: self.raw_prices[i])


def snapshot(
    series: list[OutcomeSeries],
    ts: int,
    *,
    max_staleness: int = DEFAULT_MAX_STALENESS,
    require_all: bool = True,
) -> MarketSnapshot | None:
    """Build the market's cross-section at `ts`, or None if it is not clean.

    Returns None rather than a partial snapshot when any bucket lacks a fresh
    quote and `require_all` is set. A distribution missing one outcome is not a
    distribution, and quietly renormalising over the rest would move mass onto
    buckets the market never priced.
    """
    buckets: list[Bucket] = []
    prices: list[float] = []

    for entry in series:
        bucket = parse_bucket(entry.bucket_label)
        if bucket is None:
            continue
        price = entry.price_at(ts, max_staleness=max_staleness)
        if price is None:
            if require_all:
                return None
            continue
        buckets.append(bucket)
        prices.append(price)

    if not buckets or sum(prices) <= 0:
        return None

    # Market order is arbitrary (Gamma returns "23C or below" first, then "31C").
    # Sorting by the ladder is what makes RPS meaningful -- a ranked score on
    # unordered outcomes is nonsense.
    order = sorted(range(len(buckets)), key=lambda i: (buckets[i].low, buckets[i].high))
    return MarketSnapshot(
        ts=ts,
        buckets=tuple(buckets[i] for i in order),
        raw_prices=tuple(prices[i] for i in order),
    )


def snapshots_over(
    series: list[OutcomeSeries],
    start_ts: int,
    end_ts: int,
    *,
    step: int = 3600,
    max_staleness: int = DEFAULT_MAX_STALENESS,
) -> list[MarketSnapshot]:
    """Cross-sections on a regular grid across [start_ts, end_ts]."""
    out = []
    ts = start_ts
    while ts <= end_ts:
        snap = snapshot(series, ts, max_staleness=max_staleness)
        if snap is not None:
            out.append(snap)
        ts += step
    return out


def observed_index(buckets: tuple[Bucket, ...], tmax_c: int) -> int | None:
    """Which bucket contains the settled value, or None if none does."""
    for index, bucket in enumerate(buckets):
        if bucket.contains(tmax_c):
            return index
    return None
