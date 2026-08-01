"""Forecast harvesting: fetch, wrap with provenance, and archive.

Why this module is the most time-critical part of the project: ensemble
forecasts cannot be fetched retrospectively. Open-Meteo's ensemble endpoint
returns only ~4 days of history regardless of what you ask for, and MOGREPS-UK
on AWS is a 30-day rolling window. Every scheduled run that does not happen is
a training row that can never be recovered.

Records are stored as gzipped JSON with the **raw upstream payload preserved
verbatim**. Any normalisation we apply today is a guess about what the model
will need; the raw response is the only thing we can be sure is complete.

Every record carries `harvested_at_utc`. That timestamp is the leakage anchor:
it is the moment we could first have known this forecast, and no backtest may
use a record to predict anything at or before its own harvest time.
"""

from __future__ import annotations

import gzip
import json
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from weatherbot.config import (
    FORECAST_ARCHIVE_DIR,
    STATION_ICAO,
    STATION_LAT,
    STATION_LON,
)
from weatherbot.sources import metoffice, openmeteo

SCHEMA_VERSION = 1


@dataclass
class HarvestResult:
    """Outcome of harvesting one model."""

    model: str
    ok: bool
    path: Path | None = None
    n_members: int = 0
    n_timesteps: int = 0
    bytes_written: int = 0
    skipped: bool = False
    error: str | None = None
    member_warning: str | None = None

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        return "ok" if self.ok else "FAILED"


def _slot(moment: datetime) -> str:
    """Archive filename stem: hour-resolution UTC slot.

    Hour resolution makes reruns within the same hour idempotent, so a retried
    workflow does not write duplicate records.
    """
    return moment.strftime("%Y-%m-%dT%HZ")


def archive_path(model: str, moment: datetime, root: Path | None = None) -> Path:
    """Where a record for `model` harvested at `moment` belongs."""
    root = root or FORECAST_ARCHIVE_DIR
    return root / model / moment.strftime("%Y") / f"{_slot(moment)}.json.gz"


def build_record(
    model: str,
    payload: dict,
    harvested_at: datetime,
    *,
    kind: str = "ensemble",
    variables: tuple[str, ...] = (),
) -> dict:
    """Wrap a raw payload with the provenance needed to use it safely."""
    return {
        "schema_version": SCHEMA_VERSION,
        # The leakage anchor. Never use this record to predict anything at or
        # before this instant.
        "harvested_at_utc": harvested_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": kind,
        "model": model,
        "station": STATION_ICAO,
        "requested_latitude": STATION_LAT,
        "requested_longitude": STATION_LON,
        "variables": list(variables),
        "harvester": {
            "python": platform.python_version(),
            "node": platform.node(),
        },
        "payload": payload,
    }


def write_record(record: dict, path: Path) -> int:
    """Write a record as gzipped JSON, returning bytes written.

    Written to a temporary file and then moved into place, so an interrupted
    run cannot leave a half-written record that later looks valid.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    blob = json.dumps(record, separators=(",", ":")).encode("utf-8")
    # mtime=0 keeps the gzip header deterministic, so re-running produces an
    # identical file rather than a spurious git diff.
    with gzip.GzipFile(filename="", mode="wb", fileobj=tmp.open("wb"), mtime=0) as fh:
        fh.write(blob)

    size = tmp.stat().st_size
    tmp.replace(path)
    return size


def read_record(path: Path) -> dict:
    """Read a gzipped record back."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def harvest_ensemble_model(
    model: openmeteo.EnsembleModel,
    harvested_at: datetime,
    *,
    forecast_days: int = 7,
    root: Path | None = None,
    overwrite: bool = False,
) -> HarvestResult:
    """Harvest one ensemble model. Never raises -- failures are returned."""
    path = archive_path(model.model_id, harvested_at, root)
    if path.exists() and not overwrite:
        return HarvestResult(model=model.model_id, ok=True, path=path, skipped=True)

    try:
        payload = openmeteo.fetch_ensemble(
            model.model_id, forecast_days=forecast_days
        )
    except openmeteo.OpenMeteoError as exc:
        return HarvestResult(model=model.model_id, ok=False, error=str(exc))

    series = openmeteo.extract_members(payload)
    if series.n_members == 0:
        return HarvestResult(
            model=model.model_id,
            ok=False,
            error="payload contained no non-null member series",
        )

    # A feed silently changing shape is the kind of thing that corrupts a
    # training set quietly, so it is surfaced rather than swallowed.
    warning = None
    if series.n_members != model.members:
        warning = (
            f"expected {model.members} member series, got {series.n_members} "
            f"-- upstream feed may have changed"
        )

    record = build_record(
        model.model_id,
        payload,
        harvested_at,
        kind="ensemble",
        variables=openmeteo.ENSEMBLE_VARIABLES,
    )
    size = write_record(record, path)

    return HarvestResult(
        model=model.model_id,
        ok=True,
        path=path,
        n_members=series.n_members,
        n_timesteps=len(series.times),
        bytes_written=size,
        member_warning=warning,
    )


