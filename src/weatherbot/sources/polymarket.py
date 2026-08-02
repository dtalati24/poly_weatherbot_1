"""Polymarket Gamma API client for London daily-high temperature markets.

Used in Phase 0 purely as a source of ground-truth *resolutions*, so the
reconstruction in weatherbot.resolve can be validated against what Polymarket
actually settled. No trading happens here.

Two traps this module exists to defuse, both discovered empirically:

  1. Slugs do not identify a date. Polymarket has used a year-suffixed form
     ("...-july-30-2026") and an unsuffixed one ("...-may-17"), inconsistently
     across years. The unsuffixed form for a given month/day may belong to any
     year, so matching on slug alone silently pairs observations from one year
     with a settlement from another. Every event's target date is therefore
     verified against `eventDate`/`endDate` before it is used.

  2. The bucket format is not stable. Observed variants include 7, 9 and 11
     outcomes; single-degree Celsius ("28°C"); and two-degree Fahrenheit ranges
     ("64-65°F", also written with an en dash). Buckets are parsed with their
     unit attached and compared in that unit.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from math import floor
from pathlib import Path

import requests

from weatherbot.config import GAMMA_API, MARKETS_DIR, MARKET_SLUG_PREFIX

MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

# Range form: "54-55°F" or "64–65°F" (hyphen or en/em dash).
_RANGE_RE = re.compile(r"(-?\d+)\s*[-–—]\s*(-?\d+)")
_SINGLE_RE = re.compile(r"(-?\d+)")
_BELOW_RE = re.compile(r"or\s+(below|lower)", re.IGNORECASE)
_ABOVE_RE = re.compile(r"or\s+(higher|above)", re.IGNORECASE)


def _round_half_up(value: float) -> int:
    """Round half away from zero (see weatherbot.resolve for rationale)."""
    return int(floor(value + 0.5)) if value >= 0 else -int(floor(-value + 0.5))


def celsius_to_fahrenheit_int(tmax_c: int) -> int:
    """Convert an integer Celsius reading the way a display layer would."""
    return _round_half_up(tmax_c * 9.0 / 5.0 + 32.0)


class BucketKind(str, Enum):
    """Shape of a market outcome."""

    EXACT = "exact"
    RANGE = "range"
    AT_OR_BELOW = "at_or_below"
    AT_OR_ABOVE = "at_or_above"


@dataclass(frozen=True)
class Bucket:
    """One market outcome, carrying its own unit."""

    label: str
    kind: BucketKind
    unit: str  # "C" or "F"
    low: int
    high: int

    @property
    def is_censored(self) -> bool:
        """Open-ended tails bound the maximum but do not pin it."""
        return self.kind in (BucketKind.AT_OR_BELOW, BucketKind.AT_OR_ABOVE)

    def contains(self, tmax_c: int) -> bool:
        """Does an integer-Celsius reading fall in this bucket?

        For a Fahrenheit bucket this converts, and the conversion starts from an
        already-rounded integer Celsius value. That is correct only where the
        station reports whole degrees Celsius, which is true at EGLC and NOT
        true at US stations -- their METARs carry tenths in the remark T-group.
        Rounding to whole C first and then converting double-rounds, and lands
        in the wrong bucket on 28% of LA days. Use `holds` there.
        """
        value = tmax_c if self.unit == "C" else celsius_to_fahrenheit_int(tmax_c)
        return self.holds(value)

    def holds(self, value: int) -> bool:
        """Membership test for a value already in this bucket's own unit.

        No conversion and no rounding, so it cannot double-round. This is the
        right entry point whenever the caller has already reduced observations
        to the settled integer.
        """
        if self.kind is BucketKind.EXACT:
            return value == self.low
        if self.kind is BucketKind.RANGE:
            return self.low <= value <= self.high
        if self.kind is BucketKind.AT_OR_BELOW:
            return value <= self.low
        return value >= self.low


@dataclass(frozen=True)
class ResolvedMarket:
    """A settled London daily-high market, verified to belong to `day`."""

    day: date
    slug: str
    buckets: tuple[Bucket, ...]
    winner: Bucket
    volume: float
    liquidity: float

    @property
    def unit(self) -> str:
        return self.winner.unit

    @property
    def is_censored(self) -> bool:
        return self.winner.is_censored

    def agrees_with(self, tmax_c: int | None) -> bool:
        return tmax_c is not None and self.winner.contains(tmax_c)


def parse_bucket(label: str) -> Bucket | None:
    """Parse an outcome label into a Bucket, preserving its unit."""
    if not label or not label.strip():
        return None
    text = label.strip()
    unit = "F" if re.search(r"°?\s*F\b", text) else "C"

    # Ranges must be tried first: a naive integer scan reads "54-55" as 54 and
    # -55, because the separator looks like a minus sign.
    range_match = _RANGE_RE.search(text)
    if range_match:
        low, high = int(range_match.group(1)), int(range_match.group(2))
        if low > high:
            low, high = high, low
        return Bucket(label=text, kind=BucketKind.RANGE, unit=unit, low=low, high=high)

    single = _SINGLE_RE.search(text)
    if not single:
        return None
    value = int(single.group(1))

    if _BELOW_RE.search(text):
        kind = BucketKind.AT_OR_BELOW
    elif _ABOVE_RE.search(text):
        kind = BucketKind.AT_OR_ABOVE
    else:
        kind = BucketKind.EXACT

    return Bucket(label=text, kind=kind, unit=unit, low=value, high=value)


def slug_candidates(day: date, prefix: str = MARKET_SLUG_PREFIX) -> list[str]:
    """Candidate event slugs for a date, most specific first.

    `prefix` selects the city. The unsuffixed stem is tried second because it
    can belong to a different year's market entirely -- callers must still
    verify with `event_target_date`.
    """
    stem = f"{prefix}-{MONTHS[day.month - 1]}-{day.day}"
    return [f"{stem}-{day.year}", stem]


def event_target_date(event: dict) -> date | None:
    """The date an event is actually about.

    Prefers the explicit `eventDate`; falls back to the date part of `endDate`,
    which has matched `eventDate` wherever both are present.
    """
    raw_event_date = event.get("eventDate")
    if raw_event_date:
        try:
            return datetime.fromisoformat(str(raw_event_date)[:10]).date()
        except ValueError:
            pass

    raw_end = event.get("endDate")
    if raw_end:
        try:
            return datetime.fromisoformat(str(raw_end)[:10]).date()
        except ValueError:
            pass
    return None


def _cache_path(slug: str) -> Path:
    return MARKETS_DIR / f"{slug}.json"


def fetch_event(slug: str, *, use_cache: bool = True, timeout: int = 30) -> dict | None:
    """Fetch one Gamma event by slug, caching the raw payload."""
    path = _cache_path(slug)
    if use_cache and path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        return cached or None

    response = requests.get(
        f"{GAMMA_API}/events", params={"slug": slug}, timeout=timeout
    )
    response.raise_for_status()
    payload = response.json()
    event = payload[0] if isinstance(payload, list) and payload else None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event or {}, indent=2), encoding="utf-8")
    return event


def _as_list(value) -> list:
    """Gamma returns some array fields as JSON-encoded strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse_resolved_event(event: dict, day: date) -> ResolvedMarket | None:
    """Extract the settled outcome, or None if unresolved or ambiguous."""
    buckets: list[Bucket] = []
    winner: Bucket | None = None

    for market in event.get("markets") or []:
        bucket = parse_bucket(market.get("groupItemTitle") or "")
        if bucket is None:
            continue
        buckets.append(bucket)

        prices = [
            float(p) for p in _as_list(market.get("outcomePrices")) if p not in (None, "")
        ]
        # A settled binary market prices YES at exactly 1.
        if prices and prices[0] >= 0.99:
            if winner is not None:
                return None  # more than one winning bucket
            winner = bucket

    if winner is None or not buckets:
        return None

    return ResolvedMarket(
        day=day,
        slug=event.get("slug", ""),
        buckets=tuple(buckets),
        winner=winner,
        volume=float(event.get("volume") or 0.0),
        liquidity=float(event.get("liquidity") or 0.0),
    )


def fetch_resolved_market(
    day: date,
    *,
    use_cache: bool = True,
    polite_delay: float = 0.25,
    prefix: str = MARKET_SLUG_PREFIX,
) -> ResolvedMarket | None:
    """Find the settled market for `day`, verifying it is really that day."""
    for slug in slug_candidates(day, prefix):
        # Only rate-limit real network calls; replaying the cache should be fast.
        was_cached = use_cache and _cache_path(slug).exists()
        event = fetch_event(slug, use_cache=use_cache)
        if polite_delay and not was_cached:
            time.sleep(polite_delay)
        if not event:
            continue
        # Guard against the unsuffixed slug resolving to another year's market.
        if event_target_date(event) != day:
            continue
        resolved = parse_resolved_event(event, day)
        if resolved:
            return resolved
    return None


def fetch_resolved_range(
    start: date, end: date, *, use_cache: bool = True,
    prefix: str = MARKET_SLUG_PREFIX,
) -> list[ResolvedMarket]:
    """Collect every settled market in [start, end)."""
    out: list[ResolvedMarket] = []
    day = start
    while day < end:
        market = fetch_resolved_market(day, use_cache=use_cache, prefix=prefix)
        if market:
            out.append(market)
        day += timedelta(days=1)
    return out
