"""Phase 6 — is Kalshi a better fair value for Polymarket than Polymarket is?

Phases 4 and 5 established that our weather models do not beat Polymarket's own
midpoint. This asks a different question that does not require forecasting
anything: Kalshi runs the same Los Angeles contract, settled on the same
physical station (KLAX), with more volume. If Kalshi's prices are sharper, they
are a fair value we can quote against Polymarket with — and no weather model is
needed at all.

Scored the only way that means anything here: **Kalshi's distribution, expressed
in Polymarket's buckets, against Polymarket's own midpoint, at the same instant,
on Polymarket's settled outcome.**

Results are split by bucket-ladder alignment, and that split is the point. On
the ~half of days where the two ladders share a lattice the mapping is exact; on
the rest a Kalshi bucket straddles two Polymarket buckets and its mass has to be
split by assumption. If Kalshi wins on aligned days but not overall, the problem
is the re-binning rather than the premise.

Leakage control: every price on both venues is read backwards from the
comparison instant, and the outcome comes from observations only.

Usage:
    py scripts/evaluate_crossvenue.py
    py scripts/evaluate_crossvenue.py --hours 9 12 15 18
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
from weatherbot.crossvenue import (  # noqa: E402
    SourceOffset,
    alignment,
    to_poly_buckets,
)
from weatherbot.evaluation import summarise  # noqa: E402
from weatherbot.market import snapshot as poly_snapshot  # noqa: E402
from weatherbot.priceharvest import archived_days, load_day  # noqa: E402
from weatherbot.resolve import Strategy, daily_maxima_fahrenheit  # noqa: E402
from weatherbot.sources import iem, kalshi  # noqa: E402
from weatherbot.sources.polymarket import (  # noqa: E402
    BucketKind,
    fetch_resolved_range,
)

CACHE = DATA_DIR / "raw" / "kalshi_candles.json.gz"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    # Kalshi lists at D-1 07:00 LA and its books go fully one-sided after about
    # 16:00 LA on the settlement day, so there is no fair value to read outside
    # this window. Measured, not assumed -- see docs/PHASE6_CROSSVENUE.md.
    parser.add_argument("--hours", type=float, nargs="+",
                        default=[-18, -12, -6, 0, 6, 9, 12])
    parser.add_argument("--refresh", action="store_true",
                        help="Re-fetch Kalshi candles instead of using the cache.")
    return parser.parse_args()


def local_instant(day: date, hour: float) -> datetime:
    """UTC instant `hour` hours into the LA-local day."""
    midnight = datetime.combine(day, datetime.min.time()).replace(tzinfo=LA.tz)
    return midnight.astimezone(kalshi.timezone.utc) + timedelta(hours=hour)


def load_candles(by_day, refresh: bool) -> dict:
    """Kalshi candles for every bucket of every day, cached to disk.

    414 requests otherwise, every run.
    """
    if CACHE.exists() and not refresh:
        with gzip.open(CACHE, "rt", encoding="utf-8") as fh:
            return json.load(fh)

    out: dict[str, list] = {}
    for index, day in enumerate(sorted(by_day), 1):
        start = int((local_instant(day, -36)).timestamp())
        end = int((local_instant(day, 26)).timestamp())
        for market in by_day[day]:
            candles = kalshi.fetch_candles(
                LA.kalshi_series, market.ticker, start, end, period_minutes=1
            )
            out[market.ticker] = [
                [c.ts, c.bid, c.ask, c.volume] for c in candles
            ]
        if index % 10 == 0:
            print(f"    fetched {index}/{len(by_day)} days")

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(CACHE, "wt", encoding="utf-8") as fh:
        json.dump(out, fh)
    return out


def main() -> int:
    args = parse_args()
    ensure_dirs()

    print(f"City    : {LA.name} ({LA.station})")
    print(f"Kalshi  : {LA.kalshi_series}, settles NWS CLI at {LA.kalshi_station}")
    print("Poly    : Wunderground at KLAX\n")

    by_day = kalshi.markets_by_day(LA.kalshi_series)
    poly_days = set(archived_days(city=LA))
    markets = {
        m.day: m
        for m in fetch_resolved_range(
            min(by_day), max(by_day) + timedelta(days=1), prefix=LA.slug_prefix
        )
    }
    common = sorted(set(by_day) & poly_days & set(markets))
    print(f"Overlap : {len(common)} days, {min(common)} -> {max(common)}")

    observations = iem.fetch_observations(
        LA.station, min(common) - timedelta(days=1), max(common) + timedelta(days=2)
    )
    settled = daily_maxima_fahrenheit(observations, Strategy(tz=LA.tz))

    print("Fetching Kalshi candles..." if args.refresh or not CACHE.exists()
          else "Using cached Kalshi candles.")
    candles = load_candles({d: by_day[d] for d in common}, args.refresh)

    # Rebuild typed candles once.
    series_by_day: dict[date, dict[str, list[kalshi.KalshiCandle]]] = {}
    buckets_by_day: dict[date, dict[str, object]] = {}
    for day in common:
        series_by_day[day] = {}
        buckets_by_day[day] = {}
        for market in by_day[day]:
            rows = candles.get(market.ticker) or []
            series_by_day[day][market.ticker] = [
                kalshi.KalshiCandle(ts=r[0], bid=r[1], ask=r[2], mean=None,
                                    volume=r[3], open_interest=0.0)
                for r in rows
            ]
            buckets_by_day[day][market.ticker] = market.bucket

    # The venues settle on different numbers, so Kalshi's distribution has to be
    # translated before it can be scored against Polymarket's outcome. Fit that
    # translation on the first half of the days and apply it to the second, so
    # no evaluation day informs its own correction.
    cut = len(common) // 2
    pairs: list[tuple[int, int]] = []
    for day in common[:cut]:
        winners = [m for m in by_day[day] if m.settled_yes]
        truth = settled.get(day)
        if len(winners) != 1 or truth is None:
            continue
        b = winners[0].bucket
        centre = (b.low + b.high) // 2 if b.kind is BucketKind.RANGE else b.low
        pairs.append((centre, truth))

    offset = SourceOffset.fit(pairs)
    test_days = set(common[cut:])
    print(f"Offset  : fitted on {len(pairs)} days, mean {offset.mean:+.2f} F")
    print("          " + str({k: round(v, 3) for k, v in sorted(offset.weights.items())}))
    print(f"Scoring : {len(test_days)} held-out days")

    print("\n" + "=" * 78)
    print("KALSHI-AS-FAIR-VALUE vs POLYMARKET'S OWN MIDPOINT")
    print("=" * 78)
    print(f"  {'hour':>5}{'n':>5}{'poly':>10}{'kalshi':>10}{'uncorr':>10}"
          f"{'kalshi vs poly':>17}{'k spread':>10}")

    for hour in args.hours:
        rows = []
        for day in sorted(test_days):
            truth = settled.get(day)
            if truth is None:
                continue
            ts = int(local_instant(day, hour).timestamp())

            ksnap = kalshi.snapshot_from_series(
                series_by_day[day], buckets_by_day[day], ts
            )
            if ksnap is None:
                continue
            psnap = poly_snapshot(load_day(day, city=LA), ts,
                                  max_staleness=6 * 3600, require_all=False)
            if psnap is None:
                continue

            index = next(
                (i for i, b in enumerate(psnap.buckets) if b.holds(truth)), None
            )
            if index is None:
                continue
            try:
                mapped = to_poly_buckets(ksnap, psnap.buckets, offset=offset)
                raw = to_poly_buckets(ksnap, psnap.buckets)
            except ValueError:
                continue

            align = alignment(ksnap.buckets, psnap.buckets)
            rows.append((psnap.probabilities, mapped, index, align.aligned,
                         ksnap.mean_spread, raw))

        if not rows:
            print(f"  {hour:>5.0f}    0   (no clean instants)")
            continue

        outcomes = [r[2] for r in rows]
        p = summarise([r[0] for r in rows], outcomes)
        k = summarise([r[1] for r in rows], outcomes)
        u = summarise([r[5] for r in rows], outcomes)   # uncorrected Kalshi
        spread = sum(r[4] for r in rows) / len(rows)
        print(
            f"  {hour:>5.0f}{p.n:>5}{p.rps:>10.5f}{k.rps:>10.5f}{u.rps:>10.5f}"
            f"{(p.rps - k.rps) / p.rps:>16.1%}{spread * 100:>9.2f}c"
        )

        # The diagnostic that separates "the idea is wrong" from "my mapping is".
        for want, label in ((True, "aligned"), (False, "offset ")):
            sub = [r for r in rows if r[3] is want]
            if len(sub) < 10:
                continue
            so = [r[2] for r in sub]
            sp = summarise([r[0] for r in sub], so)
            sk = summarise([r[1] for r in sub], so)
            print(f"        {label} n={sp.n:<4} poly {sp.rps:.5f}  "
                  f"kalshi {sk.rps:.5f}  ({(sp.rps - sk.rps) / sp.rps:+.1%})")

    print("=" * 78)
    print("  'kalshi vs poly' > 0 means Kalshi is the better fair value.")
    print("  On aligned days the mapping is exact; on offset days it is assumed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
