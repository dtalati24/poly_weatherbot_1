"""Kalshi market data — the cross-venue signal.

Kalshi runs the same contract we do: "highest temperature in Los Angeles",
daily, settled on **the same physical station, KLAX**. Only the reporting
pipeline differs — Kalshi reads the NWS Climatological Report, Polymarket reads
Weather Underground. That makes LA the one city where the two venues are
measuring the same thermometer, which is why the cross-venue idea is tested
there and not in NYC (where Kalshi settles Central Park and Polymarket settles
LaGuardia, several miles apart).

Two things Kalshi gives us that Polymarket does not:

**A real bid and ask.** Candlesticks carry `yes_bid` and `yes_ask` separately,
so the historical spread is directly observable. Polymarket's price history is
midpoint-only and its book is not recoverable at all, so this is strictly more
information than we have on our own venue.

**Bucket edges on the same lattice.** Kalshi quotes `77° or below`, `78° to
79°`, `80° to 81°`, … `86° or above`; Polymarket quotes `77°F or below`,
`78-79°F`, `80-81°F`, … The 2°F boundaries coincide, so Kalshi's distribution
maps onto Polymarket's buckets without interpolation. Kalshi is coarser at the
tails (6 buckets against 11), so the mapping is many-to-one at the ends and
exact in the middle.

The API is public and unauthenticated for market data.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone  # noqa: F401  (re-exported)

import requests

from weatherbot.sources.polymarket import Bucket, BucketKind

API = "https://api.elections.kalshi.com/trade-api/v2"

# Ticker date component, e.g. KXHIGHLAX-26AUG01-B80.5
_TICKER_DAY = re.compile(r"^[A-Z]+-(\d{2}[A-Z]{3}\d{2})-")

# Subtitles seen on temperature markets.
_RANGE = re.compile(r"(-?\d+)\s*°?\s*to\s*(-?\d+)", re.I)
_OR_BELOW = re.compile(r"(-?\d+)\s*°?\s*or\s+below", re.I)
_OR_ABOVE = re.compile(r"(-?\d+)\s*°?\s*or\s+(?:above|higher)", re.I)


class KalshiError(RuntimeError):
    """A Kalshi request failed in a way the caller should see."""


@dataclass(frozen=True)
class KalshiCandle:
    """One period of a bucket's order book."""

    ts: int  # end of period, unix seconds UTC
    bid: float | None
    ask: float | None
    mean: float | None
    volume: float
    open_interest: float

    @property
    def has_book(self) -> bool:
        """Whether there is a genuine two-sided quote.

        An EMPTY book is reported as bid 0 / ask 1, not as null. Taken at face
        value that is a 50c midpoint, and after about 17:00 LA every bucket goes
        one-sided as the day resolves -- so a naive reading produces a confident
        uniform distribution exactly when the answer is already known. That is
        not a small error: it made the cross-venue score flat across the whole
        day instead of sharpening, which is what exposed it.
        """
        if self.bid is None or self.ask is None:
            return False
        return self.bid > 0.0 and self.ask < 1.0

    @property
    def mid(self) -> float | None:
        """Midpoint of the touch, or None if there is no real two-sided book."""
        if not self.has_book:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float | None:
        if not self.has_book:
            return None
        return self.ask - self.bid


@dataclass(frozen=True)
class KalshiMarket:
    """One bucket of one event-day."""

    ticker: str
    event_ticker: str
    day: date
    bucket: Bucket
    result: str | None = None

    @property
    def settled_yes(self) -> bool:
        return self.result == "yes"


def _dollars(node: dict | None, key: str = "close_dollars") -> float | None:
    """Pull a dollar-denominated field, which the API returns as a string."""
    if not isinstance(node, dict):
        return None
    raw = node.get(key)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_ticker_day(ticker: str) -> date | None:
    """Extract the event day from a market ticker."""
    match = _TICKER_DAY.match(ticker)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%y%b%d").date()
    except ValueError:
        return None


def parse_bucket(subtitle: str) -> Bucket | None:
    """Parse a Kalshi temperature subtitle into the project's Bucket type.

    Order matters for the same reason it does in the Polymarket parser: the
    open-ended forms contain a bare number, so a naive single-number match would
    swallow them and silently produce an EXACT bucket.
    """
    text = (subtitle or "").strip()
    if not text:
        return None

    match = _OR_BELOW.search(text)
    if match:
        value = int(match.group(1))
        return Bucket(text, BucketKind.AT_OR_BELOW, "F", value, value)

    match = _OR_ABOVE.search(text)
    if match:
        value = int(match.group(1))
        return Bucket(text, BucketKind.AT_OR_ABOVE, "F", value, value)

    match = _RANGE.search(text)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        return Bucket(text, BucketKind.RANGE, "F", min(low, high), max(low, high))

    match = re.search(r"(-?\d+)", text)
    if match:
        value = int(match.group(1))
        return Bucket(text, BucketKind.EXACT, "F", value, value)
    return None


