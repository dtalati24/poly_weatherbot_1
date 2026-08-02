"""Archive quoted prices, one record per market day.

Records are keyed by market day rather than by harvest slot, because a price
record is a *path* rather than a snapshot: harvesting the same day twice
returns two views of one underlying series, and what we want is the better one.

Since `clob.fetch_event_prices` requests an explicit window covering the whole
tradeable life of the market, a single successful harvest is already complete.
So a re-harvest **replaces** rather than merges -- but before it does, it
compares the incoming change-points against the stored ones and counts any
disagreement. That counter is the early warning if the endpoint ever starts
revising history; without it, revisions would surface much later as unexplained
backtest drift.

Storage is change-points plus explicit coverage bounds (see
`clob.OutcomeSeries`). That is lossless with respect to `price_at` and about a
tenth the size of storing every 1-minute sample.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from weatherbot.cities import LONDON, City
from weatherbot.config import PRICE_ARCHIVE_DIR
from weatherbot.sources import clob
from weatherbot.sources.polymarket import (
    event_target_date,
    fetch_event,
    slug_candidates,
)


def price_archive_root(city: City = LONDON, root: Path | None = None) -> Path:
    """Archive root for a city.

    London lives at the top of `archive/prices/` because it was archived before
    this was multi-city and 536 committed files are not worth relocating for
    tidiness. Every other city gets its own subdirectory.
    """
    base = root or PRICE_ARCHIVE_DIR
    return base if city.key == LONDON.key else base / city.key

# 1: 10-minute samples from `interval=max`, no coverage bounds. Superseded --
#    that path silently truncated settled markets. See sources/clob.py.
# 2: 1-minute change-points over an explicit window, with coverage bounds.
SCHEMA_VERSION = 2


@dataclass
class PriceHarvestResult:
    """Outcome of harvesting one market day."""

    day: date
    ok: bool
    path: Path | None = None
    n_buckets: int = 0
    n_points: int = 0
    n_minutes: int = 0
    n_conflicts: int = 0
    bytes_written: int = 0
    skipped: bool = False
    upgraded: bool = False
    error: str | None = None

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        if not self.ok:
            return "FAILED"
        return "upgraded" if self.upgraded else "ok"


def archive_path(
    day: date, root: Path | None = None, city: City = LONDON
) -> Path:
    """Where the price record for `day` belongs."""
    return price_archive_root(city, root) / day.strftime("%Y") / f"{day.isoformat()}.json.gz"


def read_record(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def write_record(record: dict, path: Path) -> int:
    """Write atomically, with a deterministic gzip header.

    Same discipline as the forecast archive: a half-written record that later
    looks valid is worse than no record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    blob = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with gzip.GzipFile(filename="", mode="wb", fileobj=tmp.open("wb"), mtime=0) as fh:
        fh.write(blob)

    size = tmp.stat().st_size
    tmp.replace(path)
    return size


def count_revisions(existing: dict | None, series: list[clob.OutcomeSeries]) -> int:
    """How many stored change-points the endpoint now reports differently.

    Only meaningful against a schema-2 record; a schema-1 record sampled a
    different grid entirely, so comparing them would report noise as revision.
    """
    if not existing or existing.get("schema_version") != SCHEMA_VERSION:
        return 0

    stored = {
        entry["token_id"]: {int(ts): float(p) for ts, p in entry["points"]}
        for entry in existing.get("series") or []
    }
    conflicts = 0
    for entry in series:
        previous = stored.get(entry.token_id)
        if not previous:
            continue
        for point in entry.points:
            if point.ts in previous and previous[point.ts] != point.price:
                conflicts += 1
    return conflicts


