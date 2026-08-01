# Phase 2 — Baselines

**Status: benchmark established.**

Walk-forward split: observations 2008-01-01 → 2026-01-01 and markets
2025-01-01 → 2026-01-01 for fitting; **207 settled markets in 2026-01-01 →
2026-08-01** for scoring. Nothing fitted on the evaluation window.

| Baseline | RPS | log loss | Brier | RPS skill vs uniform |
|---|---|---|---|---|
| uniform | 0.13496 | 2.2771 | 0.8956 | — |
| climatology (Model A) | 0.19077 | 2.6627 | 1.0524 | **−41.4%** |
| **positional (Model A′)** | **0.11760** | **2.0196** | **0.8440** | **+12.9%** |

> **The number to beat is RPS 0.11760.** A forecast model that does not improve
> on it has learned nothing.

---

## 1. The headline finding: climatology is the wrong benchmark

Plain temperature climatology scores **worse than uniform**. That is not a bug —
the model is sound, and its quantiles track the realised historical
distribution closely:

| Target | Model p5 / p50 / p95 | Historical p5 / p50 / p95 |
|---|---|---|
| 2025-01-15 | 4 / 10 / 15 | 2 / 8 / 13 |
| 2025-04-15 | 10 / 15 / 23 | 10 / 14 / 23 |
| 2025-07-15 | 20 / 24 / 31 | 18 / 23 / 30 |
| 2025-10-15 | 13 / 17 / 22 | 12 / 16 / 21 |

The problem is structural: **Polymarket centres the bucket window on its own
forecast.** Measured across 535 settled markets:

- outcome landed in an **end bucket only 5.8%** of the time
- mean relative position **0.551** (0.5 is dead centre), sd 0.203
- for Celsius markets, observed minus window centre: **+0.41 °C**, sd 1.99 °C
- **63.2%** within ±1 °C of centre, **83.3%** within ±2 °C

Worked example — 2026-07-30, buckets spanning 23–33 °C, actual 28 °C:

```
bucket : 23C or b   24C   25C   26C   27C   28C   29C   30C   31C   32C  33C or h
clim p :    0.401 0.130 0.109 0.092 0.072 0.054 0.042 0.030 0.019 0.014     0.036
                                            ^ truth got 0.054
```

Climatology knows only the day of year, so it dumps 40% of its mass on the cold
tail. RPS then punishes it for the bucket *distance*, which is exactly what RPS
is for. Uniform, spreading 1/11 evenly, is closer on average.

**Implication for later models:** the bucket window is free forecast
information. Its centre is a usable feature, and it carries a measurable
**+0.41 °C cold bias** relative to outcomes — worth revisiting once a real
forecast model exists.

## 2. Model A′ — positional climatology

Instead of "what temperature will it be", ask "where in the window does the
outcome land". Relative position `u = index / (n_buckets − 1)` is modelled with
a Gaussian kernel so markets with 7, 9 and 11 buckets all inform one estimate
rather than being fitted on thin per-size slices.

It uses **no weather information at all** — it is a pure structural baseline,
and it still beats uniform by 12.9%.

Calibration on the evaluation window is good:

| forecast range | n | mean forecast | observed | gap |
|---|---|---|---|---|
| 0.0–0.1 | 1221 | 0.0461 | 0.0418 | +0.0043 |
| 0.1–0.2 | 684 | 0.1642 | 0.1740 | −0.0097 |
| 0.2–0.3 | 102 | 0.2432 | 0.2745 | −0.0313 |
| 0.3–0.4 | 42 | 0.3227 | 0.2143 | +0.1084 |

Only the sparse top bin is materially off.

## 3. Station homogeneity — two breaks, and a correction

> **Correction.** An earlier version of this document said the 2005–2007 record
> was *hourly* and became half-hourly in 2008. That was wrong. The cadence has
> been `:20/:50` for the entire archive — **0.000%** of 354,009 observations fall
> on any other minute, in every era. The magnitude I quoted was roughly right;
> the mechanism was not.

