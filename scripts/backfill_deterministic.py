"""Backfill archived deterministic forecasts, resolved by lead time.

This is the only training data available *today*. Ensemble history cannot be
backfilled at all, so without this we would have to wait a year before fitting
anything. Verified archive depth for EGLC:

    ecmwf_ifs025    from ~2024-03  (2024-01-01 returns nulls, 2024-03-01 is full)
    best_match      from at least 2022-06
    gfs_seamless    from at least 2022-06

Each record stores, for every valid hour, the forecast as issued 1..N days
earlier (`temperature_2m_previous_dayN`). Training on the lead you actually
trade at is the difference between an honest model and one that looks brilliant
in backtest and loses money live.

This is archived rather than cached because Open-Meteo's archive is a
third-party service that could be pruned or re-based, and the whole backfill is
only a few MB gzipped -- cheap insurance.

Usage:
    py scripts/backfill_deterministic.py
    py scripts/backfill_deterministic.py --model gfs_seamless --start 2022-06-01
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.config import ARCHIVE_DIR, ensure_dirs  # noqa: E402
from weatherbot.harvest import build_record, write_record  # noqa: E402
from weatherbot.sources import openmeteo  # noqa: E402

BACKFILL_DIR = ARCHIVE_DIR / "backfill"

# Earliest date each model returns non-null data for EGLC, verified 2026-08-01.
MODEL_START = {
    "ecmwf_ifs025": date(2024, 3, 1),
    "ecmwf_ifs04": date(2023, 1, 1),
    "best_match": date(2022, 1, 1),
    "gfs_seamless": date(2022, 1, 1),
}

AUX_VARIABLES = (
    "dew_point_2m",
    "cloud_cover",
    "wind_speed_10m",
    "wind_direction_10m",
    "shortwave_radiation",
    "pressure_msl",
)

CHUNK_DAYS = 120


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="ecmwf_ifs025", choices=sorted(MODEL_START))
    parser.add_argument(
        "--start",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=None,
        help="Defaults to the model's earliest available date.",
    )
    parser.add_argument(
        "--end",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date.today() - timedelta(days=1),
    )
    parser.add_argument(
        "--leads",
        type=int,
        nargs="+",
        default=[1, 2, 3, 4, 5],
        help="Lead times in days to retrieve alongside the base forecast.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()

    start = args.start or MODEL_START[args.model]
    earliest = MODEL_START[args.model]
    if start < earliest:
        print(f"Note: {args.model} has no data before {earliest}; clamping start.")
        start = earliest

    if args.end <= start:
        print(f"Nothing to do: end ({args.end}) is not after start ({start}).")
        return 1

    leads = tuple(sorted(set(args.leads)))
    print(f"Model  : {args.model}")
    print(f"Range  : {start} -> {args.end}")
    print(f"Leads  : {', '.join(f'day{n}' for n in leads)}")
    print(f"Chunks : {CHUNK_DAYS} days each\n")

    total_bytes = 0
    written = skipped = failed = 0
    empty_chunks: list[str] = []

    cursor = start
    while cursor < args.end:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), args.end)
        label = f"{cursor.isoformat()}_{chunk_end.isoformat()}"
        path = BACKFILL_DIR / args.model / f"{label}.json.gz"

        if path.exists() and not args.overwrite:
            print(f"  {label}  skipped (exists)")
            skipped += 1
            cursor = chunk_end
            continue

        try:
            payload = openmeteo.fetch_historical_forecast_with_leads(
                cursor,
                chunk_end,
                model=args.model,
                leads=leads,
                aux_variables=AUX_VARIABLES,
            )
        except openmeteo.OpenMeteoError as exc:
            print(f"  {label}  FAILED: {exc}")
            failed += 1
            cursor = chunk_end
            continue

        hourly = payload.get("hourly") or {}
        base = hourly.get("temperature_2m") or []
        non_null = sum(1 for v in base if v is not None)
        if non_null == 0:
            print(f"  {label}  empty (model has no data for this range)")
            empty_chunks.append(label)
            cursor = chunk_end
            continue

        record = build_record(
            args.model,
            payload,
            datetime.now(timezone.utc),
            kind="historical_forecast",
            variables=tuple(
                openmeteo.lead_variables("temperature_2m", leads)
            )
            + AUX_VARIABLES,
        )
        size = write_record(record, path)
        total_bytes += size
        written += 1
        print(
            f"  {label}  {non_null}/{len(base)} non-null, {size / 1024:.1f} KB"
        )
        openmeteo.polite_sleep(1.0)
        cursor = chunk_end

    print(
        f"\n{written} chunks written, {skipped} skipped, {failed} failed, "
        f"{len(empty_chunks)} empty, {total_bytes / 1024 / 1024:.2f} MB total"
    )
    if empty_chunks:
        print(f"Empty ranges (model unavailable): {', '.join(empty_chunks[:5])}")

    if failed:
        print("\nSome chunks failed. Re-run to retry only those.")
        return 1
    if written == 0 and skipped == 0:
        print("\nNothing was written. Check the model start date.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
