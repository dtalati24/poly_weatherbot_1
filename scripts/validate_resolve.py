"""Phase 0 gate: prove resolve() reproduces real Polymarket settlements.

Scores every candidate interpretation of the resolution rule against markets
that have already settled, and reports which one reproduces history exactly.

Nothing downstream in this project is trustworthy until a strategy scores 100%
on the uncensored markets using the `wunderground` source -- Weather Underground
is what Polymarket actually settles against, and it is not always equal to the
raw METAR record. Run with `--source iem` to see that gap for yourself.

Usage:
    py scripts/validate_resolve.py
    py scripts/validate_resolve.py --source iem --start 2025-01-01
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.anomalies import KNOWN_SETTLEMENT_ANOMALIES  # noqa: E402
from weatherbot.config import STATION_ICAO, ensure_dirs  # noqa: E402
from weatherbot.resolve import Strategy, all_strategies, daily_maxima  # noqa: E402
from weatherbot.sources import iem, wunderground  # noqa: E402
from weatherbot.sources.polymarket import fetch_resolved_range  # noqa: E402

SOURCES = {"wunderground": wunderground, "iem": iem}

# Minimum number of settled, non-censored markets required for the gate to
# count as meaningful evidence rather than a small-sample coincidence.
MIN_EXACT_MARKETS = 30


def parse_args() -> argparse.Namespace:
    today = date.today()
    parser = argparse.ArgumentParser(description=__doc__)
    # IEM is the default because it empirically reproduces settlement best
    # (503/504 vs 502/504). Weather Underground is nominally the settlement
    # source, but its archive is mutable -- see weatherbot.anomalies.
    parser.add_argument("--source", choices=sorted(SOURCES), default="iem")
    parser.add_argument(
        "--start",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date(2025, 1, 1),
    )
    parser.add_argument(
        "--end",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=today,
    )
    parser.add_argument("--no-cache", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()
    use_cache = not args.no_cache
    source = SOURCES[args.source]

    print(f"Station    : {STATION_ICAO}")
    print(f"Source     : {args.source}")
    print(f"Date range : {args.start} -> {args.end}")

    print(f"\nFetching observations from {args.source} ...")
    # Pad either side so local-vs-UTC day boundaries are fully covered.
    observations = source.fetch_observations(
        STATION_ICAO,
        args.start - timedelta(days=1),
        args.end + timedelta(days=1),
        use_cache=use_cache,
    )
    print(f"  {len(observations):,} observations")

    print("Fetching resolved markets from Polymarket Gamma ...")
    markets = fetch_resolved_range(args.start, args.end, use_cache=use_cache)
    exact = [m for m in markets if not m.is_censored]
    censored = [m for m in markets if m.is_censored]
    print(f"  {len(markets)} resolved markets ({len(exact)} pinned, {len(censored)} open-ended)")

    if not markets:
        print("\nNo resolved markets found. Widen the date range.")
        return 1

    print("\n" + "=" * 78)
    print(f"{'strategy':<44}{'pinned':>10}{'all':>10}{'rate':>12}")
    print("=" * 78)

    results: list[tuple[Strategy, int, int, float]] = []
    for strategy in all_strategies():
        maxima = daily_maxima(observations, strategy)

        def reconstructed(day):
            return maxima[day].tmax_c if day in maxima else None

        exact_hits = sum(1 for m in exact if m.agrees_with(reconstructed(m.day)))
        all_hits = sum(1 for m in markets if m.agrees_with(reconstructed(m.day)))
        rate = exact_hits / len(exact) if exact else 0.0
        results.append((strategy, exact_hits, all_hits, rate))

        print(
            f"{str(strategy):<44}{exact_hits:>6}/{len(exact):<3}"
            f"{all_hits:>6}/{len(markets):<3}{rate:>11.1%}"
        )

    print("=" * 78)

    results.sort(key=lambda r: (r[3], r[2]), reverse=True)
    best_strategy, best_exact, _, best_rate = results[0]
    print(f"\nBest strategy: {best_strategy}  ({best_rate:.1%} on pinned markets)")

    maxima = daily_maxima(observations, best_strategy)
    mismatches = [
        m
        for m in markets
        if not m.agrees_with(maxima[m.day].tmax_c if m.day in maxima else None)
    ]

    unexplained = [m for m in mismatches if m.day not in KNOWN_SETTLEMENT_ANOMALIES]

    if mismatches:
        print(f"\n{len(mismatches)} mismatch(es) under the best strategy:")
        print(f"  {'date':<12}{'settled':<18}{'recon':<8}{'obs':>5}  {'status'}")
        for market in mismatches:
            daily = maxima.get(market.day)
            recon = f"{daily.tmax_c}C" if daily else "NO DATA"
            n_obs = daily.n_obs if daily else 0
            status = (
                "known anomaly"
                if market.day in KNOWN_SETTLEMENT_ANOMALIES
                else "*** UNEXPLAINED ***"
            )
            print(
                f"  {market.day.isoformat():<12}{market.winner.label:<18}"
                f"{recon:<8}{n_obs:>5}  {status}"
            )

    print("\n" + "-" * 78)
    if len(exact) < MIN_EXACT_MARKETS:
        print(
            f"GATE INCOMPLETE: only {len(exact)} pinned markets "
            f"(need >= {MIN_EXACT_MARKETS}). Widen --start."
        )
        return 1

    if unexplained:
        print(f"GATE FAILED: {len(unexplained)} unexplained mismatch(es).")
        print("Investigate before training or trading on this reconstruction.")
        print("If a mismatch turns out to be another feed gap, document it in")
        print("src/weatherbot/anomalies.py with evidence -- do not just widen the gate.")
        return 1

    print(f"GATE PASSED: {best_strategy} on '{args.source}'")
    print(f"             {best_exact}/{len(exact)} pinned settlements ({best_rate:.2%}),")
    print(f"             {len(mismatches)} mismatch(es), all documented feed anomalies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
