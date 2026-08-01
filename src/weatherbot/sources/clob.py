"""Polymarket CLOB price history.

This is the market's own opinion, and the only source here that answers the
question that actually decides whether any of this is worth trading: not "was
the model right?" but "was the model right about something the price had wrong?"

Four properties of the endpoint drive this module, and three of them are traps.

**`interval=max` silently truncates settled markets.** This is the important
one. On a market resolved more than roughly a month ago it returns an empty
history, and on a recent one it returns a degraded ~10-minute series no matter
what `fidelity` you ask for. Reading that at face value makes it look as though
Polymarket deletes price history after ~31 days. It does not. The same tokens
return full 1-minute data when queried with explicit `startTs`/`endTs`:

    market day    interval=max      startTs/endTs
    2025-06-10       0 points      6599 points, 60 s steps
    2025-12-05       0 points      3357 points, 60 s steps
    2026-05-01       0 points      3514 points, 60 s steps
    2026-07-15     406 points      4058 points, 60 s steps

So this module never uses `interval`. Note that passing both is worse than
useless: `interval` silently wins and you get the truncated series back while
believing you asked for a window.

**Windows are capped at 15 days** (21600 minutes). A market's whole life is
about a week, so one window covers it, but the cap is enforced here rather than
discovered as an opaque `invalid filters` error.

**`p` is the midpoint of the book, not a traded price.** Verified against
`/midpoint`, `/last-trade-price` and `/price` across 16 live markets: `p` equals
`(best_bid + best_ask)/2` exactly, including on wide books where the last trade
sat far from the mid. Two consequences that matter more than they sound: the
series moves when the book moves even with zero volume, and it is unweighted by
size, so a one-share quote at the touch moves it as much as a ten-thousand-share
one. It is evidence about belief, not about what you could have transacted.

**There is no public historical order book.** `/book` is current-state only, the
Goldsky `Orderbook` entity is aggregate volume counters rather than depth, and
the complementary NO series carries no extra information (mid_YES + mid_NO - 1
was exactly 0 in 360/360 sampled minutes). The historical spread is simply not
recoverable, which is the binding constraint on any maker backtest built on
this data.

Leakage note: unlike a forecast record, a price point is self-anchoring. Its own
timestamp is the moment it was knowable, so no separate harvest anchor is needed
to use it safely.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock, timedelta, timezone

import requests

from weatherbot.config import CLOB_API

PRICES_HISTORY_URL = f"{CLOB_API}/prices-history"

# The endpoint honours 1-minute sampling when given an explicit window.
FINEST_FIDELITY_MINUTES = 1

# Hard server limit on startTs..endTs, measured by binary search: 21600 minutes
# accepted, 21601 rejected.
MAX_WINDOW_MINUTES = 21600

# A London market opens ~2 days ahead. Seven days back is comfortable margin and
# still one window, well inside the cap.
LOOKBACK_DAYS = 7


class ClobError(RuntimeError):
    """A CLOB request failed in a way the caller should see."""


@dataclass(frozen=True)
class PricePoint:
    """One price observation for one outcome token."""

    ts: int  # unix seconds, UTC
    price: float

    @property
    def moment(self) -> datetime:
        return datetime.fromtimestamp(self.ts, timezone.utc)


@dataclass(frozen=True)
class OutcomeSeries:
    """The midpoint path of a single bucket's YES token.

    `points` holds only the instants at which the price *changed*. On a quiet
    book that is around 7% of the 1-minute samples, so storing changes rather
    than samples cuts the archive tenfold at zero information loss -- but only
    because `first_ts`/`last_ts` record the true extent of coverage separately.
    Without them a flat tail would be indistinguishable from a series that
    stopped updating, and `price_at` would wrongly report it stale.
    """

    bucket_label: str
    token_id: str
    points: tuple[PricePoint, ...]
    first_ts: int | None = None
    last_ts: int | None = None

    def __len__(self) -> int:
        return len(self.points)

    @property
    def covers(self) -> tuple[int, int] | None:
        """The interval this series is evidence about."""
        if not self.points:
            return None
        start = self.first_ts if self.first_ts is not None else self.points[0].ts
        end = self.last_ts if self.last_ts is not None else self.points[-1].ts
        return start, end

    def price_at(self, ts: int, *, max_staleness: int = 3600) -> float | None:
        """Midpoint at `ts`, or None if we cannot honestly claim to know it.

        Walks backwards only -- a backtest that reads a price from the future is
        both silent and fatal. Returns None before coverage starts, after it
        ends, or when the last change is older than `max_staleness` *and* the
        series had stopped being observed.
        """
        span = self.covers
        if span is None:
            return None
        start, end = span
        if ts < start or ts > end + max_staleness:
            return None

        best: PricePoint | None = None
        for point in self.points:
            if point.ts > ts:
                break
            best = point
        return best.price if best is not None else None


def token_ids(market: dict) -> tuple[str, str] | None:
    """Extract (yes_token, no_token) from a Gamma market, or None.

    Gamma returns this field as a JSON-encoded string rather than an array,
    which is the same trap `_as_list` exists for in the polymarket module.
    """
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    return str(raw[0]), str(raw[1])


def market_window(day: date, *, lookback_days: int = LOOKBACK_DAYS) -> tuple[int, int]:
    """UTC window covering a market day's whole tradeable life."""
    start = datetime.combine(day - timedelta(days=lookback_days), clock.min, timezone.utc)
    end = datetime.combine(day + timedelta(days=1), clock.min, timezone.utc)
    minutes = int((end - start).total_seconds() // 60)
    if minutes > MAX_WINDOW_MINUTES:
        raise ValueError(
            f"window of {minutes} minutes exceeds the server cap of "
            f"{MAX_WINDOW_MINUTES}; reduce lookback_days"
        )
    return int(start.timestamp()), int(end.timestamp())


def to_change_points(rows: list[tuple[int, float]]) -> list[PricePoint]:
    """Keep only the instants at which the price changed."""
    out: list[PricePoint] = []
    last: float | None = None
    for ts, price in rows:
        if price != last:
            out.append(PricePoint(ts=ts, price=price))
            last = price
    return out


def fetch_price_history(
    token_id: str,
    start_ts: int,
    end_ts: int,
    *,
    fidelity: int = FINEST_FIDELITY_MINUTES,
    timeout: int = 60,
    retries: int = 3,
) -> tuple[list[PricePoint], int | None, int | None]:
    """Fetch one token's midpoint path over an explicit window.

    Returns `(change_points, first_ts, last_ts)`. An empty result means the
    token genuinely never quoted in the window -- it does not mean the history
    expired, because with an explicit window nothing expires.
    """
    minutes = (end_ts - start_ts) // 60
    if minutes > MAX_WINDOW_MINUTES:
        raise ValueError(f"window of {minutes} minutes exceeds cap {MAX_WINDOW_MINUTES}")

    # `interval` is deliberately absent: if sent alongside a window it wins, and
    # returns the truncated series instead.
    params = {
        "market": token_id,
        "startTs": str(start_ts),
        "endTs": str(end_ts),
        "fidelity": str(fidelity),
    }
    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            response = requests.get(PRICES_HISTORY_URL, params=params, timeout=timeout)
            if response.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            response.raise_for_status()
            history = response.json().get("history") or []
            rows = sorted(
                (int(r["t"]), float(r["p"]))
                for r in history
                if r.get("t") is not None and r.get("p") is not None
            )
            if not rows:
                return [], None, None
            return to_change_points(rows), rows[0][0], rows[-1][0]
        except (requests.RequestException, ValueError, KeyError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))

    raise ClobError(f"price history failed for {token_id}: {last_error}")


def fetch_event_prices(
    event: dict,
    day: date,
    *,
    fidelity: int = FINEST_FIDELITY_MINUTES,
    lookback_days: int = LOOKBACK_DAYS,
    polite_delay: float = 0.1,
) -> list[OutcomeSeries]:
    """Fetch every bucket's midpoint path for one market day."""
    start_ts, end_ts = market_window(day, lookback_days=lookback_days)
    out: list[OutcomeSeries] = []

    for index, market in enumerate(event.get("markets") or []):
        ids = token_ids(market)
        if ids is None:
            continue
        if index and polite_delay:
            time.sleep(polite_delay)
        points, first_ts, last_ts = fetch_price_history(
            ids[0], start_ts, end_ts, fidelity=fidelity
        )
        out.append(
            OutcomeSeries(
                bucket_label=str(market.get("groupItemTitle") or ""),
                token_id=ids[0],
                points=tuple(points),
                first_ts=first_ts,
                last_ts=last_ts,
            )
        )
    return out
