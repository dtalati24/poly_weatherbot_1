# poly_weatherbot

A model for Polymarket's **"Highest temperature in London on \<date\>?"** markets.

The markets present ~11 mutually exclusive, ordered temperature buckets and
settle on the daily maximum at **London City Airport (EGLC)** as published by
**Weather Underground**, in whole degrees Celsius. The goal is a calibrated
probability distribution over those buckets, good enough to quote against.

Build plan and strategy rationale: [`docs/PLAN.md`](docs/PLAN.md).
Phase 0 results: [`docs/PHASE0_FINDINGS.md`](docs/PHASE0_FINDINGS.md).

---

## Why the settlement variable gets its own phase

The thing these markets settle on is **not** "the daily high temperature in
London." It is:

```
Y = max over the observations Weather Underground ingested for EGLC,
    grouped by London-local calendar day,
    of the whole-degree Celsius temperature
```

Three properties of that definition drive the entire design, and each one was
verified against real settled markets rather than assumed:

1. **The station is EGLC**, in the Royal Docks — not Heathrow. Anyone modelling
   a generic "London" grid point is modelling a different variable.
2. **The source is Weather Underground's ingested subset**, which occasionally
   drops individual METARs. When that happens the settled value can sit a full
   degree — a full market bucket — below what the raw METAR record implies.
3. **Whole degrees**: the value is already integer-rounded at the METAR encoder,
   and the maximum is taken over discrete half-hourly samples. `max(round(x))`
   is not `round(max(x))`.

`resolve()` reconstructs `Y` from raw observations, and is validated by
replaying every settled London market on record.

---

## Layout

```
src/weatherbot/
  config.py              Station, endpoints, paths. Single source of truth.
  observation.py         Shared observation record used by all sources.
  resolve.py             Reconstructs the settlement value. Candidate rule
                         interpretations are enumerated, not assumed.
  fees.py                Fee and rebate arithmetic. The published formula, not
                         the one assumed in PLAN.md.
  cities.py              Per-city station, timezone, slug prefix and unit.
  crossvenue.py          Kalshi's distribution -> Polymarket's buckets.
  intraday.py            Running maximum and remaining maximum, leakage-safe.
  market.py              Archived prices -> the market's own distribution.
  priceharvest.py        Price archive: one record per market day.
  sources/
    wunderground.py      The settlement source. Train and validate on this.
    iem.py               Raw METAR archive (decades of history, free).
    polymarket.py        Gamma API: settled markets, bucket parsing.
    clob.py              Order book API: 1-minute historical midpoints.
    kalshi.py            Kalshi market data: 1-minute bid/ask candles.
  analysis/
    cadence.py           Observation cadence and truncation analysis.

scripts/
  validate_resolve.py    Phase 0 gate: replay every settled market.
  compare_sources.py     Quantify Weather Underground vs raw METAR divergence.
  analyze_cadence.py     Reporting cadence, curfew and truncation checks.
  harvest_prices.py      Archive quoted prices (--all backfills everything).
  analyze_prices.py      Phase 4 gate: does the model beat the market?
  evaluate_model_d.py    Phase 5: the nowcast vs the market, hour by hour.
  evaluate_crossvenue.py Phase 6: is Kalshi a better fair value than Polymarket?
  blend_significance.py  Does a model+market blend survive out-of-sample?

tests/                   Unit tests for the correctness-critical paths.
docs/                    Plan and findings.
data/                    Cached API responses (gitignored, reproducible).
```

## Setup

Requires Python 3.11+.

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Phase 0 gate: does resolve() reproduce real settlements?
py scripts/validate_resolve.py

# The same replay against raw METARs instead, to see the source gap
py scripts/validate_resolve.py --source iem

# How often does Weather Underground disagree with the raw record?
py scripts/compare_sources.py

# Reporting cadence, curfew truncation risk, hour-of-maximum distribution
py scripts/analyze_cadence.py --source iem