def _get(path: str, params: dict, timeout: int = 40, retries: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(f"{API}{path}", params=params, timeout=timeout)
            if response.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise KalshiError(f"GET {path} failed: {last}")


def fetch_settled_markets(series: str, limit: int = 1000) -> list[KalshiMarket]:
    """Every settled bucket-market for a series, grouped-ready by day."""
    payload = _get("/markets", {"series_ticker": series, "status": "settled",
                                "limit": str(limit)})
    out: list[KalshiMarket] = []
    for row in payload.get("markets") or []:
        ticker = row.get("ticker") or ""
        day = parse_ticker_day(ticker)
        subtitle = row.get("yes_sub_title") or row.get("subtitle") or ""
        bucket = parse_bucket(subtitle)
        if day is None or bucket is None:
            continue
        out.append(
            KalshiMarket(
                ticker=ticker,
                event_ticker=row.get("event_ticker") or "",
                day=day,
                bucket=bucket,
                result=row.get("result"),
            )
        )
    return out


def markets_by_day(series: str) -> dict[date, list[KalshiMarket]]:
    """Settled markets keyed by event day, buckets sorted up the ladder."""
    grouped: dict[date, list[KalshiMarket]] = {}
    for market in fetch_settled_markets(series):
        grouped.setdefault(market.day, []).append(market)
    for markets in grouped.values():
        markets.sort(key=lambda m: (m.bucket.low, m.bucket.high))
    return grouped


def fetch_candles(
    series: str,
    ticker: str,
    start_ts: int,
    end_ts: int,
    *,
    period_minutes: int = 1,
) -> list[KalshiCandle]:
    """Order-book history for one bucket over a window.

    An empty list means the market never quoted in the window, which is normal
    for a bucket that opened late, not an error.
    """
    payload = _get(
        f"/series/{series}/markets/{ticker}/candlesticks",
        {"start_ts": str(start_ts), "end_ts": str(end_ts),
         "period_interval": str(period_minutes)},
    )
    out: list[KalshiCandle] = []
    for row in payload.get("candlesticks") or []:
        ts = row.get("end_period_ts")
        if ts is None:
            continue
        out.append(
            KalshiCandle(
                ts=int(ts),
                bid=_dollars(row.get("yes_bid")),
                ask=_dollars(row.get("yes_ask")),
                mean=_dollars(row.get("price"), "mean_dollars"),
                volume=float(row.get("volume_fp") or 0.0),
                open_interest=float(row.get("open_interest_fp") or 0.0),
            )
        )
    out.sort(key=lambda c: c.ts)
    return out


@dataclass(frozen=True)
class KalshiSnapshot:
    """Kalshi's cross-section at one instant."""

    ts: int
    buckets: tuple[Bucket, ...]
    mids: tuple[float, ...]
    spreads: tuple[float, ...]

    @property
    def overround(self) -> float:
        return sum(self.mids)

    @property
    def probabilities(self) -> tuple[float, ...]:
        total = self.overround
        if total <= 0:
            raise ValueError("cannot normalise a snapshot with no price mass")
        return tuple(m / total for m in self.mids)

    @property
    def mean_spread(self) -> float:
        return sum(self.spreads) / len(self.spreads) if self.spreads else 0.0


def snapshot_from_series(
    series_by_bucket: dict[str, list[KalshiCandle]],
    buckets: dict[str, Bucket],
    ts: int,
    *,
    max_staleness: int = 3600,
    require_all: bool = False,
    min_buckets: int = 4,
) -> KalshiSnapshot | None:
    """Build Kalshi's cross-section at `ts`, reading backwards only.

    Demanding all six buckets is too strict to be useful: even overnight, when
    quoting is at its best, only about 4.4 of 6 carry a two-sided book, and the
    count falls through the settlement day. So the default accepts a partial
    cross-section of at least `min_buckets` and renormalises over what is
    quoted.

    That is a real approximation and worth naming. Renormalising moves the
    missing buckets' mass onto the quoted ones rather than leaving it where it
    belongs. It is tolerable here only because the buckets that stop quoting are
    the far tails, which carry almost no mass -- and it would not be tolerable
    if the missing bucket were near the money.
    """
    picked: list[tuple[Bucket, float, float]] = []

    for ticker, candles in series_by_bucket.items():
        bucket = buckets.get(ticker)
        if bucket is None:
            continue
        best: KalshiCandle | None = None
        for candle in candles:
            if candle.ts > ts:
                break
            if candle.mid is not None:
                best = candle
        if best is None or ts - best.ts > max_staleness:
            if require_all:
                return None
            continue
        picked.append((bucket, best.mid, best.spread or 0.0))

    if len(picked) < min_buckets:
        return None
    picked.sort(key=lambda row: (row[0].low, row[0].high))
    return KalshiSnapshot(
        ts=ts,
        buckets=tuple(b for b, _, _ in picked),
        mids=tuple(m for _, m, _ in picked),
        spreads=tuple(s for _, _, s in picked),
    )
