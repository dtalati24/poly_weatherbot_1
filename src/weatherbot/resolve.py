"""Reconstruct the Polymarket settlement variable from raw observations.

Market rule:
    "the highest temperature recorded for all times on this day for the London
     City Airport Station" -- source Wunderground, whole degrees Celsius.

Two things the rule does not pin down, both of which are resolved empirically
against already-settled markets rather than assumed:

  1. Day boundary. "This day" in London local time (Europe/London, so UTC+1
     under BST) or in UTC? These differ for observations in the 23:00-00:00
     UTC window.

  2. Rounding path. EGLC METARs encode temperature in whole degrees Celsius
     already, so `as_reported` should be exact. But if Wunderground stores
     Fahrenheit internally and converts back for display, the value passes
     through a second rounding (C -> F -> C) that can shift it by a degree.
     `via_fahrenheit` models that hypothesis.

Both are enumerated as strategies so `scripts/validate_resolve.py` can score
every combination against real resolutions and pick the one that reproduces
history exactly. Do not hardcode a winner until that validation passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from itertools import product

from weatherbot.config import LOCAL_TZ, UTC
from weatherbot.observation import Observation


class DayBoundary(str, Enum):
    """Which calendar day an observation is attributed to."""

    LOCAL = "local"
    UTC = "utc"


class Rounding(str, Enum):
    """How the reported Celsius value is converted to the settled integer."""

    AS_REPORTED = "as_reported"
    VIA_FAHRENHEIT = "via_fahrenheit"


class ReportFilter(str, Enum):
    """Which report types contribute to the daily maximum."""

    ALL = "all"
    ROUTINE_ONLY = "routine_only"


@dataclass(frozen=True)
class Strategy:
    """One candidate interpretation of the resolution rule."""

    boundary: DayBoundary = DayBoundary.LOCAL
    rounding: Rounding = Rounding.AS_REPORTED
    reports: ReportFilter = ReportFilter.ALL

    def __str__(self) -> str:
        return f"{self.boundary.value}/{self.rounding.value}/{self.reports.value}"


def all_strategies() -> list[Strategy]:
    """Every combination worth testing in Phase 0."""
    return [
        Strategy(boundary=b, rounding=r, reports=f)
        for b, r, f in product(DayBoundary, Rounding, ReportFilter)
    ]


@dataclass(frozen=True)
class DailyMax:
    """The reconstructed settlement value for one day, with provenance."""

    day: date
    tmax_c: int
    n_obs: int
    argmax_utc: datetime
    first_obs_utc: datetime
    last_obs_utc: datetime

    @property
    def argmax_local(self) -> datetime:
        return self.argmax_utc.astimezone(LOCAL_TZ)

    @property
    def last_obs_local(self) -> datetime:
        return self.last_obs_utc.astimezone(LOCAL_TZ)


def _apply_rounding(tmpc: float, rounding: Rounding) -> int:
    """Convert a reported Celsius value to the integer that would be settled."""
    if rounding is Rounding.AS_REPORTED:
        return _round_half_up(tmpc)
    if rounding is Rounding.VIA_FAHRENHEIT:
        fahrenheit = _round_half_up(tmpc * 9.0 / 5.0 + 32.0)
        return _round_half_up((fahrenheit - 32.0) * 5.0 / 9.0)
    raise ValueError(f"unknown rounding: {rounding}")


def _round_half_up(value: float) -> int:
    """Round half away from zero.

    Python's built-in round() uses banker's rounding, which would send 27.5 to
    28 but 26.5 to 26. Meteorological convention rounds halves up, and getting
    this wrong shifts bucket probabilities in exactly the tails we trade.
    """
    from math import floor

    return int(floor(value + 0.5)) if value >= 0 else -int(floor(-value + 0.5))


def _day_of(obs: Observation, boundary: DayBoundary) -> date:
    if boundary is DayBoundary.LOCAL:
        return obs.valid_utc.astimezone(LOCAL_TZ).date()
    return obs.valid_utc.astimezone(UTC).date()


def daily_maxima(
    observations: list[Observation],
    strategy: Strategy | None = None,
) -> dict[date, DailyMax]:
    """Group observations into days and reduce each to its settled maximum.

    Returns a mapping keyed by the calendar day implied by `strategy.boundary`.
    Days with no valid temperature reading are omitted rather than reported as
    zero, so callers can distinguish "cold" from "no data".
    """
    strategy = strategy or Strategy()

    buckets: dict[date, list[Observation]] = {}
    for obs in observations:
        if obs.tmpc is None:
            continue
        if strategy.reports is ReportFilter.ROUTINE_ONLY and obs.is_special:
            continue
        buckets.setdefault(_day_of(obs, strategy.boundary), []).append(obs)

    result: dict[date, DailyMax] = {}
    for day, day_obs in buckets.items():
        day_obs.sort(key=lambda o: o.valid_utc)
        peak = max(day_obs, key=lambda o: (_apply_rounding(o.tmpc, strategy.rounding), -o.valid_utc.timestamp()))
        result[day] = DailyMax(
            day=day,
            tmax_c=_apply_rounding(peak.tmpc, strategy.rounding),
            n_obs=len(day_obs),
            argmax_utc=peak.valid_utc,
            first_obs_utc=day_obs[0].valid_utc,
            last_obs_utc=day_obs[-1].valid_utc,
        )

    return result


def resolve(
    observations: list[Observation],
    day: date,
    strategy: Strategy | None = None,
) -> int | None:
    """Settlement value for a single day, or None if there is no data."""
    result = daily_maxima(observations, strategy).get(day)
    return result.tmax_c if result else None
