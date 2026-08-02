# Phase 6 — Kalshi as fair value for Polymarket

**Status: does not work, and the reason is specific and measured. The two venues
do not settle on the same number.**

The idea was the best one yet, because it needed no forecasting skill: Kalshi
runs the same daily "highest temperature in Los Angeles" market, on the same
physical station, with more volume. If its prices were sharper, they would be a
fair value to quote against Polymarket with — and Phases 4 and 5 had already
shown that *our* forecasts are not.

LA was chosen deliberately over NYC. Both venues settle LA on **KLAX**, so only
the reporting pipeline differs. In NYC, Kalshi settles Central Park and
Polymarket settles LaGuardia — different thermometers miles apart, which would
stack a second unknown on the first.

Scored on 35 held-out days, Kalshi's distribution expressed in Polymarket's
buckets, against Polymarket's own midpoint, at the same instant:

| LA hour | n | Polymarket | Kalshi (corrected) | Kalshi (raw) | Kalshi vs Poly |
|---|---|---|---|---|---|
| −12 | 35 | **0.03982** | 0.05479 | 0.06003 | −37.6% |
| −6 | 35 | **0.03654** | 0.04970 | 0.05629 | −36.0% |
| 0 | 34 | **0.03543** | 0.04839 | 0.05415 | −36.6% |
| +6 | 30 | **0.03668** | 0.04102 | 0.05149 | −11.8% |
| +9 | 29 | **0.03613** | 0.03903 | 0.05059 | −8.0% |

---

## 1. The finding: same station, different variable

Kalshi settles on the **NWS Climatological Report**, which is computed from ASOS
**5-minute** data. Polymarket settles on **Weather Underground's** METAR record,
and KLAX transmits METARs **hourly at :53**. So CLI sees peaks that fall between
our observations, and is never lower:

Measured over **1288 days** (2023–2026) of actual CLI and WU values:

| CLI − WU | share |
|---|---|
| 0 (exact) | **35.3%** |
| +1 °F | **49.8%** |
| +2 °F | 12.1% |
| +3 °F or more | 2.6% |
| negative | 0.2% (both explained by the day boundary) |

**Mean +0.833 °F, sd 0.810**, and stable year over year (+0.840 / +0.852 /
+0.823 / +0.804 for 2023–26). CLI equals the max of the 6-hourly METAR remark
max-groups on **98.8%** of days, confirming this is a pure sampling-density gap:
CLI is effectively the 5-minute ASOS max, WU is the max of hourly observations.

At Polymarket's 2 °F bucket resolution that becomes **same bucket 61.3%, one
bucket lower 37.0%, two or more lower 1.6%**.

> **Correction.** An earlier version of this document reported "exact agreement
> 60%, +1 °F 33%, +2 °F 7%", measured on the 70-day Kalshi window using *bucket
> containment*. Those numbers are bucket-level, not degree-level, and were
> mislabelled as °F differences. The bucket-level figure was right (61.3% here);
> the degree-level agreement is **35.3%**, not 60%. The mean gap is +0.83 °F,
> not +0.5 °F — a fit on bucket centres is biased toward zero precisely because
> bucket containment hides sub-bucket differences.

Both premises were verified from the venues' own settled data rather than their
documentation alone: Kalshi's settled bucket contains CLI on **70/70** days (and
WU on only 60%); Polymarket's settled bucket contains WU on **126/126** days
(and CLI on only 58.7%, losing all 52 discriminating days).

**It is also fatal to the idea.** Late in the day Kalshi becomes very confident
about a value one bucket above the one Polymarket will settle on. The signature
was unmistakable once looked for: before correction, Kalshi's RPS was **flat at
~0.05 across the entire day** while Polymarket's collapsed from 0.036 to
0.00002. A real market must sharpen as the answer becomes known; a market
sharpening on the *wrong variable* does not.

Correcting for it — convolving Kalshi's distribution with the empirically fitted
offset distribution, fitted on the first half of the days and applied to the
second — **helps at every hour** (the "corrected" column beats "raw"
consistently). It is not nearly enough. The offset distribution is diffuse
(`−2: 0.10, −1: 0.43, 0: 0.32, +1: 0.15`), so the convolution that removes the
bias also smears away exactly the sharpness that made Kalshi worth consulting.

