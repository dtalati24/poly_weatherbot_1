"""Assemble modelling tables from the archive.

Turns archived forecast records into `{date: forecast daily maximum}` keyed by
model and lead time, ready to pair with observed settlement values.

Two properties of the archive make this straightforward and are worth stating,
because getting either wrong would silently corrupt the training set:

  - **Times are already local.** Records are requested with
    `timezone=Europe/London`, so the `time` strings are local wall-clock and
    group directly onto the local calendar day the market settles on. No
    conversion, and no DST handling, is required here.
  - **Lead time is explicit.** `temperature_2m_previous_dayN` is the forecast
    for the same valid hour as issued N days earlier. Training on the lead you
    actually trade is the difference between an honest model and one that
    backtests brilliantly and loses money live.

Chunks overlap at their boundaries, so hourly values are merged on timestamp
before the daily maximum is taken rather than being maxed chunk-by-chunk.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from weatherbot.config import ARCHIVE_DIR
from weatherbot.harvest import read_record

BACKFILL_DIR = ARCHIVE_DIR / "backfill"

BASE_VARIABLE = "temperature_2m"


def lead_variable(lead: int) -> str:
    """Series name for a lead time in days. Lead 0 is the base series."""
    if lead < 0:
        raise ValueError(f"lead must be non-negative, got {lead}")
    return BASE_VARIABLE if lead == 0 else f"{BASE_VARIABLE}_previous_day{lead}"


def available_models(root: Path | None = None) -> list[str]:
    root = root or BACKFILL_DIR
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def load_hourly(
    model: str, lead: int, *, root: Path | None = None
) -> dict[str, float]:
    """Merge every archived chunk for a model into {local timestamp: value}."""
    root = root or BACKFILL_DIR
    directory = root / model
    if not directory.exists():
        raise FileNotFoundError(f"no backfill archived for model {model!r}")

    variable = lead_variable(lead)
    merged: dict[str, float] = {}

    for path in sorted(directory.glob("*.json.gz")):
        hourly = (read_record(path).get("payload") or {}).get("hourly") or {}
        times = hourly.get("time") or []
        series = hourly.get(variable)
        if series is None:
            raise KeyError(
                f"{path.name} has no series {variable!r}; "
                f"available: {sorted(k for k in hourly if k != 'time')}"
            )
        for timestamp, value in zip(times, series):
            if value is not None:
                merged[timestamp] = float(value)

    return merged


def daily_maxima_from_hourly(hourly: dict[str, float]) -> dict[date, float]:
    """Reduce local hourly values to a maximum per local calendar day."""
    by_day: dict[date, float] = {}
    for timestamp, value in hourly.items():
        day = datetime.fromisoformat(timestamp).date()
        current = by_day.get(day)
        if current is None or value > current:
            by_day[day] = value
    return by_day


def daily_completeness(hourly: dict[str, float]) -> dict[date, int]:
    """Hours present per local day, so short days can be screened out.

    A day with only a handful of forecast hours would produce a maximum that is
    too low for reasons that have nothing to do with the weather.
    """
    counts: dict[date, int] = defaultdict(int)
    for timestamp in hourly:
        counts[datetime.fromisoformat(timestamp).date()] += 1
    return dict(counts)


def forecast_daily_max(
    model: str,
    lead: int,
    *,
    root: Path | None = None,
    min_hours: int = 20,
) -> dict[date, float]:
    """`{date: forecast daily maximum}` for one model and lead time.

    Days with fewer than `min_hours` forecast hours are dropped.
    """
    hourly = load_hourly(model, lead, root=root)
    maxima = daily_maxima_from_hourly(hourly)
    counts = daily_completeness(hourly)
    return {day: value for day, value in maxima.items() if counts.get(day, 0) >= min_hours}


def remaining_forecast_max(
    hourly: dict[str, float], day: date, from_hour: float
) -> float | None:
    """Highest forecast hourly temperature still to come on `day`.

    The intraday counterpart to `forecast_daily_max`: once part of the day has
    happened, the forecast for the hours *already past* is no longer a
    prediction and including it would double-count what the running maximum
    already knows.

    Timestamps in the archive are local wall-clock (see the module docstring),
    so the hour in the key is directly comparable to a London-local hour.
    Returns None when no forecast hours remain.
    """
    prefix = day.isoformat()
    values = [
        value
        for stamp, value in hourly.items()
        if stamp.startswith(prefix) and int(stamp[11:13]) >= from_hour
    ]
    return max(values) if values else None


def build_training_pairs(
    forecasts: dict[date, float],
    observations: dict[date, int],
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[tuple[float, int]]:
    """Align forecasts to observed settlement values.

    `end` is exclusive and should be set to the start of any evaluation window;
    including evaluation days here is leakage.
    """
    pairs: list[tuple[float, int]] = []
    for day, forecast in sorted(forecasts.items()):
        if start is not None and day < start:
            continue
        if end is not None and day >= end:
            continue
        observed = observations.get(day)
        if observed is not None:
            pairs.append((forecast, observed))
    return pairs