def harvest_day(
    day: date,
    *,
    root: Path | None = None,
    fidelity: int = clob.FINEST_FIDELITY_MINUTES,
    lookback_days: int = clob.LOOKBACK_DAYS,
    skip_existing: bool = False,
    city: City = LONDON,
) -> PriceHarvestResult:
    """Harvest the full price path for one market day.

    Never raises; failures are returned so one bad day cannot abort a sweep of
    several hundred.
    """
    path = archive_path(day, root, city)
    existing = read_record(path) if path.exists() else None

    if skip_existing and existing and existing.get("schema_version") == SCHEMA_VERSION:
        return PriceHarvestResult(
            day=day,
            ok=True,
            path=path,
            skipped=True,
            n_buckets=len(existing.get("series") or []),
            n_points=sum(len(s["points"]) for s in existing.get("series") or []),
        )

    event = None
    for slug in slug_candidates(day, city.slug_prefix):
        try:
            candidate = fetch_event(slug, use_cache=True)
        except Exception as exc:  # noqa: BLE001 - one day must not abort the sweep
            return PriceHarvestResult(day=day, ok=False, error=f"gamma: {exc}")
        # The unsuffixed slug can belong to another year's market entirely.
        if candidate and event_target_date(candidate) == day:
            event = candidate
            break

    if event is None:
        return PriceHarvestResult(day=day, ok=False, error="no market found for day")

    try:
        fetched = clob.fetch_event_prices(
            event, day, fidelity=fidelity, lookback_days=lookback_days
        )
    except (clob.ClobError, ValueError) as exc:
        return PriceHarvestResult(day=day, ok=False, error=str(exc))

    populated = [s for s in fetched if s.points]
    if not populated:
        return PriceHarvestResult(
            day=day, ok=False, n_buckets=len(fetched), error="no price history returned"
        )

    conflicts = count_revisions(existing, populated)
    covered = [s.covers for s in populated if s.covers]
    minutes = (
        (max(c[1] for c in covered) - min(c[0] for c in covered)) // 60 if covered else 0
    )

    record = {
        "schema_version": SCHEMA_VERSION,
        "day": day.isoformat(),
        "slug": event.get("slug"),
        "kind": "clob_midpoint_change_points",
        "fidelity_minutes": fidelity,
        "harvests_utc": (existing or {}).get("harvests_utc", [])
        + [datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")],
        "settled": bool(event.get("closed")),
        "series": [
            {
                "bucket_label": s.bucket_label,
                "token_id": s.token_id,
                "first_ts": s.first_ts,
                "last_ts": s.last_ts,
                "points": [[p.ts, p.price] for p in s.points],
            }
            for s in sorted(populated, key=lambda s: s.token_id)
        ],
    }

    size = write_record(record, path)
    return PriceHarvestResult(
        day=day,
        ok=True,
        path=path,
        n_buckets=len(populated),
        n_points=sum(len(s.points) for s in populated),
        n_minutes=minutes,
        n_conflicts=conflicts,
        bytes_written=size,
        upgraded=bool(existing and existing.get("schema_version") != SCHEMA_VERSION),
    )


def load_day(
    day: date, root: Path | None = None, city: City = LONDON
) -> list[clob.OutcomeSeries]:
    """Read an archived day back as typed series, or [] if not archived."""
    path = archive_path(day, root, city)
    if not path.exists():
        return []
    record = read_record(path)
    return [
        clob.OutcomeSeries(
            bucket_label=entry.get("bucket_label", ""),
            token_id=entry["token_id"],
            points=tuple(
                clob.PricePoint(ts=int(ts), price=float(p)) for ts, p in entry["points"]
            ),
            first_ts=entry.get("first_ts"),
            last_ts=entry.get("last_ts"),
        )
        for entry in record.get("series") or []
    ]


def archived_days(root: Path | None = None, city: City = LONDON) -> list[date]:
    """Every market day present in the archive, ascending."""
    root = price_archive_root(city, root)
    if not root.exists():
        return []
    days = []
    for path in root.glob("*/*.json.gz"):
        try:
            days.append(date.fromisoformat(path.name.removesuffix(".json.gz")))
        except ValueError:
            continue
    return sorted(days)