**Break 1 — 2007, coverage hours (verified here).** What changed is how much of
the day was covered, not how often it sampled:

| Year | obs/day | UTC hours covered |
|---|---|---|
| 2005–2006 | 26.4–26.7 | ~04:00–21:00 |
| 2007 | 32.6 | transitional |
| 2008+ | 45.9–48.0 | full 24 h |

This still matters, because the settlement variable is a **maximum over
samples**. Restricting modern data to the old window:

> Restricting to 05:00–20:00 UTC lowers the observed daily maximum by
> **0.081 °C** on average, affecting 5.5% of days — and the true maximum falls
> outside that window on **10.15%** of days, overwhelmingly the
> just-after-local-midnight case identified in Phase 0.

**Break 2 — October 2013, an undocumented instrument change (corroborated, not
independently reproduced).** External analysis differencing EGLC against
Heathrow, Stansted and Gatwick places a step of ≈ **+0.47 °C** around October
2013: dry-bulb only (dewpoint +0.08), uniform across seasons (+0.43 to +0.54)
and across the temperature range. That is the signature of a sensor or screen
replacement, not a relocation — a siting change would be summer-amplified. It
appears in no AIP, Met Office or airport publication.

Corroborating evidence computed here:

| Training start | Fitted trend |
|---|---|
| 2008 | **+1.185 °C/decade** |
| 2014 | **+0.415 °C/decade** |

A step part-way through the window inflates a trend fitted across it, and
+0.415 sits far closer to the regional airport composite. **The +1.19 figure
should not be reported as a warming estimate.**

`HOMOGENEOUS_START` is therefore **2014-01-01**. `COVERAGE_BREAK_2007` is
exposed for sensitivity testing. Note the honest limit: the neighbour-station
differencing behind the 2013 date has **not** been reproduced in this
repository.

**Effect on the benchmark: none.** Positional climatology is unchanged at RPS
0.11760 because it uses no temperature data. Climatology moves 0.19077 →
0.19626, which is immaterial for a model already well behind uniform.

Also flagged and not yet acted on: **2011 contains 25 days with no maximum at
all** — the only year with missing days. Screen on reports-per-day, not just
day presence.

## 4. Why RPS, and why normalised

These buckets are **ordered**. Predicting 27 °C when the answer is 28 °C is a
near miss; predicting 22 °C is a disaster. Log loss cannot tell them apart — it
only reads the probability on the one true bucket. There is a test asserting
exactly this contrast (`test_log_loss_cannot_distinguish_near_from_far`).

RPS is normalised by `(K−1)` because bucket counts vary by era (7, 9 and 11 all
appear). Raw RPS grows with K, so comparing an unnormalised 7-bucket score
against an 11-bucket one would be meaningless.

## 5. Reproducing

```bash
py scripts/evaluate_baselines.py    # the table above
py -m pytest                        # 118 tests
```

## 6. Engineering note

The IEM observation cache was re-keyed from arbitrary chunk boundaries to
**calendar years**. The old scheme meant a query starting one day earlier missed
every cached file and re-downloaded 22 years, which is how this hit an IEM rate
limit and then a connection reset. Year keys make the cache reusable across any
range; the current year refetches once its cache is 6 hours old, completed years
are treated as immutable.

---

## Next: Model B

Feed actual forecasts in. The deterministic backfill (ECMWF from 2024-03, GFS
from 2022-01, at leads 1–5) is already sufficient. Model B maps ensemble/
deterministic forecasts to a temperature distribution; it must beat **RPS
0.11760** on the same 207 markets to justify itself.

Note that Model B inherits the same structural problem climatology hit: it must
be evaluated *through the market's bucket window*, not on raw temperature error.
A model with excellent °C accuracy can still lose to positional climatology if
its errors straddle bucket edges.
