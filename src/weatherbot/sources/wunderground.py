"""Weather Underground historical observations -- the settlement source.

This is the source Polymarket actually resolves against, so it is the one the
model must be trained and validated on.

Why this module exists rather than just using raw METARs:

    Weather Underground does not always ingest every METAR the station issues.
    Over 1 Apr - 31 Jul 2026 (122 days) WU's local-day maximum for EGLC differed
    from the raw-METAR maximum on 2 days, both times with WU *lower*:

        2026-05-12  WU 16C  vs raw 17C   (WU missing the 13:50Z obs, the daily peak)
        2026-05-27  WU 24C  vs raw 25C   (WU missing the 23:20Z obs)

    Both settled at WU's value. A raw-METAR-derived maximum is therefore too
    high on roughly 1-2% of days -- and since a single degree is a whole market
    bucket, that is a live source of both mispricing and risk.

Day grouping is confirmed to be LOCAL calendar day: requesting a date returns
the block running 00:20-23:50 in the station's local time, shifting by an hour
between GMT and BST.

The endpoint is the public JSON API backing wunderground.com's History page.
The key below is the one embedded in the WU site itself, not a private
credential. It may rotate without notice, which is why IEM is retained as the
robust long-history archive -- see sources/iem.py.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from weatherbot.config import UTC, WU_API_KEY, WU_HISTORY_URL, WU_STATION_KEY
from weatherbot.observation import Observation

# WU throttles aggressively and will hang the connection rather than return a
# clean 429. Retries cover read timeouts as well as status codes, and a browser
# User-Agent avoids the harsher limits applied to the default requests UA.
_RETRY = Retry(
    total=5,
    connect=5,
    read=5,
    status=5,
    backoff_factor=1.5,
    status_forcelist=(408, 429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    raise_on_status=False,
)

_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
)
_SESSION.mount("https://", HTTPAdapter(max_retries=_RETRY, pool_maxsize=4))


def _cache_path(station_key: str, day: date) -> Path:
    from weatherbot.config import OBS_DIR

    safe = station_key.replace(":", "_")
    return OBS_DIR / "wunderground" / safe / f"{day.isoformat()}.json"


def fetch_day_raw(
    day: date,
    *,
    station_key: str = WU_STATION_KEY,
    use_cache: bool = True,
    timeout: int = 60,
) -> dict:
    """Fetch and cache WU's raw observation block for one local calendar day.

    Raises on persistent failure rather than returning a partial result: a
    silently gappy observation set would corrupt everything downstream. Each
    day is cached independently, so an interrupted backfill resumes cheaply.
    """
    path = _cache_path(station_key, day)
    if use_cache and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    try:
        response = _SESSION.get(
            WU_HISTORY_URL.format(station_key=station_key),
            params={
                "apiKey": WU_API_KEY,
                "units": "m",  # metric: temperatures in degrees Celsius
                "startDate": day.strftime("%Y%m%d"),
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Weather Underground fetch failed for {day} after retries: {exc}. "
            f"Re-run to resume from cache."
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def parse_block(payload: dict, station: str = "EGLC") -> list[Observation]:
    """Convert a WU observation block into Observations."""
    out: list[Observation] = []
    for record in payload.get("observations") or []:
        epoch = record.get("valid_time_gmt")
        if epoch is None:
            continue
        out.append(
            Observation(
                station=station,
                valid_utc=datetime.fromtimestamp(int(epoch), tz=UTC),
                tmpc=_as_float(record.get("temp")),
                dwpc=_as_float(record.get("dewPt")),
                drct=_as_float(record.get("wdir")),
                sknt=None,  # metric feed is km/h; see Observation.sknt
                gust=_as_float(record.get("gust")),
                skyc1=record.get("clds"),
                vsby=_as_float(record.get("vis")),
                metar=None,
                source="wunderground",
            )
        )
    out.sort(key=lambda o: o.valid_utc)
    return out


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_observations(
    station: str,
    start: date,
    end: date,
    *,
    use_cache: bool = True,
    polite_delay: float = 0.5,
    station_key: str = WU_STATION_KEY,
    progress_every: int = 100,
) -> list[Observation]:
    """Fetch observations for [start, end), deduplicated by timestamp.

    Signature matches sources.iem.fetch_observations so the two are drop-in
    interchangeable. Because WU returns whole local days, adjacent day blocks
    overlap slightly; duplicates are collapsed on timestamp.
    """
    if end <= start:
        raise ValueError(f"end ({end}) must be after start ({start})")

    merged: dict[datetime, Observation] = {}
    day = start
    n_days = (end - start).days
    fetched_from_network = False
    for index in range(n_days):
        path = _cache_path(station_key, day)
        cached = path.exists()
        # Only rate-limit actual network calls; cached replays run at full speed.
        if fetched_from_network and not cached and polite_delay:
            time.sleep(polite_delay)
        payload = fetch_day_raw(day, station_key=station_key, use_cache=use_cache)
        if not cached:
            fetched_from_network = True
        for obs in parse_block(payload, station=station):
            merged.setdefault(obs.valid_utc, obs)
        if progress_every and index and index % progress_every == 0:
            print(f"    ... {index}/{n_days} days", flush=True)
        day += timedelta(days=1)

    return sorted(merged.values(), key=lambda o: o.valid_utc)
