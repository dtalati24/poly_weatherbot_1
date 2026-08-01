"""Is the model's contribution to a blend distinguishable from zero?

`analyze_prices.py` finds that a blend of model and market beats the market by
1-2% RPS at a best weight of about 0.2. Two things could make that illusory:

  - the weight is chosen on the same days it is scored on, and
  - 300-400 markets is not many when the effect is a couple of percent.

So this does two things the headline number cannot. It **splits the sample**,
choosing the weight on the first half and scoring it on the second, and it runs
a **paired bootstrap** over days on that held-out half. Pairing matters: model
and market see the same weather, so their scores are strongly correlated and an
unpaired test would be far too conservative.

Usage:
    py scripts/blend_significance.py
    py scripts/blend_significance.py --iterations 20000
"""

from __future__ import annotations

import argparse
import pathlib
import random
import sys
from datetime import date, datetime, time, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.config import LOCAL_TZ, STATION_ICAO, ensure_dirs  # noqa: E402
from weatherbot.dataset import forecast_daily_max  # noqa: E402
from weatherbot.evaluation import ranked_probability_score  # noqa: E402
from weatherbot.market import observed_index, snapshot  # noqa: E402
from weatherbot.models.forecast_mos import LeadIndexedMOS  # noqa: E402
from weatherbot.priceharvest import archived_days, load_day  # noqa: E402
from weatherbot.resolve import Strategy, daily_maxima  # noqa: E402
from weatherbot.sources import iem  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="ecmwf_ifs025")
    parser.add_argument("--leads", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--hour", type=int, default=12)
    parser.add_argument("--eval-start", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                        default=date(2025, 7, 1))
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260801)
    return parser.parse_args()


def mix(model, market, w):
    return tuple(w * m + (1 - w) * q for m, q in zip(model, market))


def mean_rps(forecasts, outcomes) -> float:
    return sum(
        ranked_probability_score(f, o) for f, o in zip(forecasts, outcomes)
    ) / len(outcomes)


def main() -> int:
    args = parse_args()
    ensure_dirs()
    rng = random.Random(args.seed)

    price_days = [d for d in archived_days() if d >= args.eval_start]
    observations = iem.fetch_observations(
        STATION_ICAO, date(2023, 1, 1), max(price_days) + timedelta(days=1)
    )
    observed = {d: m.tmax_c for d, m in daily_maxima(observations, Strategy()).items()}

    print(f"Evaluate  : {min(price_days)} -> {max(price_days)}")
    print(f"Bootstrap : {args.iterations} paired resamples over days\n")

    for lead in args.leads:
        daily = forecast_daily_max(args.model, lead)
        static = LeadIndexedMOS()
        try:
            static.fit_lead(
                lead,
                [(f, observed[d]) for d, f in daily.items()
                 if d in observed and d < args.eval_start],
            )
        except ValueError as exc:
            print(f"lead {lead}: {exc}")
            continue

        rows = []
        for day in price_days:
            truth, forecast = observed.get(day), daily.get(day)
            if truth is None or forecast is None:
                continue
            naive = datetime.combine(day - timedelta(days=lead), time(hour=args.hour))
            snap = snapshot(load_day(day), int(naive.replace(tzinfo=LOCAL_TZ).timestamp()))
            if snap is None:
                continue
            index = observed_index(snap.buckets, truth)
            if index is None:
                continue
            try:
                model_probs = static.predict(lead, forecast).to_buckets(snap.buckets)
            except ValueError:
                continue
            rows.append((model_probs, snap.probabilities, index))

        if len(rows) < 60:
            print(f"lead {lead}: only {len(rows)} usable days; skipping")
            continue

        # Chronological split: choose the weight on the past, score on the future.
        cut = len(rows) // 2
        train, test = rows[:cut], rows[cut:]

        weights = [w / 20 for w in range(21)]
        best_w = min(
            weights,
            key=lambda w: mean_rps([mix(m, q, w) for m, q, _ in train],
                                   [o for _, _, o in train]),
        )

        test_outcomes = [o for _, _, o in test]
        market_rps = mean_rps([q for _, q, _ in test], test_outcomes)
        blend_rps = mean_rps([mix(m, q, best_w) for m, q, _ in test], test_outcomes)
        gain = (market_rps - blend_rps) / market_rps

        # Paired bootstrap on the per-day RPS difference.
        diffs = [
            ranked_probability_score(q, o) - ranked_probability_score(mix(m, q, best_w), o)
            for m, q, o in test
        ]
        n = len(diffs)
        means = sorted(
            sum(diffs[rng.randrange(n)] for _ in range(n)) / n
            for _ in range(args.iterations)
        )
        lo = means[int(0.025 * args.iterations)]
        hi = means[int(0.975 * args.iterations)]
        p_no_gain = sum(1 for m in means if m <= 0) / args.iterations

        print(f"lead {lead}   train {len(train)} days -> w={best_w:.2f}, "
              f"test {len(test)} days")
        print(f"  market RPS        {market_rps:.5f}")
        print(f"  blend  RPS        {blend_rps:.5f}   ({gain:+.1%})")
        print(f"  mean RPS gain     {sum(diffs) / n:+.5f}")
        print(f"  95% CI            [{lo:+.5f}, {hi:+.5f}]")
        print(f"  P(gain <= 0)      {p_no_gain:.3f}")
        verdict = (
            "distinguishable from zero" if lo > 0
            else "NOT distinguishable from zero"
        )
        print(f"  => {verdict}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
