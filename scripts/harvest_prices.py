"""Archive Polymarket quoted prices. Entry point for the scheduler.

Price history is retained for a rolling ~31 days and then deleted, so this
sweeps the whole window on every run rather than only harvesting today. That
looks wasteful and is not: re-harvesting is how a day partially captured while
still trading becomes a complete settled path, and how a run missed yesterday
is recovered rather than lost.

The only unrecoverable failure is not running for a month.

Exit codes:
    0  the window was swept; days outside retention are expected to fail
    1  every day inside the window failed, which means something is broken

Usage:
    py scripts/harvest_prices.py
    py scripts/harvest_prices.py --days 31
    py scripts/harvest_prices.py --start 2026-07-01 --end 2026-08-02
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.config import ensure_dirs  # noqa: E402
from weatherbot.priceharvest import harvest_day  # noqa: E402
from weatherbot.sources.clob import RETENTION_DAYS  # noqa: E402


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=RETENTION_DAYS + 1,
        help="How far back to sweep from today (default: the retention window).",
    )
    parser.add_argument("--start", type=_date, default=None)
    parser.add_argument("--end", type=_date, default=None, help="Exclusive.")
    parser.add_argument(
        "--skip-settled",
        action="store_true",
        help="Skip days already archived as settled. Faster, but forgoes the "
             "chance to notice the endpoint revising history.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()

    today = datetime.now(timezone.utc).date()
    start = args.start or today - timedelta(days=args.days)
    # Tomorrow is exclusive-end so today's still-trading market is captured too.
    end = args.end or today + timedelta(days=1)

    print(f"Sweep    : {start} -> {end} (exclusive)")
    print(f"Retention: ~{RETENTION_DAYS} days; days older than that will be empty\n")

    print(f"{'day':<12}{'status':<9}{'buckets':>8}{'points':>8}{'new':>7}"
          f"{'conflict':>9}{'KB':>7}")
    print("-" * 60)

    results = []
    day = start
    while day < end:
        result = harvest_day(day, skip_if_complete=args.skip_settled)
        results.append(result)
        size_kb = result.bytes_written / 1024 if result.bytes_written else 0
        print(
            f"{day.isoformat():<12}{result.status:<9}{result.n_buckets:>8}"
            f"{result.n_points:>8}{result.n_new_points:>7}"
            f"{result.n_conflicts:>9}{size_kb:>7.1f}"
        )
        day += timedelta(days=1)

    print("-" * 60)

    ok = [r for r in results if r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]
    aged_out = [r for r in results if not r.ok and r.error == "no retained history"]
    no_market = [r for r in results if not r.ok and r.error == "no market found for day"]
    broken = [r for r in results if not r.ok and r not in aged_out and r not in no_market]

    total_points = sum(r.n_points for r in ok)
    total_new = sum(r.n_new_points for r in ok)
    total_conflicts = sum(r.n_conflicts for r in ok)

    print(
        f"{len(ok)} archived ({total_points} points, {total_new} new), "
        f"{len(skipped)} skipped, {len(aged_out)} aged out, "
        f"{len(no_market)} no market, {len(broken)} failed"
    )

    if total_conflicts:
        print(
            f"\nWARNING: {total_conflicts} timestamps came back with a different "
            f"price than previously archived. The endpoint may be revising "
            f"history; stored values were kept."
        )

    if broken:
        print("\nFailures:")
        for result in broken:
            print(f"  {result.day}: {result.error}")

    if not ok and not skipped:
        print("\nFATAL: nothing was archived anywhere in the window.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