## 2. It is not the bucket mapping

The obvious suspect was the change of basis. Both venues re-centre their ladder
daily on their own forecast, and they disagree about where the edges sit:

```
aligned (same 2F lattice)   35 / 69 days
offset by 1F                34 / 69 days
```

On an aligned day Kalshi's `80-81` *is* Polymarket's `80-81` and the mapping is
exact. On an offset day Kalshi's `65-66` straddles Polymarket's `64-65` and
`66-67`, and its mass must be split by assumption.

So the results are reported split by alignment, and the split is decisive:

| LA hour | aligned (exact mapping) | offset (assumed split) |
|---|---|---|
| −6 | −51.2% | −28.9% |
| 0 | −73.7% | −20.1% |
| +6 | −38.9% | **+3.0%** |
| +9 | −42.5% | **+14.1%** |

Kalshi loses **most** on the days where the mapping is exact and no assumption
is involved. The re-binning is not the problem; the premise is.

The reversal on offset days late in the session is not noise, and it has a
mechanism. The gap between CLI and WU is one degree half the time, so whether it
crosses a Polymarket bucket edge depends on the **parity** of CLI:

| | P(same Polymarket bucket) |
|---|---|
| CLI odd (upper half of a bucket) | **84.3%** |
| CLI even (on a bucket's lower edge) | **38.8%** |

Kalshi's own 2 °F buckets normally hide that parity. But when Kalshi's ladder is
offset from Polymarket's, Kalshi's edges fall *inside* Polymarket's buckets and
the offset itself leaks sub-bucket position. That is a real effect rather than a
fitting artifact — but n=16, it was found after the fact, and it appears at
exactly the hours where the sample is thinnest. Recorded, not acted on.

## 3. Two premises that did not survive contact

**"Kalshi is tighter."** It is not. Measured over 98,574 two-sided minutes:
median spread **1¢**, 74.6% of quotes at exactly 1¢, 90% within 2¢. Kalshi's
tick is 1¢ (`linear_cent`, 0 of 180,546 quotes off the grid), so 1¢ *is* the
structural floor and Kalshi sits on it three-quarters of the time. Against
Polymarket's measured 1–2¢ touch, that is **parity, not an advantage**.

**"More volume means a better price."** Kalshi is genuinely liquid — median
548k contracts per event-day across the six buckets, rising from ~250k in May to
~800k–1M by late July. It still predicts Polymarket's settlement worse than
Polymarket does, because liquidity in the wrong variable does not help.

Kalshi's real advantages turned out to be different ones, and none of them is
the edge we wanted: round-the-clock quoting (including 02:00–06:00 LA when
Polymarket is thin), a six-bucket simultaneous cross-section giving a full
implied distribution, and exact reconcilable volume data.

## 4. A bug worth recording

The first run produced Kalshi RPS of 0.053 against Polymarket's 0.00002 at
18:00 — a nonsensical −305,608%. The cause: **Kalshi reports an empty book as
`bid = 0, ask = 100`, not as null.** Taken at face value that is a 50¢ midpoint,
and after about 17:00 LA *every* bucket goes one-sided as the day resolves. So
the pipeline was manufacturing a confident uniform distribution precisely when
the answer was already known.

The flat-across-the-day RPS is what exposed it. That shape — a score that does
not improve as information arrives — is a better bug detector than any absolute
number, and is worth watching for directly.

Usable quoting window, measured: Kalshi lists at **D−1 07:00 LA** and its books
collapse to one-sided after about **16:00 LA** on the settlement day. Outside
that there is no fair value to read at all.

## 5. Two risks this surfaced that have nothing to do with Kalshi

**Weather Underground breaks on every spring-forward day at KLAX too.** The same
defect found for London. WU serves only the 00:53 and 01:53 observations and
nothing else:

| date | WU obs | WU max | CLI | METAR max | error |
|---|---|---|---|---|---|
| 2023-03-12 | 2 | 57 | 62 | 61 | −4 |
| 2024-03-10 | 2 | 52 | 64 | 64 | **−12** |
| 2025-03-09 | 2 | 51 | 71 | 70 | **−19** |
| 2026-03-08 | 2 | 69 | 88 | 88 | **−19** |

**And Polymarket settles on WU including its gaps** — verified on two smaller
cases (2026-04-05: WU 15 observations, WU max 75, CLI 78, settled `74-75°F`;
2026-04-29: 17 observations, WU 68, CLI 70, settled `68-69°F`). The LA series
began 2026-03-24, so no market has yet spanned a spring-forward day and how
Polymarket would resolve one is **unknown**. **2027-03-14 is a live, dated tail
risk** on a market whose resolution source would be showing a ~52 °F high on a
64 °F day. A further 15 partial-gap days cluster in April–May 2026, suggesting
WU ingest reliability has recently degraded.

**Kalshi's settlement value is knowable hours before it settles.** Since CLI is
the max of the 6-hourly METAR remark max-groups on 98.8% of days, and the 00Z
METAR (~17:00 PDT) carries the group covering 11:00–16:00 PDT, the free live
METAR stream essentially reveals CLI before the Kalshi market resolves. That is
a *Kalshi-side* edge and outside the scope chosen here (signal-only, quote on
Polymarket), but it is the most interesting thing this phase turned up and it
does not help predict Polymarket — whose settlement is the hourly-observation
max, which we already observe directly.

## 6. Operational notes

- **Kalshi history is a 70-day rolling purge**, exchange-wide rather than
  series-specific: `KXHIGHLAX`, `KXHIGHNY`, `KXHIGHCHI` and `KXHIGHMIA` all
  return exactly 432 markets with the identical earliest event. Event shells
  persist back to 2025-04-01 but the markets underneath are gone. **A day of
  backtest is lost for every day not archived.** Not acted on, per the decision
  to answer the question on existing data rather than build more pipeline.
- Candles carry `volume_fp`/`open_interest_fp`; the plain `volume`/`yes_bid`
  keys exist but are `None`, so reading them yields nulls rather than an error.
- 45% of minutes have no candle at all; forward-fill or cross-sections misalign.
- Settled markets are **not** degraded — per-candle volume sums to the market
  total at ratio 1.0000 for all 60 finalized tickers. The Polymarket
  `interval=max` failure mode does not reproduce here.

## 7. What Phase 6 did leave behind

A correct LA settlement reconstruction, which is reusable and was validated the
same way London's was:

> **72/72 settled LA markets reproduced exactly (100%).**

That required finding LA's own version of the Phase 0 trap. KLAX METARs carry
tenths of a degree Celsius in the remark T-group, and the market settles in
whole Fahrenheit. Rounding to whole Celsius first and then converting — which is
what `Bucket.contains` does, correctly, for London — double-rounds:

| reduction | markets reproduced |
|---|---|
| max in °C → one °F conversion | **72/72 (100%)** |
| round each observation to °F → max | 72/72 (100%) |
| round to whole °C → convert to °F | **52/72 (72%)** |

`daily_maxima_fahrenheit` and `Bucket.holds` exist to make the right path the
easy one. The project is now genuinely multi-city (`weatherbot.cities`), with
per-city timezone, slug prefix, station and unit.

## 8. Reproducing

```bash
py scripts/harvest_prices.py --city los-angeles --start 2026-05-24 --end 2026-08-02
py scripts/evaluate_crossvenue.py
py -m pytest                                        # 294 tests
```

---

## Verdict

Four independent attempts have now failed to find an edge: forecast MOS at leads
1–2, a rolling-window variant, an intraday nowcast, and cross-venue transfer
from a more liquid exchange.

The cross-venue attempt failed for a more interesting reason than the others. It
was not out-predicted — it was defeated by a **settlement-definition mismatch of
half a degree Fahrenheit**, arising purely from one venue reading 5-minute data
and the other reading hourly data at the same physical thermometer. On 40% of
days that half-degree moves the answer, and no amount of liquidity on the other
venue fixes it.

That is worth carrying forward as a general lesson rather than an LA one: for
cross-venue work on any contract, the settlement *definitions* have to match, not
merely the underlying. Same station is not the same variable.
