"""Archive quoted prices before they are deleted.

Records are keyed by **market day**, not by harvest slot, and that difference
matters. A forecast record is a snapshot of an opinion held at one instant, so
each harvest is a distinct object worth keeping. A price record is a *path*:
harvesting the same market day twice returns overlapping views of one underlying
series, and what we want is the union of everything we have ever seen.

So harvests merge rather than accumulate. Merging is by `(token_id, ts)`, and a
timestamp we have already seen is never overwritten -- the first observation
wins, and any later disagreement is counted and surfaced rather than silently
resolved. If the endpoint ever starts revising history, the conflict count is
how we find out, instead of discovering it as unexplained backtest drift.

Retention is ~31 days (see sources/clob.py), so the practical rule is: harvest
every settled day still inside the window, every day, forever.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from weatherbot.config import PRICE_ARCHIVE_DIR
from weatherbot.sources import clob
from weatherbot.sources.polymarket import (
    event_target_date,
    fetch_event,
    slug_candidates,
)

SCHEMA_VERSION = 1


@dataclass
class PriceHarvestResult:
    """Outcome of harvesting one market day."""

    day: date
    ok: bool
    path: Path | None = None
    n_buckets: int = 0
    n_points: int = 0
    n_new_points: int = 0
    n_conflicts: int = 0
    bytes_written: int = 0
    skipped: bool = False
    error: str | None = None

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        return "ok" if self.ok else "FAILED"


def archive_path(day: date, root: Path | None = None) -> Path:
    """Where the price record for `day` belongs."""
    root = root or PRICE_ARCHIVE_DIR
    return root / day.strftime("%Y") / f"{day.isoformat()}.json.gz"


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


def merge_series(
    existing: list[dict], fetched: list[clob.OutcomeSeries]
) -> tuple[list[dict], int, int]:
    """Union new points into the stored series.

    Returns `(merged, n_new, n_conflicts)`. First observation of a timestamp
    wins; a differing later value counts as a conflict and is discarded.
    """
    by_token: dict[str, dict] = {entry["token_id"]: entry for entry in existing}
    n_new = n_conflicts = 0

    for series in fetched:
        entry = by_token.get(series.token_id)
        if entry is None:
            entry = {
                "bucket_label": series.bucket_label,
                "token_id": series.token_id,
                "points": [],
            }
            by_token[series.token_id] = entry

        seen = {int(p[0]): float(p[1]) for p in entry["points"]}
        for point in series.points:
            if point.ts in seen:
                if seen[point.ts] != point.price:
                    n_conflicts += 1
                continue
            seen[point.ts] = point.price
            n_new += 1
        entry["points"] = [[ts, seen[ts]] for ts in sorted(seen)]
        # A bucket label can only improve (an empty stored label filled in).
        if series.bucket_label and not entry.get("bucket_label"):
            entry["bucket_label"] = series.bucket_label

    return [by_token[t] for t in sorted(by_token)], n_new, n_conflicts


def harvest_day(
    day: date,
    *,
    root: Path | None = None,
    fidelity: int = clob.FINEST_FIDELITY_MINUTES,
    skip_if_complete: bool = False,
) -> PriceHarvestResult:
    """Harvest (and merge) the full price path for one market day.

    Never raises; failures are returned so one bad day cannot abort a sweep
    across the whole retention window.
    """
    path = archive_path(day, root)
    existing_record = read_record(path) if path.exists() else None

    if skip_if_complete and existing_record and existing_record.get("settled"):
        return PriceHarvestResult(
            day=day,
            ok=True,
            path=path,
            skipped=True,
            n_buckets=len(existing_record.get("series") or []),
            n_points=sum(
                len(s["points"]) for s in existing_record.get("series") or []
            ),
        )

    event = None
    for slug in slug_candidates(day):
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
        fetched = clob.fetch_event_prices(event, fidelity=fidelity)
    except clob.ClobError as exc:
        return PriceHarvestResult(day=day, ok=False, error=str(exc))

    existing_series = list(existing_record.get("series") or []) if existing_record else []
    merged, n_new, n_conflicts = merge_series(existing_series, fetched)
    total_points = sum(len(s["points"]) for s in merged)

    if total_points == 0:
        # Aged out of the retention window. Writing an empty shell would make
        # the archive look like it holds a day it does not.
        return PriceHarvestResult(
            day=day, ok=False, n_buckets=len(merged), error="no retained history"
        )

    record = {
        "schema_version": SCHEMA_VERSION,
        "day": day.isoformat(),
        "slug": event.get("slug"),
        "kind": "clob_price_history",
        "fidelity_minutes": fidelity,
        # Every harvest that contributed, so coverage gaps stay explainable.
        "harvests_utc": (existing_record or {}).get("harvests_utc", [])
        + [datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")],
        # A settled event's path is final; an unsettled one will grow.
        "settled": bool(event.get("closed")),
        "series": merged,
    }

    size = write_record(record, path)
    return PriceHarvestResult(
        day=day,
        ok=True,
        path=path,
        n_buckets=len(merged),
        n_points=total_points,
        n_new_points=n_new,
        n_conflicts=n_conflicts,
        bytes_written=size,
    )


def load_day(day: date, root: Path | None = None) -> list[clob.OutcomeSeries]:
    """Read an archived day back as typed series, or [] if not archived."""
    path = archive_path(day, root)
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
        )
        for entry in record.get("series") or []
    ]


def archived_days(root: Path | None = None) -> list[date]:
    """Every market day present in the archive, ascending."""
    root = root or PRICE_ARCHIVE_DIR
    if not root.exists():
        return []
    days = []
    for path in root.glob("*/*.json.gz"):
        try:
            days.append(date.fromisoformat(path.name.removesuffix(".json.gz")))
        except ValueError:
            continue
    return sorted(days)
