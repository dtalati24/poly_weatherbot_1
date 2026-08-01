"""Phase 4 — the reality check: is the model right about something the market has wrong?

Every score before this compared the model to a *statistical* baseline. That
answers "does the model know something about the weather", which is necessary
and nowhere near sufficient. The question that decides whether any of this is
worth trading is different: at the moment we would have quoted, did the model
disagree with the price, and was it right to?

So this scores model and market **against each other at the same instant**, on
the same buckets, over every settled market day for which prices exist.

Four forecasts are compared:

    market       the midpoint cross-section, normalised
    model B      lead-indexed MOS, fitted once on everything before eval start
    model B'     the same, refitted each day on a trailing window
    positional   the Phase 2 structural baseline, no weather data at all

Leakage control:
  - Model B is fitted strictly before `--eval-start`.
  - Model B' fits only on days strictly before the day it predicts.
  - The comparison instant is `lead` days before the market day at a fixed
    London hour, and every price is read backwards from it.
  - The forecast used at lead L is the archived lead-L forecast.

Usage:
    py scripts/analyze_prices.py
    py scripts/analyze_prices.py --hour 18 --window 120
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, time, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.config import LOCAL_TZ, STATION_ICAO, ensure_dirs  # noqa: E402
from weatherbot.dataset import forecast_daily_max  # noqa: E402
from weatherbot.evaluation import summarise, uniform_forecast  # noqa: E402
from weatherbot.market import observed_index, snapshot  # noqa: E402
from weatherbot.models.forecast_mos import LeadIndexedMOS, RollingMOS  # noqa: E402
from weatherbot.models.positional import PositionalClimatology  # noqa: E402
from weatherbot.models.positional import observed_index as pos_index  # noqa: E402
from weatherbot.priceharvest import archived_days, load_day  # noqa: E402
from weatherbot.resolve import Strategy, daily_maxima  # noqa: E402
from weatherbot.sources import iem  # noqa: E402
from weatherbot.sources.polymarket import fetch_resolved_range  # noqa: E402


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="ecmwf_ifs025")
    parser.add_argument("--leads", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--hour", type=int, default=12, help="London hour to compare at.")
    parser.add_argument("--eval-start", type=_date, default=date(2025, 7, 1))
    parser.add_argument("--window", type=int, default=180, help="Model B' window days.")
    return parser.parse_args()


def compare_instant(day: date, lead: int, hour: int) -> int:
    """Unix seconds for `hour` London time, `lead` days before `day`."""
    naive = datetime.combine(day - timedelta(days=lead), time(hour=hour))
    return int(naive.replace(tzinfo=LOCAL_TZ).timestamp())


def main() -> int:
    args = parse_args()
    ensure_dirs()

    price_days = [d for d in archived_days() if d >= args.eval_start]
    if not price_days:
        print("No price archive at or after eval start. Run harvest_prices.py --all.")
        return 1

    print(f"Station : {STATION_ICAO}   Forecast model: {args.model}")
    print(f"Evaluate: {min(price_days)} -> {max(price_days)} ({len(price_days)} days)")
    print(f"Instant : {args.hour:02d}:00 London, `lead` days before settlement")
    print(f"Model B': trailing {args.window}-day window, refit per day\n")

    observations = iem.fetch_observations(
        STATION_ICAO, date(2023, 1, 1), max(price_days) + timedelta(days=1)
    )
    observed = {d: m.tmax_c for d, m in daily_maxima(observations, Strategy()).items()}

    forecasts: dict[int, dict[date, float]] = {
        lead: forecast_daily_max(args.model, lead) for lead in args.leads
    }

    # --- Fit ---------------------------------------------------------------
    static = LeadIndexedMOS()
    rolling: dict[int, RollingMOS] = {}
    print(f"{'lead':<6}{'B pairs':>9}{'B bias':>9}{'B sd':>8}")
    for lead in args.leads:
        dated = [
            (d, f, observed[d])
            for d, f in sorted(forecasts[lead].items())
            if d in observed
        ]
        rolling[lead] = RollingMOS(window_days=args.window).fit_history(dated)
        pairs = [(f, o) for d, f, o in dated if d < args.eval_start]
        try:
            static.fit_lead(lead, pairs)
            m = static.model_for(lead)
            print(f"{lead:<6}{m.n_training_pairs:>9}{m.mean_error:>+9.3f}"
                  f"{m.error_stdev:>8.3f}")
        except ValueError as exc:
            print(f"{lead:<6} {exc}")

    train_markets = fetch_resolved_range(date(2024, 6, 1), args.eval_start)
    positional = PositionalClimatology().fit(
        [
            (idx, len(mk.buckets))
            for mk in train_markets
            if (idx := pos_index(mk.buckets, observed.get(mk.day, -999))) is not None
        ]
    )

    # --- Score -------------------------------------------------------------
    print("\n" + "=" * 78)
    print("MODEL vs MARKET — same instant, same buckets, RPS (lower is better)")
    print("=" * 78)
    print(f"  {'lead':<6}{'n':>5}{'market':>10}{'model B':>10}{'model B''':>11}"
          f"{'positional':>12}{'best model':>13}")

    rows = []
    blend_inputs: dict[int, dict] = {}
    for lead in args.leads:
        if lead not in static.leads:
            continue
        mkt_f, b_f, br_f, pos_f, uni_f, outcomes = [], [], [], [], [], []
        overrounds, bias_rows = [], []

        for day in price_days:
            truth = observed.get(day)
            forecast = forecasts[lead].get(day)
            if truth is None or forecast is None:
                continue
            snap = snapshot(load_day(day), compare_instant(day, lead, args.hour))
            if snap is None:
                continue
            index = observed_index(snap.buckets, truth)
            if index is None:
                continue
            roll = rolling[lead].predict(day, forecast)
            if roll is None:
                continue
            try:
                b_probs = static.predict(lead, forecast).to_buckets(snap.buckets)
                br_probs = roll.to_buckets(snap.buckets)
            except (ValueError, KeyError):
                continue

            mkt_f.append(snap.probabilities)
            b_f.append(b_probs)
            br_f.append(br_probs)
            pos_f.append(positional.predict(len(snap.buckets)))
            uni_f.append(uniform_forecast(len(snap.buckets)))
            outcomes.append(index)
            overrounds.append(snap.overround)

            implied = snap.implied_mean_c()
            if implied is not None and snap.tail_mass < 0.10:
                bias_rows.append((implied, forecast, truth))

        if not outcomes:
            print(f"  {lead:<6}    0   (no clean snapshots)")
            continue

        mk = summarise(mkt_f, outcomes)
        b = summarise(b_f, outcomes)
        br = summarise(br_f, outcomes)
        p = summarise(pos_f, outcomes)
        best = min(b.rps, br.rps)
        print(
            f"  {lead:<6}{mk.n:>5}{mk.rps:>10.5f}{b.rps:>10.5f}{br.rps:>11.5f}"
            f"{p.rps:>12.5f}{(mk.rps - best) / mk.rps:>12.1%}"
        )
        rows.append((lead, mk, b, br, p, overrounds, bias_rows))
        # Blend against whichever model variant scored better on its own.
        blend_inputs[lead] = {
            "model": b_f if b.rps <= br.rps else br_f,
            "market": mkt_f,
            "outcomes": outcomes,
        }

    print("=" * 78)
    print("  'best model' > 0 means a model beat the market; < 0 means it did not.")
    if not rows:
        print("\nNothing scored.")
        return 1

    # --- Does the model carry information the market lacks? ----------------
    # Losing to the market head-to-head does not mean the model is useless. If
    # its errors are partly orthogonal to the market's, a blend beats both, and
    # the optimal weight measures how much independent signal it holds. A best
    # weight of zero is the genuinely discouraging outcome.
    print("\n" + "-" * 78)
    print("BLEND — w x model + (1-w) x market, best w by RPS")
    print("-" * 78)
    print(f"  {'lead':<6}{'best w':>9}{'blend RPS':>12}{'vs market':>12}"
          f"{'w=0.25':>10}{'w=0.50':>10}")
    for lead, mk, b, br, _, _, _ in rows:
        model_f = blend_inputs[lead]["model"]
        market_f = blend_inputs[lead]["market"]
        outs = blend_inputs[lead]["outcomes"]

        def blended(w: float) -> float:
            mixed = [
                tuple(w * m + (1 - w) * q for m, q in zip(mp, qp))
                for mp, qp in zip(model_f, market_f)
            ]
            return summarise(mixed, outs).rps

        scores = {round(w / 20, 2): blended(w / 20) for w in range(21)}
        best_w = min(scores, key=lambda k: scores[k])
        print(
            f"  {lead:<6}{best_w:>9.2f}{scores[best_w]:>12.5f}"
            f"{(mk.rps - scores[best_w]) / mk.rps:>11.1%}"
            f"{scores[0.25]:>10.5f}{scores[0.5]:>10.5f}"
        )
    print()
    print("  best w = 0     the model adds nothing the market does not have")
    print("  best w > 0     it holds independent signal, even while losing alone")

    # --- Cost of trading ---------------------------------------------------
    print("\n" + "-" * 78)
    print("OVERROUND — what the bucket prices sum to (1.00 would be free)")
    print("-" * 78)
    for lead, _, _, _, _, overrounds, _ in rows:
        ordered = sorted(overrounds)
        median = ordered[len(ordered) // 2]
        print(
            f"  lead {lead}:  median {median:.4f}   min {ordered[0]:.4f}   "
            f"max {ordered[-1]:.4f}   => {(median - 1) * 100:.2f}c across the ladder"
        )

    # --- The Phase 3 bias hypothesis, tested against prices ----------------
    print("\n" + "-" * 78)
    print("BIAS — has the market corrected the forecast, or inherited it?")
    print("-" * 78)
    print(f"  {'lead':<6}{'n':>5}{'obs-implied':>14}{'obs-forecast':>15}"
          f"{'implied-forecast':>19}")
    for lead, _, _, _, _, _, bias_rows in rows:
        if not bias_rows:
            print(f"  {lead:<6}    0   (tails too heavy to read a centre)")
            continue
        n = len(bias_rows)
        print(
            f"  {lead:<6}{n:>5}"
            f"{sum(t - i for i, _, t in bias_rows) / n:>+14.3f}"
            f"{sum(t - f for _, f, t in bias_rows) / n:>+15.3f}"
            f"{sum(i - f for i, f, _ in bias_rows) / n:>+19.3f}"
        )
    print()
    print("  obs-implied      > 0 the market runs cold;  < 0 it runs warm")
    print("  obs-forecast     > 0 the raw forecast runs cold")
    print("  implied-forecast ~ 0 would mean the market has NOT corrected it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
