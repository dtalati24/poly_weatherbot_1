"""Archive Polymarket quoted prices. Entry point for the scheduler.

Full history is available -- see sources/clob.py for why it briefly looked as
though it was not -- so this has two modes:

    --all           backfill every market day from the first market onward
    --days N        sweep the last N days (the scheduled mode)

The scheduled mode still re-harvests several days rather than only today,
because a day captured while the market is still trading is incomplete, and
re-harvesting after settlement is what completes it.

Exit codes:
    0  the window was swept; days with no market are expected and not failures
    1  nothing was archived anywhere in the window

Usage:
    py scripts/harvest_prices.py --days 7
    py scripts/harvest_prices.py --all --skip-existing
    py scripts/harvest_prices.py --start 2026-07-01 --end 2026-08-02
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.cities import get as get_city  # noqa: E402
from weatherbot.config import ensure_dirs  # noqa: E402
from weatherbot.priceharvest import harvest_day  # noqa: E402

# London daily-high markets do not go back further than this; earlier days
# simply have no event to harvest.
FIRST_MARKET_DAY = date(2024, 1, 1)


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="london")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--all", action="store_true", help="Backfill from the start.")
    parser.add_argument("--start", type=_date, default=None)
    parser.add_argument("--end", type=_date, default=None, help="Exclusive.")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip days already archived at the current schema. Use for "
             "resuming a backfill; omit to refresh and detect revisions.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()
    city = get_city(args.city)

    today = datetime.now(timezone.utc).date()
    if args.all:
        start = args.start or FIRST_MARKET_DAY
    else:
        start = args.start or today - timedelta(days=args.days)
    # Exclusive end of tomorrow, so today's still-trading market is captured.
    end = args.end or today + timedelta(days=1)

    print(f"Sweep: {start} -> {end} (exclusive), {(end - start).days} days\n")
    print(f"{'day':<12}{'status':<10}{'buckets':>8}{'changes':>9}{'minutes':>9}"
          f"{'revised':>9}{'KB':>7}")
    print("-" * 64)

    results = []
    day = start
    while day < end:
        result = harvest_day(day, skip_existing=args.skip_existing, city=city)
        results.append(result)
        # A day with no market is the normal case across a long backfill; only
        # print it if something interesting happened.
        if result.ok or result.error != "no market found for day":
            size_kb = result.bytes_written / 1024 if result.bytes_written else 0
            print(
                f"{day.isoformat():<12}{result.status:<10}{result.n_buckets:>8}"
                f"{result.n_points:>9}{result.n_minutes:>9}"
                f"{result.n_conflicts:>9}{size_kb:>7.1f}"
            )
        day += timedelta(days=1)

    print("-" * 64)

    ok = [r for r in results if r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]
    no_market = [r for r in results if not r.ok and r.error == "no market found for day"]
    no_prices = [r for r in results if not r.ok and r.error == "no price history returned"]
    broken = [
        r for r in results
        if not r.ok and r not in no_market and r not in no_prices
    ]

    total_points = sum(r.n_points for r in ok)
    total_conflicts = sum(r.n_conflicts for r in ok)
    total_kb = sum(r.bytes_written for r in ok) / 1024

    print(
        f"{len(ok)} archived ({total_points} change-points, {total_kb:.0f} KB), "
        f"{len(skipped)} skipped, {len(no_market)} no market, "
        f"{len(no_prices)} no prices, {len(broken)} failed"
    )

    if total_conflicts:
        print(
            f"\nWARNING: {total_conflicts} stored change-points came back with a "
            f"different price. The endpoint may be revising history."
        )

    if broken:
        print("\nFailures:")
        for result in broken[:20]:
            print(f"  {result.day}: {result.error}")

    if not ok and not skipped:
        print("\nFATAL: nothing was archived anywhere in the window.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
