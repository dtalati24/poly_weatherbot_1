"""Model D — does the intraday nowcast beat the market, hour by hour?

Phase 4 established the only benchmark that matters: RPS against the market's
own midpoints at the same instant. Model B lost that comparison by ~20% at
leads 1 and 2. This asks the same question *during* the settlement day, where
the model holds something the earlier comparison could not use -- the running
maximum, which is a piece of the answer rather than a guess at it.

The comparison is deliberately hour-by-hour rather than pooled. The whole thesis
is that the model's advantage grows through the day as the running maximum fixes
more of the answer, so a single pooled number would average away the shape that
decides when to quote.

Leakage control:
  - The rise distribution is fitted only on days strictly before --eval-start.
  - At each instant, `state_at` admits only observations with valid_utc at or
    before that instant.
  - Model B uses the archived lead-1 forecast, fitted before --eval-start.
  - Market prices are read backwards from the same instant.

One honesty caveat, stated because a backtest cannot fix it: observations are
filtered on their *valid* time, not on when they reached us. METARs transmit
with a lag, so live performance will be slightly worse than this at any hour.

Usage:
    py scripts/evaluate_model_d.py
    py scripts/evaluate_model_d.py --hours 9 12 15 18 --eval-start 2026-01-01
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.config import STATION_ICAO, ensure_dirs  # noqa: E402
from weatherbot.dataset import (  # noqa: E402
    forecast_daily_max,
    load_hourly,
    remaining_forecast_max,
)
from weatherbot.evaluation import summarise  # noqa: E402
from weatherbot.intraday import (  # noqa: E402
    build_nowcast_samples,
    build_remaining_max_samples,
    group_by_local_day,
    local_instant,
    state_at,
)
from weatherbot.market import observed_index, snapshot  # noqa: E402
from weatherbot.models.forecast_mos import LeadIndexedMOS  # noqa: E402
from weatherbot.models.nowcast import (  # noqa: E402
    ForecastConditionedNowcast,
    IntradayNowcast,
    RemainingMaxNowcast,
)
from weatherbot.priceharvest import archived_days, load_day  # noqa: E402
from weatherbot.resolve import Strategy, daily_maxima  # noqa: E402
from weatherbot.sources import iem  # noqa: E402

TRAIN_START = date(2014, 1, 1)  # station homogeneous from here; see Phase 2.


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="ecmwf_ifs025")
    parser.add_argument(
        "--hours", type=float, nargs="+",
        default=[6, 9, 11, 13, 15, 17, 19, 21],
    )
    parser.add_argument("--eval-start", type=_date, default=date(2025, 7, 1))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()
    hours = tuple(args.hours)
    # Fit on every hour: the lock-in curve is steep and fitting it only at the
    # evaluation hours would leave the kernel interpolating across gaps.
    train_hours = tuple(float(h) for h in range(24))

    price_days = [d for d in archived_days() if d >= args.eval_start]
    if not price_days:
        print("No price archive at or after eval start.")
        return 1

    print(f"Station : {STATION_ICAO}   Forecast model: {args.model}")
    print(f"Evaluate: {min(price_days)} -> {max(price_days)} ({len(price_days)} days)")
    print(f"Train   : {TRAIN_START} -> {args.eval_start} (exclusive)\n")

    observations = iem.fetch_observations(
        STATION_ICAO, TRAIN_START, max(price_days) + timedelta(days=1)
    )
    by_day = group_by_local_day(observations)
    settled = {d: m.tmax_c for d, m in daily_maxima(observations, Strategy()).items()}
    forecasts = forecast_daily_max(args.model, 1)
    hourly = load_hourly(args.model, 1)

    # Precompute the remaining-hours forecast maximum for every (day, hour) we
    # will ever ask about, so the O(n) scan over hourly keys happens once.
    all_days = sorted(set(by_day) | set(forecasts))
    remaining_fc: dict[tuple[date, float], float] = {}
    for day in all_days:
        for hour in sorted(set(hours) | set(train_hours)):
            value = remaining_forecast_max(hourly, day, hour)
            if value is not None:
                remaining_fc[(day, hour)] = value

    # --- Fit ---------------------------------------------------------------
    train_rows = build_nowcast_samples(
        by_day, settled, train_hours, start=TRAIN_START, end=args.eval_start
    )
    nowcast = IntradayNowcast().fit([(h, r) for h, _, r in train_rows])

    cond_rows = build_nowcast_samples(
        by_day, settled, train_hours,
        start=TRAIN_START, end=args.eval_start, forecasts=forecasts,
    )
    conditioned = ForecastConditionedNowcast().fit(cond_rows)

    rem_rows = build_remaining_max_samples(
        by_day, remaining_fc, train_hours, start=TRAIN_START, end=args.eval_start
    )
    remaining_model = RemainingMaxNowcast().fit(rem_rows)

    mos = LeadIndexedMOS()
    mos.fit_lead(
        1,
        [(f, settled[d]) for d, f in forecasts.items()
         if d in settled and d < args.eval_start],
    )

    print(f"Model D  : {nowcast.n_training_samples} samples "
          f"({len(train_rows) // len(train_hours)} days x {len(train_hours)} hours)")
    print(f"Model D' : gap buckets fitted {conditioned.fitted_gaps}\n")

    print("LOCK-IN CURVE — P(the maximum is already set), from training data")
    print(f"  {'hour':>6}{'P(locked)':>12}{'E[rise]':>10}")
    for hour in hours:
        print(f"  {hour:>6.0f}{nowcast.probability_locked(hour):>12.3f}"
              f"{nowcast.expected_rise(hour):>10.2f}")

    # --- Score -------------------------------------------------------------
    print("\n" + "=" * 78)
    print("MODEL D vs MARKET — same instant, same buckets, RPS (lower is better)")
    print("=" * 78)
    print(f"  {'hour':>5}{'n':>6}{'market':>10}{'D':>9}{'D-prime':>10}"
          f"{'D-remain':>11}{'model B':>10}{'best vs mkt':>14}")

    totals = {"market": [], "d": [], "dc": [], "dr": [], "b": [], "out": []}

    for hour in hours:
        mkt_f, d_f, dc_f, dr_f, b_f, outcomes = [], [], [], [], [], []

        for day in price_days:
            truth = settled.get(day)
            forecast = forecasts.get(day)
            if truth is None or forecast is None or day not in by_day:
                continue
            moment = local_instant(day, hour)
            state = state_at(by_day[day], day, moment)
            if state.running_max is None:
                continue
            snap = snapshot(load_day(day), int(moment.timestamp()))
            if snap is None:
                continue
            index = observed_index(snap.buckets, truth)
            if index is None:
                continue
            try:
                d_probs = nowcast.predict(hour, state.running_max).to_buckets(snap.buckets)
                dc_probs = conditioned.predict(
                    hour, state.running_max, forecast
                ).to_buckets(snap.buckets)
                b_probs = mos.predict(1, forecast).to_buckets(snap.buckets)
                dr_probs = remaining_model.predict(
                    hour, state.running_max, remaining_fc.get((day, hour))
                ).to_buckets(snap.buckets)
            except (ValueError, KeyError):
                continue

            mkt_f.append(snap.probabilities)
            d_f.append(d_probs)
            dc_f.append(dc_probs)
            dr_f.append(dr_probs)
            b_f.append(b_probs)
            outcomes.append(index)

        if not outcomes:
            print(f"  {hour:>5.0f}     0   (no clean instants)")
            continue

        mk = summarise(mkt_f, outcomes)
        d = summarise(d_f, outcomes)
        dc = summarise(dc_f, outcomes)
        dr = summarise(dr_f, outcomes)
        b = summarise(b_f, outcomes)
        best = min(d.rps, dc.rps, dr.rps)
        print(
            f"  {hour:>5.0f}{mk.n:>6}{mk.rps:>10.5f}{d.rps:>9.5f}{dc.rps:>10.5f}"
            f"{dr.rps:>11.5f}{b.rps:>10.5f}{(mk.rps - best) / mk.rps:>13.1%}"
        )

        totals["market"] += mkt_f
        totals["d"] += d_f
        totals["dc"] += dc_f
        totals["dr"] += dr_f
        totals["b"] += b_f
        totals["out"] += outcomes

    print("=" * 78)
    if not totals["out"]:
        print("Nothing scored.")
        return 1

    mk = summarise(totals["market"], totals["out"])
    d = summarise(totals["d"], totals["out"])
    dc = summarise(totals["dc"], totals["out"])
    dr = summarise(totals["dr"], totals["out"])
    b = summarise(totals["b"], totals["out"])
    print(f"  {'ALL':>5}{mk.n:>6}{mk.rps:>10.5f}{d.rps:>9.5f}{dc.rps:>10.5f}"
          f"{dr.rps:>11.5f}{b.rps:>10.5f}"
          f"{(mk.rps - min(d.rps, dc.rps, dr.rps)) / mk.rps:>13.1%}")
    print("\n  'best vs mkt' > 0 means a nowcast beat the market at that hour.")
    print("  Pooled 'ALL' is dominated by whichever hours are most numerous;")
    print("  the hour-by-hour shape is the result, not the pooled figure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
