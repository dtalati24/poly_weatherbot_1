"""Met Office IMPROVER spot percentiles — free, keyless, MOGREPS-UK informed.

MOGREPS-UK *gridded* data is a paid product on Weather DataHub. This is the
free route, and for our purpose it is arguably the better one.

The Met Office publishes its post-processed multi-model blend as spot values on
AWS Open Data, anonymously readable with no key and no requester-pays:

    bucket : met-office-uk-spot-percentiles   (eu-west-2)
    key    : uk-spot-percentiles/{YYYY}/{MM}/{DD}/T{HHMM}Z/
             {validTime}-B{blendTime}-{parameter}.nc

Verified directly on 2026-08-01:
  - `air_temperature`, shape (15 percentiles, 8667 sites), kelvin,
    cell_methods "time: maximum"
  - percentiles 5,10,15,20,25,30,40,50,60,70,75,80,85,90,95
  - mosg__model_configuration = "ecgl_ens gl_ens uk_ens" -- `uk_ens` is
    MOGREPS-UK, blended and calibrated in
  - title "IMPROVER Post-Processed Multi-Model Blend UK Spot Values"
  - nearest site to EGLC is 51.5048 / 0.0580, altitude 5 m, ~305 m away
  - updated every 15 minutes

So we get a calibrated predictive *distribution* of maximum temperature,
essentially at the resolving station, for free. That is a better starting point
than a raw ensemble, because IMPROVER has already done the bias correction and
blending we would otherwise have to learn.

!! IMPORTANT CAVEAT -- the window is not our target !!

    The `temperature_at_screen_level_max-PT12H` files are maxima over a
    12-hour window, NOT over the local calendar day. Verified from time_bnds:
    the file valid at 18:00Z covers 06:00Z-18:00Z, i.e. 07:00-19:00 local BST.

    Measured against 579 days of EGLC observations, the daily maximum falls
    inside 07:00-19:00 local on ~90% of days. On the other ~10% it does not --
    overwhelmingly the "just after local midnight" carryover case, which alone
    is 6.4% of days.

    Treat these percentiles as a strong FEATURE, never as the target. A model
    that equates them with the settlement variable will be wrong on one day in
    ten, and wrong in a biased direction (too low).

Storage note: the raw NetCDF is ~450 KB because it carries all 8667 sites. We
archive only the single-site extraction plus the source key, which keeps records
around 1 KB. This is a deliberate exception to the "store payloads verbatim"
rule -- archiving the full grid at this cadence is not viable, and the source
key makes the extraction reproducible while the object remains in the 30-day
window.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from weatherbot.config import STATION_LAT, STATION_LON

BUCKET = "met-office-uk-spot-percentiles"
BASE_URL = f"https://{BUCKET}.s3.eu-west-2.amazonaws.com/"
KEY_ROOT = "uk-spot-percentiles"
S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

TMAX_PARAMETER = "temperature_at_screen_level_max-PT12H"
TMIN_PARAMETER = "temperature_at_screen_level_min-PT12H"

_RETRY = Retry(
    total=4,
    connect=4,
    read=4,
    status=4,
    backoff_factor=1.5,
    status_forcelist=(408, 429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    raise_on_status=False,
)
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(max_retries=_RETRY, pool_maxsize=4))


class MetOfficeError(RuntimeError):
    """Raised when the bucket cannot be listed or an object cannot be read."""


@dataclass(frozen=True)
class SpotPercentiles:
    """A percentile distribution of max temperature at one site."""

    key: str
    parameter: str
    valid_time: str
    window_start: str
    window_end: str
    percentiles: tuple[float, ...]
    values_c: tuple[float, ...]
    site_latitude: float
    site_longitude: float
    site_altitude_m: float
    site_distance_m: float
    model_configuration: str

    def quantile(self, percentile: float) -> float | None:
        """Value at an exact published percentile, or None if not published."""
        for p, v in zip(self.percentiles, self.values_c):
            if abs(p - percentile) < 1e-9:
                return v
        return None

    @property
    def median_c(self) -> float | None:
        return self.quantile(50.0)


def _get(url: str, params: dict | None = None, timeout: int = 90) -> requests.Response:
    try:
        response = _SESSION.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise MetOfficeError(f"request to {url} failed: {exc}") from exc
    if response.status_code != 200:
        raise MetOfficeError(f"{url} returned HTTP {response.status_code}")
    return response


def list_runs(day: date) -> list[str]:
    """List the run prefixes (T{HHMM}Z) published for a day, oldest first.

    The stream updates every 15 minutes, so a day has up to 96 run prefixes.
    """
    prefixes: list[str] = []
    token: str | None = None

    while True:
        params = {
            "list-type": "2",
            "prefix": f"{KEY_ROOT}/{day:%Y/%m/%d}/",
            "delimiter": "/",
            "max-keys": "1000",
        }
        if token:
            params["continuation-token"] = token
        root = ET.fromstring(_get(BASE_URL, params).text)
        prefixes.extend(
            node.find("s3:Prefix", S3_NS).text
            for node in root.findall("s3:CommonPrefixes", S3_NS)
        )
        token_node = root.find("s3:NextContinuationToken", S3_NS)
        token = token_node.text if token_node is not None else None
        if not token:
            break

    return sorted(prefixes)


def list_parameter_keys(run_prefix: str, parameter: str = TMAX_PARAMETER) -> list[str]:
    """Every object key for one parameter within a run, sorted by valid time."""
    keys: list[str] = []
    token: str | None = None

    while True:
        params = {"list-type": "2", "prefix": run_prefix, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        root = ET.fromstring(_get(BASE_URL, params).text)
        keys.extend(
            node.find("s3:Key", S3_NS).text
            for node in root.findall("s3:Contents", S3_NS)
        )
        token_node = root.find("s3:NextContinuationToken", S3_NS)
        token = token_node.text if token_node is not None else None
        if not token:
            break

    return sorted(k for k in keys if parameter in k)


def latest_run(day: date | None = None) -> str:
    """Most recent run prefix available, falling back to the previous day."""
    day = day or datetime.now(timezone.utc).date()
    runs = list_runs(day)
    if runs:
        return runs[-1]

    runs = list_runs(day - timedelta(days=1))
    if not runs:
        raise MetOfficeError(f"no runs found for {day} or the day before")
    return runs[-1]


def recent_runs(day: date | None = None, count: int = 16) -> list[str]:
    """The `count` most recent run prefixes, newest last, spanning midnight.

    Runs cycle every 15 minutes, so 16 runs is about four hours.
    """
    day = day or datetime.now(timezone.utc).date()
    runs = list_runs(day)
    if len(runs) < count:
        runs = list_runs(day - timedelta(days=1)) + runs
    return runs[-count:] if runs else []


def parse_key_times(key: str) -> tuple[str, str]:
    """Extract (valid_time, blend_time) from an object key.

    Filenames look like:
        20260802T1800Z-B20260801T1145Z-temperature_at_screen_level_max-PT12H.nc
    """
    name = key.rsplit("/", 1)[-1]
    valid, _, rest = name.partition("-B")
    blend = rest[:16]
    return valid, blend


def collect_latest_keys(
    parameter: str = TMAX_PARAMETER,
    *,
    day: date | None = None,
    scan_runs: int = 16,
) -> list[str]:
    """Newest key per valid time, gathered across several recent runs.

    A single run is not enough. The stream publishes files progressively, so the
    most recent run frequently contains no max-temperature files at all, and
    successive runs carry different subsets of valid times (observed: 48, 30 and
    6 windows in three consecutive runs). Scanning a window of runs and keeping
    the freshest blend per valid time is what actually yields a complete set.
    """
    newest: dict[str, tuple[str, str]] = {}

    for run in recent_runs(day, scan_runs):
        for key in list_parameter_keys(run, parameter):
            valid, blend = parse_key_times(key)
            current = newest.get(valid)
            if current is None or blend > current[0]:
                newest[valid] = (blend, key)

    return [key for _, (_, key) in sorted(newest.items())]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_spot_percentiles(
    key: str,
    *,
    latitude: float = STATION_LAT,
    longitude: float = STATION_LON,
) -> SpotPercentiles:
    """Download one object and extract the site nearest our station.

    The site index is resolved by nearest neighbour at runtime. Site ordering in
    these files is not guaranteed stable, so the index must never be hardcoded.
    """
    try:
        import netCDF4  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MetOfficeError(
            "netCDF4 and numpy are required to read Met Office spot files "
            "(pip install netCDF4 numpy)"
        ) from exc

    blob = _get(BASE_URL + key).content

    try:
        dataset = netCDF4.Dataset("inmemory", mode="r", memory=blob)
    except OSError as exc:
        raise MetOfficeError(f"could not open {key} as NetCDF: {exc}") from exc

    try:
        temps = dataset.variables["air_temperature"]
        lats = np.asarray(dataset.variables["latitude"][:])
        lons = np.asarray(dataset.variables["longitude"][:])
        alts = np.asarray(dataset.variables["altitude"][:])
        percentiles = tuple(float(p) for p in np.asarray(dataset.variables["percentile"][:]))

        # Nearest neighbour in squared degrees is fine at this scale; the real
        # distance is reported separately in metres.
        index = int(np.argmin((lats - latitude) ** 2 + (lons - longitude) ** 2))

        values_k = np.asarray(temps[:, index], dtype=float)
        values_c = tuple(round(float(v) - 273.15, 3) for v in values_k)

        time_var = dataset.variables["time"]
        valid = netCDF4.num2date(time_var[:], time_var.units)
        valid_time = str(np.asarray(valid).ravel()[0])

        bounds = dataset.variables.get("time_bnds")
        if bounds is not None:
            edges = netCDF4.num2date(np.asarray(bounds[:]).ravel(), time_var.units)
            window_start, window_end = str(edges[0]), str(edges[-1])
        else:
            window_start = window_end = valid_time

        model_config = getattr(dataset, "mosg__model_configuration", "")

        return SpotPercentiles(
            key=key,
            parameter=TMAX_PARAMETER if TMAX_PARAMETER in key else key.split("-")[-1],
            valid_time=valid_time,
            window_start=window_start,
            window_end=window_end,
            percentiles=percentiles,
            values_c=values_c,
            site_latitude=float(lats[index]),
            site_longitude=float(lons[index]),
            site_altitude_m=float(alts[index]),
            site_distance_m=round(
                _haversine_m(latitude, longitude, float(lats[index]), float(lons[index])), 1
            ),
            model_configuration=model_config,
        )
    finally:
        dataset.close()


def fetch_latest_tmax(
    *,
    day: date | None = None,
    parameter: str = TMAX_PARAMETER,
    max_windows: int = 12,
    scan_runs: int = 16,
) -> list[SpotPercentiles]:
    """Freshest max-temperature windows available, nearest valid times first."""
    keys = collect_latest_keys(parameter, day=day, scan_runs=scan_runs)
    return [fetch_spot_percentiles(key) for key in keys[:max_windows]]
