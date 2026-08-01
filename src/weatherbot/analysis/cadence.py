"""Observation cadence and truncation analysis (PLAN.md checklist 0.2 / 0.3).

The settlement variable is a maximum over a *discrete, finite* set of
observations. If EGLC stops reporting before the day's thermal peak -- for
instance during the airport's weekend noise curfew -- then the settled value is
systematically below the true daily high on those days. That would be a large
and durable edge, so it is measured rather than assumed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from weatherbot.config import LOCAL_TZ
from weatherbot.observation import Observation
from weatherbot.resolve import DailyMax

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Typical window in which a London daily maximum occurs. Used only to flag days
# whose observation record ends suspiciously early.
PEAK_WINDOW_START_HOUR = 12
PEAK_WINDOW_END_HOUR = 17


@dataclass(frozen=True)
class WeekdayCadence:
    """Cadence statistics for one day of the week."""

    weekday: int
    n_days: int
    mean_obs_per_day: float
    min_obs_per_day: int
    earliest_obs_hour: float
    latest_obs_hour: float
    mean_latest_obs_hour: float
    days_ending_before_peak: int

    @property
    def name(self) -> str:
        return WEEKDAY_NAMES[self.weekday]

    @property
    def truncation_rate(self) -> float:
        return self.days_ending_before_peak / self.n_days if self.n_days else 0.0


def _local_hour(dt) -> float:
    local = dt.astimezone(LOCAL_TZ)
    return local.hour + local.minute / 60.0


def group_by_local_day(
    observations: list[Observation],
) -> dict[date, list[Observation]]:
    """Bucket observations by London-local calendar day."""
    days: dict[date, list[Observation]] = defaultdict(list)
    for obs in observations:
        days[obs.valid_utc.astimezone(LOCAL_TZ).date()].append(obs)
    for day_obs in days.values():
        day_obs.sort(key=lambda o: o.valid_utc)
    return dict(days)


def weekday_cadence(observations: list[Observation]) -> list[WeekdayCadence]:
    """Per-weekday reporting statistics.

    A materially lower `latest_obs_hour` or higher `truncation_rate` on any
    weekday is the signal we are looking for.
    """
    days = group_by_local_day(observations)

    per_weekday: dict[int, list[list[Observation]]] = defaultdict(list)
    for day, day_obs in days.items():
        per_weekday[day.weekday()].append(day_obs)

    out: list[WeekdayCadence] = []
    for weekday in range(7):
        day_groups = per_weekday.get(weekday, [])
        if not day_groups:
            continue

        counts = [len(g) for g in day_groups]
        first_hours = [_local_hour(g[0].valid_utc) for g in day_groups]
        last_hours = [_local_hour(g[-1].valid_utc) for g in day_groups]
        truncated = sum(1 for h in last_hours if h < PEAK_WINDOW_END_HOUR)

        out.append(
            WeekdayCadence(
                weekday=weekday,
                n_days=len(day_groups),
                mean_obs_per_day=sum(counts) / len(counts),
                min_obs_per_day=min(counts),
                earliest_obs_hour=min(first_hours),
                latest_obs_hour=max(last_hours),
                mean_latest_obs_hour=sum(last_hours) / len(last_hours),
                days_ending_before_peak=truncated,
            )
        )
    return out


def hourly_coverage(observations: list[Observation]) -> dict[int, dict[int, int]]:
    """Count of observations per (weekday, local hour).

    Zeros in the afternoon columns for a given weekday would confirm a curfew
    gap that truncates the daily maximum.
    """
    coverage: dict[int, dict[int, int]] = {
        wd: {hour: 0 for hour in range(24)} for wd in range(7)
    }
    for obs in observations:
        local = obs.valid_utc.astimezone(LOCAL_TZ)
        coverage[local.weekday()][local.hour] += 1
    return coverage


def argmax_hour_histogram(maxima: dict[date, DailyMax]) -> dict[int, int]:
    """Distribution of the local hour at which the daily maximum occurred."""
    histogram: dict[int, int] = defaultdict(int)
    for daily in maxima.values():
        histogram[daily.argmax_local.hour] += 1
    return dict(sorted(histogram.items()))


def peak_at_last_observation(maxima: dict[date, DailyMax]) -> list[date]:
    """Days whose maximum occurred at the final observation of the day.

    These are the days most likely to have been truncated: the temperature was
    still at its peak when reporting stopped, so the true high may be higher.
    """
    return sorted(
        day
        for day, daily in maxima.items()
        if daily.argmax_utc == daily.last_obs_utc
        and _local_hour(daily.last_obs_utc) < PEAK_WINDOW_END_HOUR
    )
