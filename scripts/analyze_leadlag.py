"""Phase 6b — does Kalshi move before Polymarket?

Phase 6 asked whether Kalshi's *level* is a better estimate of Polymarket's
settlement. It is not. This asks a different and more plausible question: when
new information arrives, which venue prices it first?

That distinction matters because the two failure modes are unrelated. Kalshi can
be a permanently worse estimate of Polymarket's settled value -- it is, because
it settles on a variable that runs 0.83 F hotter -- while still reacting to a
new observation sooner. A constant offset destroys the level comparison and
leaves the timing comparison untouched.

Method. Both venues are reduced to one number per instant: the implied mean
temperature in Fahrenheit, which is comparable across venues even though their
bucket ladders differ and their settlement variables differ by a constant. Then
cross-correlate the two series of *changes*:

    corr( dKalshi(t-w, t),  dPoly(t, t+w) )     Kalshi leads if positive
    corr( dPoly(t-w, t),    dKalshi(t, t+w) )   Polymarket leads if positive

Differencing is what makes the constant offset harmless: the +0.83 F gap is
level, not slope, so it vanishes under the difference.

A real lead shows up as an asymmetry between those two numbers. Both being
positive just means the venues co-move, which tells us nothing about who is
first.

Usage:
    py scripts/analyze_leadlag.py
    py scripts/analyze_leadlag.py --window 30 --step 10
"""

from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from weatherbot.cities import LOS_ANGELES as LA  # noqa: E402
from weatherbot.config import DATA_DIR, ensure_dirs  # noqa: E402
from weatherbot.market import snapshot as poly_snapshot  # noqa: E402
from weatherbot.priceharvest import archived_days, load_day  # noqa: E402
from weatherbot.sources import kalshi  # noqa: E402
from weatherbot.sources.polymarket import BucketKind  # noqa: E402

CACHE = DATA_DIR / "raw" / "kalshi_candles.json.gz"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", type=int, default=30,
                        help="Minutes over which a change is measured.")
    parser.add_argument("--step", type=int, default=10,
                        help="Minutes between sample points.")
    parser.add_argument("--start-hour", type=float, default=-14)
    parser.add_argument("--end-hour", type=float, default=14)
    return parser.parse_args()


def implied_mean_f(buckets, probabilities) -> float | None:
    """Probability-weighted centre of a bucket ladder, in Fahrenheit.

    Open-ended tails have no centre, so they are dropped and the rest
    renormalised. Both venues are treated identically, so any bias this
    introduces is common to both and cancels in the difference.
    """
    pairs = [
        ((b.low + b.high) / 2.0, p)
        for b, p in zip(buckets, probabilities)
        if b.kind is BucketKind.RANGE
    ]
    weight = sum(p for _, p in pairs)
    if weight <= 0.5:  # tails hold most of the mass; the centre is meaningless
        return None
    return sum(c * p for c, p in pairs) / weight


def correlate(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 30:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / (sxx * syy) ** 0.5


def main() -> int:
    args = parse_args()
    ensure_dirs()

    if not CACHE.exists():
        print("No Kalshi candle cache. Run scripts/evaluate_crossvenue.py first.")
        return 1
    with gzip.open(CACHE, "rt", encoding="utf-8") as fh:
        candles = json.load(fh)

    by_day = kalshi.markets_by_day(LA.kalshi_series)
    poly_days = set(archived_days(city=LA))
    common = sorted(set(by_day) & poly_days)
    print(f"City   : {LA.name}   days: {len(common)}")
    print(f"Window : {args.window} min changes, sampled every {args.step} min\n")

    # Build both venues' implied-mean series on a shared grid, per day.
    series: list[list[tuple[int, float, float]]] = []
    for day in common:
        kseries, kbuckets = {}, {}
        for market in by_day[day]:
            rows = candles.get(market.ticker) or []
            kseries[market.ticker] = [
                kalshi.KalshiCandle(ts=r[0], bid=r[1], ask=r[2], mean=None,
                                    volume=r[3], open_interest=0.0)
                for r in rows
            ]
            kbuckets[market.ticker] = market.bucket

        pday = load_day(day, city=LA)
        if not pday:
            continue

        midnight = datetime.combine(day, datetime.min.time()).replace(tzinfo=LA.tz)
        base = int(midnight.timestamp())

        row: list[tuple[int, float, float]] = []
        minute = int(args.start_hour * 60)
        while minute <= int(args.end_hour * 60):
            ts = base + minute * 60
            ksnap = kalshi.snapshot_from_series(kseries, kbuckets, ts, min_buckets=4)
            psnap = poly_snapshot(pday, ts, max_staleness=3600, require_all=False)
            if ksnap is not None and psnap is not None:
                km = implied_mean_f(ksnap.buckets, ksnap.probabilities)
                pm = implied_mean_f(psnap.buckets, psnap.probabilities)
                if km is not None and pm is not None:
                    row.append((ts, km, pm))
            minute += args.step
        if len(row) > 10:
            series.append(row)

    print(f"Usable days: {len(series)}   "
          f"total sample points: {sum(len(r) for r in series)}\n")

    span = max(1, args.window // args.step)

    print("=" * 74)
    print("LEAD-LAG — correlation of one venue's past move with the other's next")
    print("=" * 74)
    print(f"  {'lag (min)':>10}{'K past -> P next':>20}{'P past -> K next':>20}{'n':>8}")

    for multiple in (1, 2, 3, 6):
        lag = span * multiple
        k_lead_x, k_lead_y, p_lead_x, p_lead_y = [], [], [], []
        for row in series:
            for i in range(span, len(row) - lag):
                dk_past = row[i][1] - row[i - span][1]
                dp_past = row[i][2] - row[i - span][2]
                dk_next = row[i + lag][1] - row[i][1]
                dp_next = row[i + lag][2] - row[i][2]
                k_lead_x.append(dk_past); k_lead_y.append(dp_next)
                p_lead_x.append(dp_past); p_lead_y.append(dk_next)

        kp = correlate(k_lead_x, k_lead_y)
        pk = correlate(p_lead_x, p_lead_y)
        print(f"  {lag * args.step:>10}{kp:>20.4f}{pk:>20.4f}{len(k_lead_x):>8}")

    print("=" * 74)
    print("  A genuine lead is an ASYMMETRY between the two columns.")
    print("  Both positive and equal means they simply co-move.")

    # Contemporaneous correlation, for scale.
    now_x, now_y = [], []
    for row in series:
        for i in range(span, len(row)):
            now_x.append(row[i][1] - row[i - span][1])
            now_y.append(row[i][2] - row[i - span][2])
    print(f"\n  same-window correlation of the two venues' moves: {correlate(now_x, now_y):.4f}")

    # And the level gap, as a sanity check on the Phase 6 kernel.
    gaps = [k - p for row in series for _, k, p in row]
    if gaps:
        gaps.sort()
        print(f"  implied-mean gap (Kalshi - Poly): median {gaps[len(gaps) // 2]:+.2f} F, "
              f"mean {sum(gaps) / len(gaps):+.2f} F")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
