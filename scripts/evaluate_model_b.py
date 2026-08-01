"""Model B — does using a forecast at all beat the structural baseline?

Scores a lead-indexed forecast MOS against the Phase 2 benchmark on the same
held-out markets, so the numbers are directly comparable.

Leakage control: the error distribution is fitted only on days before the
evaluation window, and separately per lead time. Scoring a lead-1 model on
data that helped fit it would flatter it badly.

Usage:
    py scripts/evaluate_model_b.py
    py scripts/evaluate_model_b.py --model gfs_seamless
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.config import STATION_ICAO, ensure_dirs  # noqa: E402
from weatherbot.dataset import build_training_pairs, forecast_daily_max  # noqa: E402
from weatherbot.evaluation import reliability, summarise, uniform_forecast  # noqa: E402
from weatherbot.models.forecast_mos import LeadIndexedMOS  # noqa: E402
from weatherbot.models.positional import PositionalClimatology, observed_index  # noqa: E402
from weatherbot.resolve import Strategy, daily_maxima  # noqa: E402
from weatherbot.sources import iem  # noqa: E402
from weatherbot.sources.polymarket import fetch_resolved_range  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="ecmwf_ifs025")
    parser.add_argument("--leads", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    parser.add_argument(
        "--eval-start",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date(2026, 1, 1),
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

    print(f"Station : {STATION_ICAO}   Forecast model: {args.model}")
    print(f"Evaluate: {args.eval_start} -> {args.eval_end}\n")

    observations = iem.fetch_observations(
        STATION_ICAO, date(2024, 1, 1), args.eval_end + timedelta(days=1)
    )
    observed = {d: m.tmax_c for d, m in daily_maxima(observations, Strategy()).items()}

    train_markets = fetch_resolved_range(date(2025, 1, 1), args.eval_start)
    eval_markets = fetch_resolved_range(args.eval_start, args.eval_end)

    positional = PositionalClimatology().fit(
        [
            (idx, len(m.buckets))
            for m in train_markets
            if (idx := observed_index(m.buckets, observed.get(m.day, -999))) is not None
        ]
    )

    mos = LeadIndexedMOS()
    forecasts_by_lead: dict[int, dict[date, float]] = {}
    print(f"{'lead':<6}{'train pairs':>12}{'mean err':>10}{'sd err':>9}")
    for lead in args.leads:
        daily = forecast_daily_max(args.model, lead)
        forecasts_by_lead[lead] = daily
        pairs = build_training_pairs(daily, observed, end=args.eval_start)
        try:
            mos.fit_lead(lead, pairs)
        except ValueError as exc:
            print(f"  lead {lead}: {exc}")
            continue
        model = mos.model_for(lead)
        print(
            f"{lead:<6}{model.n_training_pairs:>12}"
            f"{model.mean_error:>+10.3f}{model.error_stdev:>9.3f}"
        )

    print("\n" + "=" * 78)
    print("MODEL B vs BENCHMARK (same held-out markets)")
    print("=" * 78)
    print(f"  {'lead':<6}{'n':>6}{'RPS':>10}{'logloss':>10}"
          f"{'vs positional':>16}{'vs uniform':>13}")

    best_rps = None
    best_lead = None
    stash: dict[int, tuple[list, list, list]] = {}

    for lead in sorted(forecasts_by_lead):
        if lead not in mos.leads:
            continue
        model_f, pos_f, uni_f, outcomes = [], [], [], []
        for market in eval_markets:
            truth = observed.get(market.day)
            forecast = forecasts_by_lead[lead].get(market.day)
            if truth is None or forecast is None:
                continue
            index = observed_index(market.buckets, truth)
            if index is None:
                continue
            try:
                probs = mos.predict(lead, forecast).to_buckets(market.buckets)
            except ValueError:
                continue
            model_f.append(probs)
            pos_f.append(positional.predict(len(market.buckets)))
            uni_f.append(uniform_forecast(len(market.buckets)))
            outcomes.append(index)

        if not outcomes:
            print(f"  {lead:<6}     0   (no overlapping days)")
            continue

        m = summarise(model_f, outcomes)
        p = summarise(pos_f, outcomes)
        u = summarise(uni_f, outcomes)
        stash[lead] = (model_f, outcomes, [])
        print(
            f"  {lead:<6}{m.n:>6}{m.rps:>10.5f}{m.log_loss:>10.4f}"
            f"{m.skill_against(p)['rps_skill']:>15.1%}"
            f"{m.skill_against(u)['rps_skill']:>12.1%}"
        )
        print(f"        {'':6}{'(positional ' + format(p.rps, '.5f') + ')':>26}")
        if best_rps is None or m.rps < best_rps:
            best_rps, best_lead = m.rps, lead

    print("=" * 78)
    if best_lead is None:
        print("Nothing scored.")
        return 1

    print(f"\nBest lead: {best_lead} at RPS {best_rps:.5f}")

    model_f, outcomes, _ = stash[best_lead]
    print("\n" + "-" * 78)
    print(f"RELIABILITY — Model B, lead {best_lead}")
    print("-" * 78)
    print(f"  {'range':<14}{'n':>7}{'forecast':>11}{'observed':>11}{'gap':>9}")
    for b in reliability(model_f, outcomes, n_bins=10):
        if b.count == 0:
            continue
        print(
            f"  {b.lower:.1f}-{b.upper:.1f}      {b.count:>7}"
            f"{b.mean_forecast:>11.4f}{b.observed_frequency:>11.4f}{b.gap:>+9.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