def harvest_deterministic(
    harvested_at: datetime,
    *,
    forecast_days: int = 7,
    root: Path | None = None,
    overwrite: bool = False,
) -> HarvestResult:
    """Harvest the auxiliary feature set from the deterministic endpoint.

    Kept separate from the ensembles because the ensemble endpoint returns every
    requested variable for every member, so adding features there would multiply
    the archive size by the member count for no modelling benefit.
    """
    name = "deterministic_best_match"
    path = archive_path(name, harvested_at, root)
    if path.exists() and not overwrite:
        return HarvestResult(model=name, ok=True, path=path, skipped=True)

    try:
        payload = openmeteo.fetch_deterministic(forecast_days=forecast_days)
    except openmeteo.OpenMeteoError as exc:
        return HarvestResult(model=name, ok=False, error=str(exc))

    record = build_record(
        name,
        payload,
        harvested_at,
        kind="deterministic",
        variables=openmeteo.DETERMINISTIC_VARIABLES,
    )
    size = write_record(record, path)
    hourly = payload.get("hourly") or {}

    return HarvestResult(
        model=name,
        ok=True,
        path=path,
        n_members=1,
        n_timesteps=len(hourly.get("time") or []),
        bytes_written=size,
    )


def harvest_metoffice_spot(
    harvested_at: datetime,
    *,
    root: Path | None = None,
    overwrite: bool = False,
    max_windows: int = 12,
) -> HarvestResult:
    """Harvest Met Office IMPROVER spot percentiles for the station.

    Unlike the Open-Meteo records this stores an extraction rather than the raw
    payload -- the source NetCDF carries all 8667 UK sites at ~450 KB, and we
    need one. The source object key is retained so the extraction stays
    reproducible while the object is inside its 30-day window.
    """
    name = "metoffice_spot_percentiles"
    path = archive_path(name, harvested_at, root)
    if path.exists() and not overwrite:
        return HarvestResult(model=name, ok=True, path=path, skipped=True)

    try:
        windows = metoffice.fetch_latest_tmax(max_windows=max_windows)
    except metoffice.MetOfficeError as exc:
        return HarvestResult(model=name, ok=False, error=str(exc))

    if not windows:
        return HarvestResult(model=name, ok=False, error="no max-temperature windows found")

    payload = {
        "windows": [
            {
                "key": w.key,
                "valid_time": w.valid_time,
                "window_start": w.window_start,
                "window_end": w.window_end,
                "percentiles": list(w.percentiles),
                "values_c": list(w.values_c),
                "site_latitude": w.site_latitude,
                "site_longitude": w.site_longitude,
                "site_altitude_m": w.site_altitude_m,
                "site_distance_m": w.site_distance_m,
                "model_configuration": w.model_configuration,
            }
            for w in windows
        ]
    }

    record = build_record(
        name, payload, harvested_at, kind="spot_percentiles", variables=("air_temperature",)
    )
    size = write_record(record, path)

    return HarvestResult(
        model=name,
        ok=True,
        path=path,
        n_members=len(windows[0].percentiles),
        n_timesteps=len(windows),
        bytes_written=size,
    )


def harvest_all(
    *,
    harvested_at: datetime | None = None,
    forecast_days: int = 7,
    root: Path | None = None,
    overwrite: bool = False,
    models: tuple[openmeteo.EnsembleModel, ...] = openmeteo.ENSEMBLE_MODELS,
    polite_delay: float = 0.5,
) -> list[HarvestResult]:
    """Harvest every configured model plus the deterministic feature set."""
    harvested_at = harvested_at or datetime.now(timezone.utc)

    results: list[HarvestResult] = []
    for index, model in enumerate(models):
        if index:
            openmeteo.polite_sleep(polite_delay)
        results.append(
            harvest_ensemble_model(
                model,
                harvested_at,
                forecast_days=forecast_days,
                root=root,
                overwrite=overwrite,
            )
        )

    openmeteo.polite_sleep(polite_delay)
    results.append(
        harvest_deterministic(
            harvested_at, forecast_days=forecast_days, root=root, overwrite=overwrite
        )
    )

    openmeteo.polite_sleep(polite_delay)
    results.append(
        harvest_metoffice_spot(harvested_at, root=root, overwrite=overwrite)
    )
    return results
