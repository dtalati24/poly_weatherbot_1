"""Observation cadence and truncation analysis (PLAN.md checklist 0.2 / 0.3).

Tests the hypothesis that London City Airport's weekend noise curfew
(Sat ~12:30 -> Sun ~12:30 local, per the UK AIP) truncates the observation
record before the day's thermal peak, which would bias settled maxima low on
those days.

Usage:
    py scripts/analyze_cadence.py --source wunderground
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.analysis.cadence import (  # noqa: E402
    WEEKDAY_NAMES,
    argmax_hour_histogram,
    hourly_coverage,
    peak_at_last_observation,
    weekday_cadence,
)
from weatherbot.config import STATION_ICAO, ensure_dirs  # noqa: E402
from weatherbot.resolve import DayBoundary, Strategy, daily_maxima  # noqa: E402
from weatherbot.sources import iem, wunderground  # noqa: E402

SOURCES = {"wunderground": wunderground, "iem": iem}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=sorted(SOURCES), default="wunderground")
    parser.add_argument(
        "--start",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date(2025, 1, 1),
    )
    parser.add_argument(
        "--end",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date.today(),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()
    source = SOURCES[args.source]

    print(f"Station : {STATION_ICAO}   Source: {args.source}")
    print(f"Range   : {args.start} -> {args.end}\n")

    observations = source.fetch_observations(
        STATION_ICAO, args.start - timedelta(days=1), args.end + timedelta(days=1)
    )
    print(f"{len(observations):,} observations\n")

    print("=" * 86)
    print("PER-WEEKDAY REPORTING CADENCE (local time)")
    print("=" * 86)
    print(f"{'day':<6}{'n':>6}{'mean obs':>10}{'min obs':>9}"
          f"{'earliest':>10}{'latest':>9}{'mean last':>11}{'ends<17h':>10}")
    for row in weekday_cadence(observations):
        print(
            f"{row.name:<6}{row.n_days:>6}{row.mean_obs_per_day:>10.1f}"
            f"{row.min_obs_per_day:>9}{row.earliest_obs_hour:>10.1f}"
            f"{row.latest_obs_hour:>9.1f}{row.mean_latest_obs_hour:>11.1f}"
            f"{row.truncation_rate:>9.1%}"
        )

    print("\n" + "=" * 86)
    print("AFTERNOON COVERAGE BY WEEKDAY (observation count per local hour)")
    print("Zeros in the 12-18h columns on Sat/Sun would confirm curfew truncation.")
    print("=" * 86)
    coverage = hourly_coverage(observations)
    hours = list(range(10, 20))
    print(f"{'day':<6}" + "".join(f"{h:>7}" for h in hours))
    for weekday in range(7):
        counts = coverage[weekday]
        print(f"{WEEKDAY_NAMES[weekday]:<6}" + "".join(f"{counts[h]:>7}" for h in hours))

    maxima = daily_maxima(observations, Strategy(boundary=DayBoundary.LOCAL))

    print("\n" + "=" * 86)
    print("HOUR OF DAILY MAXIMUM (local)")
    print("=" * 86)
    histogram = argmax_hour_histogram(maxima)
    total = sum(histogram.values()) or 1
    for hour, count in histogram.items():
        bar = "#" * max(1, round(60 * count / max(histogram.values())))
        print(f"  {hour:02d}h {count:>5} ({count / total:>5.1%}) {bar}")

    truncated = peak_at_last_observation(maxima)
    print("\n" + "=" * 86)
    print("TRUNCATION RISK")
    print("=" * 86)
    print(
        f"Days whose maximum occurred at the final observation AND before 17:00 "
        f"local: {len(truncated)} / {len(maxima)} ({len(truncated) / max(1, len(maxima)):.2%})"
    )
    if truncated:
        by_weekday: dict[int, int] = {}
        for day in truncated:
            by_weekday[day.weekday()] = by_weekday.get(day.weekday(), 0) + 1
        print("  by weekday: " + ", ".join(
            f"{WEEKDAY_NAMES[w]}={n}" for w, n in sorted(by_weekday.items())
        ))
        print("  sample: " + ", ".join(d.isoformat() for d in truncated[:10]))
    else:
        print("  None. The observation record covers the diurnal peak on every day.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