py -m pytest
```

All network responses are cached under `data/`, so re-runs are fast and the
first run of any script is the slow one.

## Data sources

| Source | Role | Access |
|---|---|---|
| Weather Underground | **Settlement source.** Train and validate here | Public JSON endpoint behind the History page |
| IEM ASOS archive | Long raw-METAR history; divergence signal | Free, no key |
| Polymarket Gamma API | Settled markets, buckets, volumes, fee schedule | Free, no key |
| Polymarket CLOB API | **The benchmark.** 1-minute historical midpoints per bucket | Free, no key — use `startTs`/`endTs`, never `interval` |
| Open-Meteo ensembles | ECMWF IFS/AIFS, GEFS, ICON, MOGREPS-G — 8 models | Free, no key |
| Open-Meteo historical forecast | Lead-resolved deterministic backfill to 2022 | Free, no key |
| Met Office IMPROVER spot percentiles | Calibrated Tmax distribution, MOGREPS-UK blended, ~305 m from EGLC | Free, **no key** (AWS Open Data) |

## Status

**Phase 0 — settlement reconstruction: complete.** 503/504 settled markets
reproduced. See [`docs/PHASE0_FINDINGS.md`](docs/PHASE0_FINDINGS.md).

**Phase 1 — forecast pipeline: live.** 8 ensembles + Met Office IMPROVER spot
percentiles harvested 4x daily via GitHub Actions, plus a lead-resolved
deterministic backfill to 2022. See [`docs/PHASE1_DATA.md`](docs/PHASE1_DATA.md).

Two things worth knowing before using the data:

- Ensemble history **cannot** be backfilled (~4 days available regardless of
  what you request), so the harvester must keep running. Deterministic history
  *can*, at known lead times, which is what makes training possible today.
- The Met Office percentiles are maxima over a **12-hour window**, not the local
  calendar day. They match the true daily maximum on ~90% of days. They are a
  feature, not the target.

**Phase 2 — baselines: established.** The benchmark is **RPS 0.11760** on 207
held-out settled markets. See [`docs/PHASE2_BASELINE.md`](docs/PHASE2_BASELINE.md).

The surprise: plain temperature climatology scores **worse than uniform**.
Polymarket centres each bucket window on its own forecast — outcomes land in an
end bucket only 5.8% of the time — so a day-of-year model piles mass on the wrong
tail. The benchmark is instead *positional* climatology, which models where in
the window outcomes land and beats uniform by 12.9% using no weather data at all.

**Phase 3 — Model B: beats the benchmark.** A lead-indexed forecast MOS scores
**RPS 0.07261 at lead 1** and **0.08566 at lead 2** on the same held-out markets
— **+38.3%** and **+27.2%** over the benchmark. Markets are created ~2 days
ahead, so that is exactly the tradeable region. See
[`docs/PHASE3_MODEL_B.md`](docs/PHASE3_MODEL_B.md).

One operational finding that survives: **beyond four days the forecast is worse
than knowing nothing.** At lead 5 the structural baseline wins by 14%, so quote
positional climatology out there.

> Phase 3 also claimed the raw forecast runs ~0.5 °C cold *at every lead and
> always*, and that the market had inherited that bias — which would have made
> correcting it the edge. **Both claims are false.** Phase 4 disproved them
> against real prices; see below.

**Phase 4 — the reality check: the model does not beat the market.** Scored
against the market's own midpoints, at the same instant, on the same buckets,
over 382 settled markets. See [`docs/PHASE4_PRICES.md`](docs/PHASE4_PRICES.md).

| Lead | n | market | best model | model vs market |
|---|---|---|---|---|
| 1 day | 382 | **0.06731** | 0.08137 | **−20.9%** |
| 2 days | 300 | **0.07971** | 0.09414 | **−18.1%** |

Every earlier score compared the model to a statistical baseline, which answers
"does it know something about the weather". It does. It does not know anything
the price does not: the market is ~20% sharper at both tradeable leads, a blend
of the two adds nothing that survives out-of-sample, and the market's implied
centre is within 0.1 °C of the observed value while the raw forecast is not.

Three things this corrects:

- **The forecast bias is non-stationary**, not fixed. Mean error at lead 1 in
  July was +0.97 °C (2024), +0.17 (2025), −0.49 (2026) — a 1.5 °C swing visible
  in two independent models. Fitting it on a trailing window does not close the
  gap either.
- **Price history does not expire.** An earlier commit here claimed a ~31-day
  retention cliff. That was an artifact of querying `interval=max`; with an
  explicit `startTs`/`endTs` window the full 1-minute history is available. The
  archive now holds **536 market days at 1-minute resolution**, back to the
  first London market.
- **The fee formula was wrong.** `shares × rate × [p(1−p)]^exponent`, not
  `rate × min(p, 1−p) × shares`. The no-trade band protecting a resting quote is
  1.25¢ at the midpoint, half what the plan assumed.

**Phase 5 — Model D: beats every previous model, still loses to the market.**
The intraday nowcast, scored hour by hour through the settlement day. See
[`docs/PHASE5_MODEL_D.md`](docs/PHASE5_MODEL_D.md).

| local hour | 09 | 13 | 17 | 19 | 21 |
|---|---|---|---|---|---|
| market | **0.05522** | **0.04404** | **0.00962** | **0.00282** | 0.00137 |
| best nowcast | 0.08077 | 0.06491 | 0.01461 | 0.00344 | **0.00003** |
| vs market | −46.3% | −47.4% | −51.8% | −21.8% | **+97.8%** |

The structure works: `Y = max(M, X)` is an identity, the running maximum fixes
the answer on 93% of days by 17:00, and the nowcast beats Model B at *every*
hour. But the market has the same observations, and is already doing the same
subtraction — the probability it leaves on buckets the running maximum has ruled
out is a median of **0.2¢**. The 21:00 win is real in RPS and worth nothing in
cents.

**Phase 6 — Kalshi as fair value: the venues settle on different numbers.**
Kalshi runs the same LA contract on the same station (KLAX) with more volume, so
its prices should have been a fair value to quote against Polymarket with — no
forecasting required. See [`docs/PHASE6_CROSSVENUE.md`](docs/PHASE6_CROSSVENUE.md).

| LA hour | n | Polymarket | Kalshi | Kalshi vs Poly |
|---|---|---|---|---|
| −6 | 35 | **0.03654** | 0.05168 | −41.5% |
| 0 | 34 | **0.03543** | 0.05005 | −41.3% |
| +9 | 29 | **0.03613** | 0.04274 | −18.3% |

Kalshi settles on the NWS Climatological Report (ASOS 5-minute data); Polymarket
settles on Weather Underground's METAR record, and KLAX reports hourly. So CLI
catches peaks between our observations and is **never lower**: exact agreement
60%, +1 °F 33%, +2 °F 7%. Late in the day Kalshi becomes confident about a value
one bucket above the one Polymarket settles on. Correcting for the offset helps
at every hour and is nowhere near enough — and Kalshi loses *most* on the days
where the bucket mapping is exact, so the premise fails rather than the method.

Two premises also died: Kalshi is **not** tighter (median spread 1¢, the same
tick floor Polymarket sits on), and more volume in the wrong variable does not
help. Left behind: a validated LA settlement reconstruction (**72/72, 100%**)
and genuine multi-city support.

**Next:** the market prices this contract well at every horizon measured so far
— two days out, one day out, and hour by hour on the day. The open question is
no longer accuracy but **latency**: every comparison is at a fixed instant, so
if the market reacts to a new observation more slowly than we do, the edge lives
in the minutes after each METAR and no hourly comparison can see it. After that:
season-conditioned lock-in (winter evenings are the known weak spot), Model C
(ensemble spread), and multi-model blending. The quoter waits until something
beats the mid.
