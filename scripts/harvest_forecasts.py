"""Harvest ensemble forecasts into the archive. Entry point for the scheduler.

Run this on a schedule and never stop. Ensemble forecasts cannot be fetched for
a past date -- a run that does not happen is training data permanently lost.

Exit codes are chosen so a transient upstream hiccup does not train the operator
to ignore red builds:
    0  primary model archived (some secondary models may have failed)
    1  the primary model failed, or a majority of models failed

Usage:
    py scripts/harvest_forecasts.py
    py scripts/harvest_forecasts.py --forecast-days 10 --overwrite
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.config import ensure_dirs  # noqa: E402
from weatherbot.harvest import harvest_all  # noqa: E402
from weatherbot.sources import openmeteo  # noqa: E402

# If this model is missing, the archive has a hole that matters. Everything else
# is corroboration.
PRIMARY_MODEL = "ecmwf_ifs025"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--forecast-days",
        type=int,
        default=7,
        help="Horizon to request. Markets list ~2 days ahead, so 7 covers the "
             "whole tradeable life with margin (default: 7).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-fetch even if a record already exists for this UTC hour.",
    )
    parser.add_argument(
        "--models",
        nargs="*",
        default=None,
        help="Subset of model ids to harvest (default: all configured).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()

    models = openmeteo.ENSEMBLE_MODELS
    if args.models:
        wanted = set(args.models)
        models = tuple(m for m in models if m.model_id in wanted)
        unknown = wanted - {m.model_id for m in openmeteo.ENSEMBLE_MODELS}
        if unknown:
            print(f"Unknown model id(s): {', '.join(sorted(unknown))}")
            return 1

    harvested_at = datetime.now(timezone.utc)
    print(f"Harvest slot : {harvested_at:%Y-%m-%dT%H:%M:%SZ}")
    print(f"Horizon      : {args.forecast_days} days")
    print(f"Models       : {len(models)} ensemble + 1 deterministic\n")

    results = harvest_all(
        harvested_at=harvested_at,
        forecast_days=args.forecast_days,
        overwrite=args.overwrite,
        models=models,
    )

    print(f"{'model':<30}{'status':<10}{'members':>8}{'steps':>7}{'KB':>8}")
    print("-" * 63)
    for result in results:
        size_kb = result.bytes_written / 1024 if result.bytes_written else 0
        print(
            f"{result.model:<30}{result.status:<10}{result.n_members:>8}"
            f"{result.n_timesteps:>7}{size_kb:>8.1f}"
        )
    print("-" * 63)

    total_kb = sum(r.bytes_written for r in results) / 1024
    written = [r for r in results if r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]
    failed = [r for r in results if not r.ok]

    print(
        f"{len(written)} written, {len(skipped)} skipped, {len(failed)} failed, "
        f"{total_kb:.1f} KB total"
    )

    for result in results:
        if result.member_warning:
            print(f"\nWARNING [{result.model}]: {result.member_warning}")

    if failed:
        print("\nFailures:")
        for result in failed:
            print(f"  {result.model}: {result.error}")

    primary = next((r for r in results if r.model == PRIMARY_MODEL), None)
    if primary is None or not primary.ok:
        print(f"\nFATAL: primary model {PRIMARY_MODEL} was not archived.")
        return 1

    if len(failed) > len(results) / 2:
        print(f"\nFATAL: {len(failed)}/{len(results)} models failed.")
        return 1

    if failed:
        print("\nPrimary model archived; continuing despite secondary failures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
