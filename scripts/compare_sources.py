"""Quantify how often Weather Underground disagrees with the raw METAR record.

WU is the settlement source, but it does not always ingest every METAR the
station issues. Where it drops one, the settled value can sit a full degree --
a full market bucket -- below what the raw record implies.

This matters in both directions:
  - Risk:  a model trained on raw METARs is biased high and will systematically
           overprice the upper bucket.
  - Edge:  anyone else pricing off raw METARs carries that same bias, and these
           days are identifiable in real time by comparing the two feeds.

Usage:
    py scripts/compare_sources.py --start 2025-01-01 --end 2026-08-01
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.config import STATION_ICAO, ensure_dirs  # noqa: E402
from weatherbot.resolve import DayBoundary, Strategy, daily_maxima  # noqa: E402
from weatherbot.sources import iem, wunderground  # noqa: E402
from weatherbot.sources.polymarket import fetch_resolved_range  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
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

    pad_start = args.start - timedelta(days=1)
    pad_end = args.end + timedelta(days=1)
    strategy = Strategy(boundary=DayBoundary.LOCAL)

    print(f"Station    : {STATION_ICAO}")
    print(f"Date range : {args.start} -> {args.end}\n")

    print("Fetching Weather Underground ...")
    wu_obs = wunderground.fetch_observations(STATION_ICAO, pad_start, pad_end)
    print(f"  {len(wu_obs):,} observations")

    print("Fetching IEM raw METAR ...")
    iem_obs = iem.fetch_observations(STATION_ICAO, pad_start, pad_end)
    print(f"  {len(iem_obs):,} observations")

    wu_max = daily_maxima(wu_obs, strategy)
    iem_max = daily_maxima(iem_obs, strategy)

    shared = sorted(set(wu_max) & set(iem_max))
    shared = [d for d in shared if args.start <= d < args.end]

    diffs = [(d, wu_max[d].tmax_c, iem_max[d].tmax_c) for d in shared
             if wu_max[d].tmax_c != iem_max[d].tmax_c]

    print(f"\nDays compared          : {len(shared)}")
    print(f"Days where WU != METAR : {len(diffs)}  ({len(diffs) / len(shared):.2%})"
          if shared else "no overlapping days")

    if diffs:
        wu_lower = sum(1 for _, w, i in diffs if w < i)
        print(f"  WU lower : {wu_lower}")
        print(f"  WU higher: {len(diffs) - wu_lower}")

        # Which side did Polymarket settle with? This is the decisive check.
        markets = {m.day: m for m in fetch_resolved_range(args.start, args.end)}
        print(f"\n  {'date':<12}{'WU':>4}{'METAR':>7}   {'settled':<18}{'agrees with'}")
        for day, wu_val, iem_val in diffs:
            market = markets.get(day)
            if market:
                agrees = []
                if market.agrees_with(wu_val):
                    agrees.append("WU")
                if market.agrees_with(iem_val):
                    agrees.append("METAR")
                verdict = "+".join(agrees) if agrees else "NEITHER"
                label = market.winner.label
            else:
                verdict, label = "(no market)", "-"
            print(f"  {day.isoformat():<12}{wu_val:>4}{iem_val:>7}   {label:<18}{verdict}")

    n_obs_diff = len(iem_obs) - len(wu_obs)
    print(f"\nObservation count gap (METAR - WU): {n_obs_diff:,}")
    print("\nConclusion: train and settle against Weather Underground.")
    print("Keep IEM as the long-history archive and as a real-time divergence signal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
