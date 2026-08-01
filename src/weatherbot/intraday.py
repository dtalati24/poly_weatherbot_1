"""Intraday state: what we know about today's maximum, part-way through today.

The settled value is a maximum over the whole London-local day, so at any
instant T the maximum of everything observed so far is a **lower bound** on it:

    Y = max(M_T, max of everything still to come)     =>    Y >= M_T

That single inequality is what makes an intraday model different in kind from a
forecast model. A forecast is a guess about all of Y; M_T is a fact about part
of it, and the part it fixes grows monotonically through the day until, at some
point, it fixes all of it.

Two disciplines this module exists to enforce:

**The running maximum must use the settlement operator, not an approximation.**
It is computed through `resolve`'s own reduction, so the rounding rule, the day
boundary and the SPECI filter are by construction the same ones Phase 0
validated. A running maximum computed even slightly differently from the settled
value would produce a bound that is wrong in exactly the cases that matter.

**Nothing may be read from after the as-of instant.** `running_max` filters on
`valid_utc <= as_of`, and every caller passes the instant it is pretending to
stand at. This is the leakage surface for the whole model: an observation
included one minute early would let the model see the peak it is supposed to be
predicting.

Note that `as_of` is compared against the observation's *valid* time, which is
not the same as the time the observation became available to us. METARs are
transmitted with a lag, so a live system must apply an additional delay on top
of this. Backtests using this module are therefore mildly optimistic about
timeliness, and that is stated rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from weatherbot.config import LOCAL_TZ, UTC
from weatherbot.observation import Observation

# Imported deliberately rather than reimplemented: the running maximum has to be
# the same operator as the settled value, and a second implementation would be
# free to drift from it.
from weatherbot.resolve import ReportFilter, Strategy, _apply_rounding, _day_of


@dataclass(frozen=True)
class IntradayState:
    """What is known about `day` as of a given instant."""

    day: date
    as_of_utc: datetime
    running_max: int | None
    n_obs: int
    latest_tmpc: float | None
    latest_obs_utc: datetime | None

    @property
    def local_hour(self) -> float:
        """Hours elapsed in the London-local day. 0.0 at local midnight."""
        local = self.as_of_utc.astimezone(LOCAL_TZ)
        return local.hour + local.minute / 60.0

    @property
    def is_stale(self) -> bool:
        """No observation in the last 90 minutes, so the bound may have moved."""
        if self.latest_obs_utc is None:
            return True
        return (self.as_of_utc - self.latest_obs_utc) > timedelta(minutes=90)


def local_midnight(day: date) -> datetime:
    """The instant at which `day` begins in London, as UTC.

    Returned in UTC rather than as a London wall-clock datetime on purpose.
    Subtracting two aware datetimes that share a `tzinfo` object makes Python
    skip the inter-zone adjustment and do naive wall-clock arithmetic, so a pair
    of "London midnights" spanning a DST change appear exactly 24 hours apart
    when they are really 23 or 25. Handing back UTC removes that trap from every
    caller at once.
    """
    return datetime.combine(day, time.min).replace(tzinfo=LOCAL_TZ).astimezone(UTC)


def local_instant(day: date, hour: float) -> datetime:
    """The instant `hour` hours into the London-local `day`, as UTC.

    This is elapsed time from local midnight, not a local wall-clock reading,
    and the difference is only visible on the two DST days a year. Elapsed time
    is the right choice here because `hour` indexes the lock-in curve, which is
    about how much of the day has gone -- and because the alternative can be
    asked for an hour that does not exist. On the spring-forward day 01:00 local
    never happens; asking for wall-clock 01:00 has no answer, whereas asking for
    "one hour into the day" always does.
    """
    return local_midnight(day) + timedelta(hours=hour)


def group_by_local_day(
    observations: list[Observation], strategy: Strategy | None = None
) -> dict[date, list[Observation]]:
    """Bucket observations onto the day they settle against, sorted in time."""
    strategy = strategy or Strategy()
    out: dict[date, list[Observation]] = {}
    for obs in observations:
        if obs.tmpc is None:
            continue
        out.setdefault(_day_of(obs, strategy.boundary), []).append(obs)
    for day_obs in out.values():
        day_obs.sort(key=lambda o: o.valid_utc)
    return out


def state_at(
    day_observations: list[Observation],
    day: date,
    as_of_utc: datetime,
    strategy: Strategy | None = None,
) -> IntradayState:
    """Everything known about `day` at `as_of_utc`, and nothing after it."""
    strategy = strategy or Strategy()

    seen = [
        obs
        for obs in day_observations
        if obs.valid_utc <= as_of_utc
        and obs.tmpc is not None
        and not (strategy.reports is ReportFilter.ROUTINE_ONLY and obs.is_special)
    ]
    if not seen:
        return IntradayState(day, as_of_utc, None, 0, None, None)

    latest = max(seen, key=lambda o: o.valid_utc)
    return IntradayState(
        day=day,
        as_of_utc=as_of_utc,
        running_max=max(_apply_rounding(o.tmpc, strategy.rounding) for o in seen),
        n_obs=len(seen),
        latest_tmpc=latest.tmpc,
        latest_obs_utc=latest.valid_utc,
    )


def states_through_day(
    day_observations: list[Observation],
    day: date,
    hours: tuple[float, ...] = tuple(range(24)),
    strategy: Strategy | None = None,
) -> list[IntradayState]:
    """The intraday state at each of `hours` through the local day."""
    return [
        state_at(day_observations, day, local_instant(day, h), strategy) for h in hours
    ]


def remaining_max(
    day_observations: list[Observation],
    as_of_utc: datetime,
    strategy: Strategy | None = None,
) -> int | None:
    """Settled-operator maximum over observations strictly AFTER `as_of_utc`.

    The complement of `state_at().running_max`. Together the two partition the
    day, so `max(running_max, remaining_max)` reconstructs the settled value
    exactly -- which is what makes `Y = max(M, X)` an identity rather than an
    approximation. Returns None when the day is already over.
    """
    strategy = strategy or Strategy()
    later = [
        obs
        for obs in day_observations
        if obs.valid_utc > as_of_utc
        and obs.tmpc is not None
        and not (strategy.reports is ReportFilter.ROUTINE_ONLY and obs.is_special)
    ]
    if not later:
        return None
    return max(_apply_rounding(o.tmpc, strategy.rounding) for o in later)


def build_remaining_max_samples(
    by_day: dict[date, list[Observation]],
    hourly_forecast_remaining: "dict[tuple[date, float], float]",
    hours: tuple[float, ...],
    *,
    start: date | None = None,
    end: date | None = None,
    strategy: Strategy | None = None,
) -> list[tuple[float, float, int]]:
    """Assemble (local hour, forecast remaining max, actual remaining max) rows.

    `end` is exclusive -- the leakage guard. Rows are produced only where both
    a forecast for the remaining window and at least one later observation
    exist, so a day that has already finished contributes nothing rather than
    contributing a degenerate row.
    """
    rows: list[tuple[float, float, int]] = []

    for day, day_obs in by_day.items():
        if start is not None and day < start:
            continue
        if end is not None and day >= end:
            continue
        for hour in hours:
            forecast = hourly_forecast_remaining.get((day, hour))
            if forecast is None:
                continue
            actual = remaining_max(day_obs, local_instant(day, hour), strategy)
            if actual is None:
                continue
            rows.append((hour, forecast, actual))

    return rows


def build_nowcast_samples(
    by_day: dict[date, list[Observation]],
    settled: dict[date, int],
    hours: tuple[float, ...],
    *,
    start: date | None = None,
    end: date | None = None,
    forecasts: dict[date, float] | None = None,
    strategy: Strategy | None = None,
    clip_negative: bool = True,
) -> list[tuple[float, float, int]]:
    """Assemble (local hour, forecast gap, remaining rise) training rows.

    `end` is exclusive, which is the leakage guard: passing the first day of the
    evaluation window guarantees no evaluation day contributed to the fit.

    Days whose running maximum already exceeds the settled value produce a
    negative rise. That is not a bug in the caller -- it happens when the
    settlement feed dropped the observation carrying the peak. With
    `clip_negative` those rows are clamped to 0, which keeps them in the sample
    as evidence that the day locked early, rather than discarding real days or
    letting a negative rise corrupt the distribution.

    When `forecasts` is omitted the gap is reported as 0.0 for every row, so the
    same rows can feed the unconditional model.
    """
    rows: list[tuple[float, float, int]] = []

    for day, day_obs in by_day.items():
        if start is not None and day < start:
            continue
        if end is not None and day >= end:
            continue
        truth = settled.get(day)
        if truth is None:
            continue
        forecast = forecasts.get(day) if forecasts else None
        if forecasts is not None and forecast is None:
            continue

        for hour in hours:
            state = state_at(day_obs, day, local_instant(day, hour), strategy)
            if state.running_max is None:
                continue
            rise = truth - state.running_max
            if rise < 0:
                if not clip_negative:
                    continue
                rise = 0
            gap = 0.0 if forecast is None else state.running_max - forecast
            rows.append((hour, gap, rise))

    return rows
