"""Open-Meteo clients: ensemble forecasts and the archived-forecast backfill.

Free, no API key, CC-BY licensed. Two endpoints matter, and the difference
between them decides the whole data strategy:

  Ensemble API  (ensemble-api.open-meteo.com)
      Full ensembles -- 51 ECMWF members, 31 GFS, and so on. This is what the
      distributional model needs. **It has essentially no history**: asking for
      past_days=60 returns 60 days of timestamps but only ~4 days of non-null
      values. Verified empirically, 2026-08-01.

  Historical Forecast API  (historical-forecast-api.open-meteo.com)
      Archived *deterministic* forecasts -- what the model said at the time,
      not what actually happened, so it is safe to train on. Verified depth for
      EGLC: ecmwf_ifs025 begins ~March 2024 (2024-01-01 empty, 2024-03-01 full);
      best_match and gfs_seamless reach back to at least 2022-06-01.

The consequence: ensemble training data can only be accumulated going forward,
one day at a time, and any day not harvested is lost permanently. Deterministic
history can be backfilled today. So we do both -- backfill deterministic to get
a model running now, and harvest ensembles daily to build the archive the real
model will need.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from weatherbot.config import STATION_LAT, STATION_LON

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class EnsembleModel:
    """An ensemble model as exposed by Open-Meteo.

    `members` and `horizon_days` are the values actually observed for EGLC on
    2026-08-01, not the vendor's headline numbers -- Open-Meteo truncates some
    feeds. They are recorded so the harvester can warn when a feed changes
    shape underneath us.
    """

    model_id: str
    members: int
    horizon_days: float
    note: str = ""


# Verified against the live API for EGLC on 2026-08-01.
ENSEMBLE_MODELS: tuple[EnsembleModel, ...] = (
    EnsembleModel("ecmwf_ifs025", 51, 15.0, "ECMWF IFS ENS. Primary model."),
    EnsembleModel("ecmwf_aifs025", 51, 15.0, "ECMWF AIFS ENS (ML). Independent of IFS."),
    EnsembleModel("gfs025", 31, 10.4, "NCEP GEFS."),
    EnsembleModel("icon_eu", 40, 5.3, "DWD ICON-EU EPS. Regional, short range."),
    EnsembleModel("icon_d2", 20, 2.0, "DWD ICON-D2 EPS. 2.2km but London is near the domain edge."),
    EnsembleModel("gem_global", 21, 15.0, "Environment Canada GEPS."),
    EnsembleModel("ukmo_global_ensemble_20km", 18, 10.3, "MOGREPS-G."),
    EnsembleModel(
        "ukmo_uk_ensemble_2km",
        3,
        5.6,
        "MOGREPS-UK, but Open-Meteo exposes only 2 perturbed members plus the "
        "mean. Real MOGREPS-UK has ~18. Use the Met Office DataHub for the "
        "full ensemble; this is a fallback only.",
    ),
)

# Ensemble requests return every variable for every member, so each extra
# variable multiplies payload size by the member count. Temperature is the
# only one we need at member resolution -- everything else is a feature and is
# fetched once from the deterministic endpoint instead.
ENSEMBLE_VARIABLES: tuple[str, ...] = ("temperature_2m",)

DETERMINISTIC_VARIABLES: tuple[str, ...] = (
    "temperature_2m",
    "dew_point_2m",
    "relative_humidity_2m",
    "cloud_cover",
    "wind_speed_10m",
    "wind_direction_10m",
    "shortwave_radiation",
    "pressure_msl",
)

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
_SESSION.headers.update({"Accept": "application/json"})
_SESSION.mount("https://", HTTPAdapter(max_retries=_RETRY, pool_maxsize=4))


class OpenMeteoError(RuntimeError):
    """Raised when Open-Meteo cannot be reached or returns an error payload."""


def _get(url: str, params: dict, timeout: int = 90) -> dict:
    try:
        response = _SESSION.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise OpenMeteoError(f"request to {url} failed: {exc}") from exc

    if response.status_code != 200:
        reason = ""
        try:
            reason = response.json().get("reason", "")
        except ValueError:
            reason = response.text[:200]
        raise OpenMeteoError(f"{url} returned HTTP {response.status_code}: {reason}")

    payload = response.json()
    if payload.get("error"):
        raise OpenMeteoError(f"{url} error: {payload.get('reason')}")
    return payload


def fetch_ensemble(
    model: str,
    *,
    latitude: float = STATION_LAT,
    longitude: float = STATION_LON,
    forecast_days: int = 7,
    variables: tuple[str, ...] = ENSEMBLE_VARIABLES,
    timezone: str = "Europe/London",
) -> dict:
    """Fetch a full ensemble forecast for one model.

    Returns the raw Open-Meteo payload unmodified. The harvester stores it
    verbatim -- a lossy transform applied today cannot be undone tomorrow, and
    this data is unreplayable.
    """
    return _get(
        ENSEMBLE_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(variables),
            "models": model,
            "forecast_days": forecast_days,
            "timezone": timezone,
        },
    )


def fetch_deterministic(
    *,
    latitude: float = STATION_LAT,
    longitude: float = STATION_LON,
    forecast_days: int = 7,
    variables: tuple[str, ...] = DETERMINISTIC_VARIABLES,
    models: str = "best_match",
    timezone: str = "Europe/London",
) -> dict:
    """Fetch a single-valued forecast carrying the auxiliary feature set."""
    return _get(
        FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(variables),
            "models": models,
            "forecast_days": forecast_days,
            "timezone": timezone,
        },
    )


def fetch_historical_forecast(
    start: date,
    end: date,
    *,
    model: str = "ecmwf_ifs025",
    latitude: float = STATION_LAT,
    longitude: float = STATION_LON,
    variables: tuple[str, ...] = DETERMINISTIC_VARIABLES,
    timezone: str = "Europe/London",
) -> dict:
    """Fetch archived deterministic forecasts for a past date range.

    This is what the model predicted at the time, so it is legitimate training
    data. It is NOT reanalysis -- do not substitute ERA5 here, which is built
    from future observations and would leak.
    """
    return _get(
        HISTORICAL_FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(variables),
            "models": model,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "timezone": timezone,
        },
    )


def lead_variables(variable: str, leads: tuple[int, ...]) -> list[str]:
    """Build the variable list for a base variable plus previous-run leads.

    Open-Meteo exposes `<var>_previous_dayN`: the value for the same valid time
    as forecast by the run issued N days earlier. This is what makes an honest
    training set possible -- a model trained on lead-0 data would be wildly
    overconfident at the 2-day leads these markets are actually traded at.

    Verified working on the Historical Forecast API, not just the live
    previous-runs endpoint, so it backfills.
    """
    names = [variable]
    names.extend(f"{variable}_previous_day{lead}" for lead in leads)
    return names


def fetch_historical_forecast_with_leads(
    start: date,
    end: date,
    *,
    model: str = "ecmwf_ifs025",
    variable: str = "temperature_2m",
    leads: tuple[int, ...] = (1, 2, 3, 4, 5),
    aux_variables: tuple[str, ...] = (),
    latitude: float = STATION_LAT,
    longitude: float = STATION_LON,
    timezone: str = "Europe/London",
) -> dict:
    """Archived forecasts for a past range, resolved by lead time."""
    variables = lead_variables(variable, leads) + list(aux_variables)
    return _get(
        HISTORICAL_FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(variables),
            "models": model,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "timezone": timezone,
        },
    )


@dataclass
class MemberSeries:
    """Ensemble members extracted from a payload, keyed by member name."""

    times: list[str]
    members: dict[str, list[float | None]] = field(default_factory=dict)

    @property
    def n_members(self) -> int:
        return len(self.members)


def extract_members(payload: dict, variable: str = "temperature_2m") -> MemberSeries:
    """Pull the per-member series for one variable out of a raw payload.

    Open-Meteo names the unperturbed series `temperature_2m` and the perturbed
    ones `temperature_2m_memberNN`. Series that are entirely null are dropped:
    some feeds advertise members they do not actually populate.
    """
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []

    members: dict[str, list[float | None]] = {}
    for key, series in hourly.items():
        if key == "time" or not key.startswith(variable):
            continue
        if any(value is not None for value in series):
            members[key] = series

    return MemberSeries(times=times, members=members)


def polite_sleep(seconds: float = 0.5) -> None:
    """Small delay between successive model requests."""
    if seconds > 0:
        time.sleep(seconds)
