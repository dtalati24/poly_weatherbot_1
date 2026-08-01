"""Iowa Environmental Mesonet (IEM) ASOS/METAR archive client.

IEM mirrors global METAR observations including EGLC, free and without a key.
This is our ground-truth source for the settlement variable.

Observed EGLC characteristics (verified 2026-08-01):
  - Reports half-hourly at :20 and :50 past the hour
  - Reports through the night (no overnight gap on weekdays)
  - tmpc is integer-valued, because METAR encodes temperature in whole degrees
    Celsius natively. There is therefore no "round each observation" step for
    this station -- the rounding has already happened at the encoder.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from weatherbot.config import (
    IEM_ASOS_URL,
    IEM_FIELDS,
    OBS_DIR,
    REPORT_TYPE_ROUTINE,
    REPORT_TYPE_SPECIAL,
    UTC,
)
from weatherbot.observation import Observation

MISSING_TOKENS = {"M", "", "None", "null"}

__all__ = ["Observation", "fetch_observations", "fetch_raw_csv", "parse_csv"]


def _to_float(value: str | None) -> float | None:
    if value is None or value.strip() in MISSING_TOKENS:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_str(value: str | None) -> str | None:
    if value is None or value.strip() in MISSING_TOKENS:
        return None
    return value.strip()


def _cache_path(station: str, start: date, end: date) -> Path:
    return OBS_DIR / f"{station}_{start.isoformat()}_{end.isoformat()}.csv"


def fetch_raw_csv(
    station: str,
    start: date,
    end: date,
    *,
    use_cache: bool = True,
    timeout: int = 120,
) -> str:
    """Fetch the raw IEM CSV for a date range, caching to disk.

    `end` is exclusive on IEM's side, matching how the service treats the
    year2/month2/day2 parameters.
    """
    path = _cache_path(station, start, end)
    if use_cache and path.exists():
        return path.read_text(encoding="utf-8")

    params = {
        "station": station,
        "data": list(IEM_FIELDS),
        "year1": start.year,
        "month1": start.month,
        "day1": start.day,
        "year2": end.year,
        "month2": end.month,
        "day2": end.day,
        "tz": "Etc/UTC",
        "format": "onlycomma",
        "latlon": "no",
        "elev": "no",
        "missing": "M",
        "trace": "T",
        "direct": "no",
        "report_type": [REPORT_TYPE_ROUTINE, REPORT_TYPE_SPECIAL],
    }

    response = requests.get(IEM_ASOS_URL, params=params, timeout=timeout)
    response.raise_for_status()
    text = response.text

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def parse_csv(text: str) -> list[Observation]:
    """Parse an IEM CSV payload into Observations."""
    observations: list[Observation] = []
    reader = csv.DictReader(io.StringIO(text))

    for row in reader:
        raw_valid = (row.get("valid") or "").strip()
        if not raw_valid:
            continue
        try:
            valid = datetime.strptime(raw_valid, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            continue

        observations.append(
            Observation(
                station=(row.get("station") or "").strip(),
                valid_utc=valid,
                tmpc=_to_float(row.get("tmpc")),
                dwpc=_to_float(row.get("dwpc")),
                drct=_to_float(row.get("drct")),
                sknt=_to_float(row.get("sknt")),
                gust=_to_float(row.get("gust")),
                skyc1=_to_str(row.get("skyc1")),
                vsby=_to_float(row.get("vsby")),
                metar=_to_str(row.get("metar")),
                source="iem",
            )
        )

    observations.sort(key=lambda o: o.valid_utc)
    return observations


def fetch_observations(
    station: str,
    start: date,
    end: date,
    *,
    use_cache: bool = True,
    chunk_days: int = 366,
    polite_delay: float = 1.0,
) -> list[Observation]:
    """Fetch observations over an arbitrary range, chunking long requests.

    Long ranges are split so no single IEM request times out, and each chunk is
    cached independently so an interrupted backfill resumes cheaply.
    """
    if end <= start:
        raise ValueError(f"end ({end}) must be after start ({start})")

    all_obs: list[Observation] = []
    seen: set[tuple[str, datetime]] = set()

    cursor = start
    first = True
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=chunk_days), end)
        if not first and polite_delay:
            time.sleep(polite_delay)
        text = fetch_raw_csv(station, cursor, chunk_end, use_cache=use_cache)
        for obs in parse_csv(text):
            key = (obs.station, obs.valid_utc)
            if key not in seen:
                seen.add(key)
                all_obs.append(obs)
        cursor = chunk_end
        first = False

    all_obs.sort(key=lambda o: o.valid_utc)
    return all_obs
