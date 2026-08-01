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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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

__all__ = [
    "Observation",
    "fetch_observations",
    "fetch_raw_csv",
    "fetch_year",
    "parse_csv",
]

# IEM returns 429 under sustained use. Backoff is generous because a long
# backfill is not urgent, whereas being blocked mid-run is disruptive.
_RETRY = Retry(
    total=6,
    connect=6,
    read=6,
    status=6,
    backoff_factor=3.0,
    status_forcelist=(408, 429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    respect_retry_after_header=True,
    raise_on_status=False,
)

_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(max_retries=_RETRY, pool_maxsize=2))


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

    response = _SESSION.get(IEM_ASOS_URL, params=params, timeout=timeout)
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


# How stale the current year's cache may get before it is refetched. Past years
# are immutable and cached forever; the current year keeps growing.
CURRENT_YEAR_MAX_AGE_SECONDS = 6 * 3600


def fetch_year(
    station: str, year: int, *, use_cache: bool = True, retries: int = 4
) -> list[Observation]:
    """Fetch one whole calendar year, cached under a year-keyed filename.

    Chunking on calendar years rather than on the caller's requested range is
    deliberate. Caching by arbitrary chunk boundaries means a query starting one
    day earlier misses every cached file and re-downloads the entire history --
    which is exactly how this hit an IEM rate limit. Year keys make the cache
    reusable across any requested range.

    The current year is refetched once its cache goes stale, since it is still
    being appended to; completed years are treated as immutable.
    """
    path = OBS_DIR / f"{station}_{year}.csv"

    if use_cache and path.exists():
        is_current = year >= date.today().year
        age = time.time() - path.stat().st_mtime if is_current else 0.0
        if not is_current or age < CURRENT_YEAR_MAX_AGE_SECONDS:
            return parse_csv(path.read_text(encoding="utf-8"))

    # urllib3's Retry cannot recover a connection reset that happens mid-body,
    # which is how IEM fails under load, so retry at the application level too.
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            text = fetch_raw_csv(
                station, date(year, 1, 1), date(year + 1, 1, 1), use_cache=False
            )
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt == retries - 1:
                if path.exists():
                    # Prefer stale data over no data.
                    return parse_csv(path.read_text(encoding="utf-8"))
                raise
            time.sleep(5.0 * (attempt + 1))
    else:  # pragma: no cover - loop always breaks or raises
        raise RuntimeError(f"unreachable: {last_error}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return parse_csv(text)


def fetch_observations(
    station: str,
    start: date,
    end: date,
    *,
    use_cache: bool = True,
    polite_delay: float = 2.0,
) -> list[Observation]:
    """Fetch observations for [start, end), assembled from year-sized caches."""
    if end <= start:
        raise ValueError(f"end ({end}) must be after start ({start})")

    all_obs: list[Observation] = []
    seen: set[tuple[str, datetime]] = set()

    for index, year in enumerate(range(start.year, end.year + 1)):
        path = OBS_DIR / f"{station}_{year}.csv"
        # Only rate-limit real network calls.
        if index and polite_delay and not path.exists():
            time.sleep(polite_delay)
        for obs in fetch_year(station, year, use_cache=use_cache):
            if not (start <= obs.valid_utc.date() < end):
                continue
            key = (obs.station, obs.valid_utc)
            if key not in seen:
                seen.add(key)
                all_obs.append(obs)

    all_obs.sort(key=lambda o: o.valid_utc)
    return all_obs
