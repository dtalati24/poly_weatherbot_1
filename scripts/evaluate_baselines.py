"""Establish the benchmark every forecast model must beat.

Scores three baselines, none of which use any weather forecast:

    uniform      1/K on every bucket. A floor, not a benchmark.
    climatology  Model A. Day-of-year distribution of the settlement variable.
    positional   Model A'. Where in the bucket window outcomes tend to land.

The headline result is that plain temperature climatology scores *worse than
uniform* here. That is structural, not a bug: Polymarket centres the bucket
window on its own forecast, so a day-of-year model piles mass onto whichever
tail the season implies and RPS punishes it for the distance. See
weatherbot.models.positional.

Leakage control: climatology is fit only on observations before the evaluation
window, and positional climatology only on markets that settled before it.

Usage:
    py scripts/evaluate_baselines.py
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.config import STATION_ICAO, ensure_dirs  # noqa: E402
from weatherbot.evaluation import reliability, summarise, uniform_forecast  # noqa: E402
from weatherbot.models.climatology import HOMOGENEOUS_START, ClimatologyModel  # noqa: E402
from weatherbot.models.positional import PositionalClimatology, observed_index  # noqa: E402
from weatherbot.resolve import Strategy, daily_maxima  # noqa: E402
from weatherbot.sources import iem  # noqa: E402
from weatherbot.sources.polymarket import fetch_resolved_range  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--train-start",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=HOMOGENEOUS_START,
    )
    parser.add_argument(
        "--eval-start",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date(2026, 1, 1),
        help="Markets only exist from 2025, so positional climatology needs a "
             "later split than the observation record would allow.",
    )
    parser.add_argument(
        "--eval-end",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date(2026, 8, 1),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()

    print(f"Station  : {STATION_ICAO}")
    print(f"Train    : observations {args.train_start} -> {args.eval_start}")
    print(f"           markets      2025-01-01 -> {args.eval_start}")
    print(f"Evaluate : {args.eval_start} -> {args.eval_end}\n")

    observations = iem.fetch_observations(
        STATION_ICAO,
        args.train_start - timedelta(days=1),
        args.eval_end + timedelta(days=1),
    )
    maxima = {d: m.tmax_c for d, m in daily_maxima(observations, Strategy()).items()}
    print(f"{len(maxima):,} daily maxima")

    climatology = ClimatologyModel().fit(
        maxima, start=args.train_start, end=args.eval_start
    )
    print(
        f"climatology fit on {climatology.n_training_days:,} days, "
        f"trend {climatology.trend_c_per_decade:+.3f} C/decade"
    )

    train_markets = fetch_resolved_range(date(2025, 1, 1), args.eval_start)
    eval_markets = fetch_resolved_range(args.eval_start, args.eval_end)

    training_pairs = []
    for market in train_markets:
        observed = maxima.get(market.day)
        if observed is None:
            continue
        index = observed_index(market.buckets, observed)
        if index is not None:
            training_pairs.append((index, len(market.buckets)))

    positional = PositionalClimatology().fit(training_pairs)
    print(f"positional fit on {positional.n_training_markets} settled markets")
    print(f"evaluating on {len(eval_markets)} settled markets\n")

    forecasts: dict[str, list[tuple[float, ...]]] = {
        "uniform": [],
        "climatology": [],
        "positional": [],
    }
    outcomes: list[int] = []
    skipped = 0

    for market in eval_markets:
        observed = maxima.get(market.day)
        if observed is None:
            skipped += 1
            continue
        index = observed_index(market.buckets, observed)
        if index is None:
            skipped += 1
            continue

        try:
            clim = climatology.predict(market.day).to_buckets(market.buckets)
        except (ValueError, RuntimeError):
            skipped += 1
            continue

        forecasts["uniform"].append(uniform_forecast(len(market.buckets)))
        forecasts["climatology"].append(clim)
        forecasts["positional"].append(positional.predict(len(market.buckets)))
        outcomes.append(index)

    if not outcomes:
        print("Nothing scored.")
        return 1

    print(f"scored {len(outcomes)}, skipped {skipped}\n")

    summaries = {name: summarise(f, outcomes) for name, f in forecasts.items()}
    reference = summaries["uniform"]

    print("=" * 74)
    print("BASELINES (no forecast information used by any of these)")
    print("=" * 74)
    print(f"  {'model':<14}{'RPS':>10}{'logloss':>10}{'brier':>9}{'RPS skill vs uniform':>23}")
    for name in ("uniform", "climatology", "positional"):
        s = summaries[name]
        skill = "" if name == "uniform" else f"{s.skill_against(reference)['rps_skill']:+.1%}"
        print(f"  {name:<14}{s.rps:>10.5f}{s.log_loss:>10.4f}{s.brier:>9.4f}{skill:>23}")
    print("=" * 74)

    best = min(summaries, key=lambda n: summaries[n].rps)
    print(f"\nBenchmark to beat: '{best}' at RPS {summaries[best].rps:.5f}")
    if summaries["climatology"].rps > reference.rps:
        print(
            "\nNote: temperature climatology is WORSE than uniform. This is "
            "expected --\nPolymarket centres the bucket window on its own "
            "forecast, so a day-of-year\nmodel is systematically far from the "
            "answer in bucket distance."
        )

    print("\n" + "-" * 74)
    print(f"RELIABILITY — {best}")
    print("-" * 74)
    print(f"  {'range':<14}{'n':>7}{'forecast':>11}{'observed':>11}{'gap':>9}")
    for b in reliability(forecasts[best], outcomes, n_bins=10):
        if b.count == 0:
            continue
        print(
            f"  {b.lower:.1f}-{b.upper:.1f}      {b.count:>7}"
            f"{b.mean_forecast:>11.4f}{b.observed_frequency:>11.4f}{b.gap:>+9.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
