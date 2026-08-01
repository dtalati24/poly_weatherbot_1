"""Polymarket CLOB price history.

This is the *market's own opinion*, and it is the only data source in the
project that lets us ask the question that actually matters: not "was the model
right?" but "was the model right about something the market had wrong?"

Everything else here is a consequence of one measured fact:

    Price history is retained for a rolling ~31 days and then deleted.

Probing settled London markets on 2026-08-01 returned, for *every* bucket:

    2025-06-10   0 points        2026-07-01    2 points (22:00Z, 23:00Z)
    2025-12-05   0 points        2026-07-10  715 points
    2026-05-01   0 points        2026-07-15  737 points

A market with real settled volume returning zero points across all eleven
buckets is pruning, not absence of trading. So this source has the same
property as the ensemble archive -- **a day not harvested is a day lost
permanently** -- except it is worse, because the window is 31 days rather than
4, which makes it easy to believe there is no urgency until a month has
silently rolled off.

Resolution: the API accepts a `fidelity` parameter, but 1, 5 and 10 all return
the same ~10-minute series (measured step 579-586 s). 10 minutes is the floor;
asking for finer is not an error, it just does nothing.

Leakage note: unlike a forecast record, a price point is self-anchoring. Its
own timestamp is the moment it was knowable, so no separate harvest anchor is
needed to use it safely.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from weatherbot.config import CLOB_API

PRICES_HISTORY_URL = f"{CLOB_API}/prices-history"

# 10 minutes is the finest the endpoint actually honours; see module docstring.
FINEST_FIDELITY_MINUTES = 10

# Retention measured on 2026-08-01. Treated as a floor for planning, not a
# guarantee -- it is undocumented and could change without notice.
RETENTION_DAYS = 31


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
    """The price path of a single bucket's YES token."""

    bucket_label: str
    token_id: str
    points: tuple[PricePoint, ...]

    def __len__(self) -> int:
        return len(self.points)

    @property
    def first_ts(self) -> int | None:
        return self.points[0].ts if self.points else None

    @property
    def last_ts(self) -> int | None:
        return self.points[-1].ts if self.points else None

    def price_at(self, ts: int, *, max_staleness: int = 3600) -> float | None:
        """Last price at or before `ts`, or None if there isn't a fresh one.

        A backtest must never read a price from the future, so this walks
        backwards only. `max_staleness` rejects a quote so old it is no longer
        evidence of anything -- without it, a market that stopped trading would
        appear to hold a firm opinion indefinitely.
        """
        best: PricePoint | None = None
        for point in self.points:
            if point.ts > ts:
                break
            best = point
        if best is None or ts - best.ts > max_staleness:
            return None
        return best.price


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


def fetch_price_history(
    token_id: str,
    *,
    fidelity: int = FINEST_FIDELITY_MINUTES,
    timeout: int = 30,
    retries: int = 3,
) -> list[PricePoint]:
    """Fetch the full retained price path for one outcome token.

    Returns an empty list for a token whose history has aged out -- that is a
    normal, expected outcome for anything older than the retention window, not
    an error worth raising.
    """
    params = {"market": token_id, "interval": "max", "fidelity": str(fidelity)}
    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            response = requests.get(PRICES_HISTORY_URL, params=params, timeout=timeout)
            if response.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            response.raise_for_status()
            history = response.json().get("history") or []
            points = [
                PricePoint(ts=int(row["t"]), price=float(row["p"]))
                for row in history
                if row.get("t") is not None and row.get("p") is not None
            ]
            points.sort(key=lambda p: p.ts)
            return points
        except (requests.RequestException, ValueError, KeyError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))

    raise ClobError(f"price history failed for {token_id}: {last_error}")


def fetch_event_prices(
    event: dict,
    *,
    fidelity: int = FINEST_FIDELITY_MINUTES,
    polite_delay: float = 0.15,
) -> list[OutcomeSeries]:
    """Fetch every bucket's YES price path for one Gamma event."""
    out: list[OutcomeSeries] = []
    for index, market in enumerate(event.get("markets") or []):
        ids = token_ids(market)
        if ids is None:
            continue
        if index and polite_delay:
            time.sleep(polite_delay)
        points = fetch_price_history(ids[0], fidelity=fidelity)
        out.append(
            OutcomeSeries(
                bucket_label=str(market.get("groupItemTitle") or ""),
                token_id=ids[0],
                points=tuple(points),
            )
        )
    return out
