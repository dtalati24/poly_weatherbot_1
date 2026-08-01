# London Max Temperature Bot — Build Plan

**Target market:** Polymarket Global, "Highest temperature in London on <date>?"
**Scope of this doc:** the prediction/ML layer. Quoting engine is a separate later doc.

---

## 0. The thing we are actually predicting

Get this exactly right before writing any model code. Everything downstream is worthless if this is wrong.

**Settlement variable `Y`:**
> The highest temperature recorded for all times on the specified day at the **London City Airport station (EGLC)**, as shown on **Weather Underground**, in **whole degrees Celsius**.

Revisions count until the first datapoint of the following date is published; after that the value is frozen.

**Market structure:** 11 mutually exclusive, exhaustive outcomes. Bucket edges shift with season:
- Jul 30 2026: `≤23, 24, 25, 26, 27, 28, 29, 30, 31, 32, ≥33`
- Aug 2 2026: `≤22, 23, 24, 25, 26, 27, 28, 29, 30, 31, ≥32`

So the model's output is a **probability vector over 11 ordered categories**, and the bucket edges are an input, not a constant.

### Why this is not "forecast London Tmax"

`Y` is not the true continuous daily maximum temperature. It is:

```
Y = max over observation times t in day D of  round_to_int_C( T_EGLC(t) )
```

Two competing distortions:
- **Sampling truncation** (biases `Y` *down*): METARs are discrete (typically ~half-hourly). The true peak between observations is missed.
- **Round-then-max** (biases `Y` *up*): each observation is rounded to integer °C *before* the max is taken. `max(round(x_i)) ≥ round(max(x_i))` in general — a day whose true peak is 27.4°C can settle at 28 if a single obs happened to land at 27.5+.

Net bias is a function of diurnal curve flatness, obs cadence, and cloudiness. **It is empirically measurable from history and it is a persistent, structural edge.** Competitors who take an ensemble Tmax and round it are systematically mispricing the tails of the bucket distribution.

### Phase 0 verification checklist (do this FIRST, ~2 days)

> **STATUS: COMPLETE — gate passed. Full write-up in [PHASE0_FINDINGS.md](PHASE0_FINDINGS.md).**
>
> - **0.1 answered.** Day boundary is **local** (Europe/London), confirmed directly
>   from WU's own block structure. Double-rounding is a non-issue: METARs are
>   already integer °C and survive a C→F→C round trip unchanged.
> - **0.2 answered.** Half-hourly at :20/:50, 48 obs/day, no systematic gaps.
> - **0.3 answered — hypothesis is FALSE.** The Sat 12:30→Sun 12:30 curfew is real
>   (UK AIP) but observing is automated and continues through it. Truncation risk
>   measured at **0/579 days**. Closed; spend no more time here.
> - **0.4 partially answered.** No rounding operator is needed — the encoder
>   already emits whole degrees. The residual discretization question is now about
>   *sampling* (max over 48 half-hourly points), not rounding.
> - **0.5 PASSED.** 503/504 pinned settlements reproduced over 535 markets.
> - **0.6 ANSWERED (Phase 4), and the assumption was wrong.** The fee is
>   `shares × rate × [p(1−p)]^exponent`, not `rate × min(p, 1−p) × shares`.
>   The no-trade band at the midpoint is 1.25¢, half what was assumed.
>
> **Unanticipated finding:** the settlement source is *mutable*. Polymarket
> settles against Weather Underground as it stood at resolution time, and WU's
> archive changes afterwards. Neither WU nor raw METAR is 100%; they fail on
> different days. See §3 of the findings.

| # | Question | How to answer | Why it matters |
|---|---|---|---|
| 0.1 | Does WU's displayed max equal the max of raw IEM METAR `tmpc` for EGLC? | Pull both for 60 sample days, diff them | If WU stores °F and converts back, there's **double rounding** — a different operator entirely |
| 0.2 | What is EGLC's actual obs cadence, and does it have gaps? | Histogram obs-per-day from IEM archive, by day-of-week and hour | Sampling truncation magnitude |
| 0.3 | **Does EGLC stop reporting during airport closure hours?** EGLC has a noise curfew (restricted Sat afternoon → Sun morning). | Count obs by hour × day-of-week over 5 years | **If Saturday obs stop before the typical 15:00–16:00 local peak, Saturday markets settle systematically low.** That would be an enormous edge on 1/7 of all markets. Unverified hypothesis — cheap to check, high payoff |
| 0.4 | Empirical distribution of `Y − true_continuous_Tmax` | Compare METAR-derived `Y` vs high-frequency/hourly Met Office obs or ERA5-corrected | Gives the discretization operator used in Model C2 |
| 0.5 | Does our `resolve()` reproduce ≥30 already-resolved Polymarket London markets? | Backtest against Gamma API resolved markets | **Gate: do not proceed until 100% match** |
| 0.6 | Exact fee formula | ~~Confirm `fee = 0.05 × min(p, 1−p) × shares`~~ | **ANSWERED in Phase 4, and the assumption was wrong.** Real formula: `shares × rate × [p(1−p)]^exponent`. Makers pay zero. `maker_base_fee`/`taker_base_fee` are inert (1000 everywhere); read gamma's `feeSchedule` |

**Deliverable:** `resolve(date) -> int`, validated against every settled market on
record. Delivered: 503/504 (99.80%) using raw METAR, with the single residual
mismatch documented as a Weather Underground ingest gap in
`src/weatherbot/anomalies.py`. 100% is not attainable — the settlement snapshot
is not recoverable after the fact — so the gate instead requires that *every*
mismatch be an explained feed anomaly, and fails on any new unexplained one.

---

## 1. Data sources

All coordinates target EGLC: **51.505°N, 0.055°E**, elev ~6 m.

### 1.1 Ground truth / observations
| Source | What | Access | Notes |
|---|---|---|---|
| **IEM ASOS archive** (mesonet.agron.iastate.edu) | Raw global METAR archive incl. EGLC, decades of history, `tmpc`, dewpoint, wind, cloud, raw METAR text | Free, no key, `/cgi-bin/request/asos.py` | **Primary ground-truth source.** Confirmed to carry international stations |
| **Weather Underground** history tab | The literal resolution source | Scrape | Only needed to validate 0.1; don't depend on it operationally |
| Neighbour stations: EGLL (Heathrow), EGKK (Gatwick), EGSS (Stansted), EGWU (Northolt), Kew/St James's Park | Spatial context, gradient features, gap backfill | IEM | Heathrow is the "London" most models/traders implicitly use — the EGLC−EGLL spread is itself a feature |
| **ERA5 reanalysis** (Copernicus CDS) | 20+ yr hourly gridded truth | Free w/ account | For learning long-run station-vs-gridcell bias |

### 1.2 Forecast models (ensembles — we need distributions, not point forecasts)
| Source | Models | Access | Why |
|---|---|---|---|
| **Open-Meteo Ensemble API** | ECMWF IFS ENS (51 mbrs), **ECMWF AIFS ENS** (50 mbrs, operational since 12 May 2026), GFS/GEFS (31), ICON-EU/ICON EPS, GEM GEPS | Free, no key, CC-BY, 10k calls/day non-commercial | Cheapest path to multi-model ensembles. Native full-res IFS (O1280) and AIFS (N320) available for Europe, 1-hourly |
| **Met Office DataHub** | **MOGREPS-UK — 2.2 km UK ensemble**, site-specific probabilistic forecasts | Free tier: 1 GB/mo; site-specific 360 calls/day | **Key differentiator.** 2.2 km actually resolves the Thames estuary and urban London. Not in Open-Meteo |
| **MOGREPS-UK on AWS Open Data** | Same, gridded | Free, **30-day rolling archive only** | Rolling window ⇒ **start harvesting to your own disk on day 1** or the history is gone forever |
| **ECMWF open data** (data.ecmwf.int) | IFS + AIFS, HRES & ENS, GRIB2 | Free, 0.25° now; 9 km subset expected later in 2026 (~2h latency) | Direct, no intermediary, lowest latency |
| **Open-Meteo Historical Forecast API** | Archived past forecasts | Free | For MOS training. **Caveat: IFS HRES archived from Mar 2024; most other models only from 2 Apr 2026** — archive is thin |

### 1.3 The training-data problem (read this)

Ensemble reforecast archives are **thin**. Most Open-Meteo model archives only start Apr 2026. Three mitigations, do all of them:

1. **Start harvesting live forecasts to disk today.** Every day of delay is a permanently lost training row. This is the single most time-sensitive action in the plan.
2. **ECMWF ENS reforecasts (hindcasts)** — twice-weekly, ~20 years, same model version. The proper way to train MOS with limited real-time history.
3. **Perfect-prognosis approach**: use ERA5 (20+ yrs) to learn the *station-vs-gridcell* mapping `g: (gridded state) → Y_EGLC`. Train on reanalysis where you have decades, then apply `g` to live forecast fields. Decouples "learn the local physics" (data-rich) from "learn the forecast bias" (data-poor).

---

## 2. Models — build in this order

Each stage must beat the previous on out-of-sample RPS before you move on.

### Model A — Climatological baseline
`P(bucket | day-of-year)` from 20 yr EGLC history. Harmonic (Fourier) regression on day-of-year for location and scale, kernel-smoothed residual distribution, plus a linear warming trend term.

- This is the **benchmark every other model must beat**, and the fallback when feeds die.
- Genuinely tradeable on its own for far-dated markets where NWP has no skill.

### Model B — Raw ensemble → bucket distribution
Take ensemble member Tmax at the EGLC gridpoint, build empirical CDF (optionally KDE-smoothed), integrate over bucket edges.

- Naive, but this is roughly what a competent competitor has. It's the bar to clear.

### Model C — Station MOS / bias correction ← **the core edge**

Learn the mapping from forecast state to the *actual settlement variable*. Two architectures; build C2 as primary.

**C1 — Direct multiclass GBM.** LightGBM, softmax over absolute integer °C bins (e.g. −5…40 °C), then map to that market's 11 buckets. Optionally an ordinal/cumulative-link formulation to enforce ordering and unimodality.

**C2 — Distributional regression + explicit discretization operator (recommended).**
```
step 1:  NWP features ──► distribution of latent continuous Tmax at EGLC
                          (NGBoost / DRN / GB quantile regression, skew-normal or
                           truncated-normal parameterization)
step 2:  latent continuous ──► P(Y = k)  via the discretization operator
                               measured empirically in Phase 0.4
step 3:  P(Y = k) ──► 11 market buckets
```
**Why C2 is architecturally right:** it cleanly separates *meteorology* (step 1) from *settlement mechanics* (step 2). Step 2 is the part competitors don't model at all. It also means you can improve either half independently, and the discretization operator can be conditioned on obs cadence — which is how the Saturday-curfew effect (0.3) enters the model automatically if it's real.

**Features:**
- Ensemble mean / spread / quantiles of Tmax, T2m diurnal curve, at multiple lead times
- **Wind direction & speed** (easterly ⇒ estuary/sea-breeze capping hypothesis; calm ⇒ urban heat island boost)
- Cloud cover fraction by period, incoming shortwave radiation
- Dewpoint, boundary-layer height, 850 hPa temp, pressure gradient
- Day-of-year, lead time, **inter-model disagreement** (IFS vs AIFS vs GFS vs MOGREPS spread — disagreement is a strong uncertainty signal)
- EGLC−EGLL historical bias conditioned on wind direction
- Yesterday's EGLC residual (persistence of local bias)

### Model D — Intraday nowcast ← **highest ROI, build right after C**

Once the day is underway the target is partially observed:
```
Y = max( running_max_so_far , max of remaining observations )
```
Model only the *remaining-day peak*, conditioned on: current temp, running max, time of day, hours of daylight left, today's ensemble, cloud/satellite trend, wind direction, and the climatological diurnal curve shape for that day-of-year.

This is a far easier problem than a day-ahead forecast and the distribution often collapses to 1–2 buckets by ~15:00 local. **This is where the largest edge over other participants lives**, because anyone quoting off a morning forecast is stale. Requires a real-time METAR ingestion loop (poll IEM / aviationweather / CheckWX every few minutes).

### Model E — Stacker + calibration
- Lead-time-dependent weighted combination of A–D (weights shift from A → C → D as the event approaches).
- **Calibration layer:** isotonic or Dirichlet calibration on the 11-vector. Non-negotiable — you are pricing probabilities, and being 3% miscalibrated at 0.05 is the difference between edge and ruin.
- Enforce `Σ p_k = 1` and reasonable unimodality.

---

## 3. Evaluation

**Primary metric: Ranked Probability Score (RPS)** — the correct proper scoring rule for *ordered* categorical outcomes. Being wrong by one bucket should hurt less than being wrong by five; log loss doesn't know that.

Also track:
- Multiclass log loss vs **Model A climatology** (the honest benchmark — "beats uniform" is meaningless)
- CRPS on the latent continuous Tmax
- Reliability diagrams **per bucket** and per lead time
- Sharpness conditional on calibration

**Validation discipline:**
- Walk-forward / blocked time-series CV **by date**. Never random-split — weather is heavily autocorrelated and random splits will lie to you spectacularly.
- Hold out entire seasons.
- Report skill separately by lead time (T−120h, −72h, −48h, −24h, −6h, intraday).

**The only test that actually matters — market-relative backtest:**
For every resolved London market, compare model distribution vs the market's implied (normalized) distribution at each timestamp. Measure edge in cents-per-bucket, net of the 5% taker fee where you'd cross, and simulated maker fills where you'd rest.

> **Gate to live trading:** model must beat market-implied on out-of-sample RPS across ≥100 market-days, at the specific lead times you intend to quote. If it doesn't beat the market, you have a science project, not a trading strategy.

---

## 4. Where the competitive advantage actually comes from

Ranked by expected contribution.

**1. Modelling the settlement operator, not the weather (§0).**
Everyone can pull the same ECMWF ensemble. Almost nobody models `max(round(·))` over a discrete, possibly gap-ridden observation schedule at one specific station. This is a pure structural edge in the bucket tails, and it does not decay as more people arrive with better weather models.

**2. Intraday partial observation (Model D).**
The target becomes progressively observed through the day. A real-time METAR loop plus a remaining-peak model dominates anyone pricing off a morning forecast. Highest ROI per unit of work.

**3. MOGREPS-UK 2.2 km (§1.2).**
The highest-resolution ensemble covering London, free, and *not* available through Open-Meteo — so anyone taking the path of least resistance doesn't have it. At 2.2 km it resolves the Thames estuary and urban London; at IFS ENS's 18 km, EGLC and Heathrow are nearly the same gridpoint, which they are emphatically not in reality.

**4. Station micro-climate (EGLC ≠ "London").**
Royal Docks, ~6 m elevation, water on both sides, dense East London. Hypotheses to test: easterly flow ⇒ estuary air caps the afternoon high; calm/clear ⇒ UHI boost. A wind-direction-conditioned bias correction is learnable and persistent. Traders anchoring on Heathrow or a generic "London" forecast are anchored on the wrong station.

**5. Full-vector coherence.**
The market's own mids summed to **~1.03** on Aug 2. Participants quote buckets independently; a model that emits a coherent normalized 11-vector prices relative value *across* buckets, which is a different and less crowded game than being right about the point forecast.

**6. Fee-structure awareness.**
`5% taker-only, exponent 1, 25% maker rebate`. Correctly modelling where the fee band makes you unhittable vs. where you'll get run over is worth more than a small amount of forecast skill.

> **Correction (Phase 4).** This section originally assumed
> `fee = 0.05 × min(p, 1−p) × shares` and concluded that a ~2.5¢ midpoint fee
> creates "an enormous no-trade band that *protects* resting maker quotes."
> The published formula is `shares × rate × [p(1−p)]^exponent` — a product, not
> a minimum. The old form overstates the fee everywhere by `1/(1−p)`: **2× at
> the midpoint**, converging to correct deep in the tails.
>
> The real midpoint band is **1.25¢**, not 2.5¢, so a resting quote is **half as
> protected** as this section claimed. The tail figure was roughly right
> (~0.24¢ at p≈0.05), but tails were never the protected part. See
> `src/weatherbot/fees.py`.

### Honest assessment of the disadvantages

- **Capacity is limited.** $40–150k volume per market-day means this scales to maybe low-five-figures of working capital before you're moving the market against yourself. This is a skill-building and steady-yield project, not a scaling business.
- **Someone competent is likely already there.** 1¢ spreads at the 26/27°C buckets on Aug 2 don't happen by accident. Assume at least one serious participant.
- **Rewards are modest** (~$100/day across the event, concentrated near the money at 4.5¢ max spread / $20–100 min size). Treat rewards as a subsidy on learning, not the thesis. The thesis has to be forecast edge.
- **Thin ensemble archives** (§1.3) mean MOS training data is the binding constraint for the first months.
- **Single-station dependency**: if EGLC data goes stale or the airport changes reporting practice, both your model and your ability to price go dark simultaneously. Needs a kill switch.

---

## 5. Sequencing

| Phase | Work | Gate to pass |
|---|---|---|
| ~~**0**~~ | ~~`resolve()` + Phase 0 checklist~~ **DONE** — 503/504 over 535 markets | ~~passed~~ |
| **1** | Data pipeline: IEM history, Open-Meteo ensembles, Met Office DataHub, ERA5 | Reproducible daily feature table, backfilled as far as sources allow |
| **2** | Model A (climatology) | Calibrated; beats uniform decisively |
| **3** | Model B (raw ensemble) | Beats A on RPS at ≤72h lead |
| **4** | Model C2 (MOS + discretization operator) | Beats B on RPS at every lead time |
| **5** | Model D (intraday nowcast) | Beats C on RPS for same-day, post-09:00 local |
| **6** | Model E (stack + calibrate) | Reliability diagrams flat; beats all components |
| **7** | Market-relative backtest | **Beats market-implied on ≥100 market-days** |
| **8** | Paper trade against live book | Positive simulated P&L over ≥30 days |
| **9** | Quoting engine (separate doc) | — |

**Start today:** Phase 0 verification *and* the forecast harvester. Everything else can wait; lost archive days cannot be recovered.

---

## Appendix — key constants

```
Station:          EGLC (London City Airport), 51.505°N, 0.055°E, ~6 m
Resolution:       Wunderground, EGLC, whole °C, max over all times on date
Outcomes:         11 ordered buckets, edges shift seasonally
Rewards:          max_spread 4.5¢, min_size $20–100/outcome, ~$100/day/event
Fees:             taker_only, rate 0.05, exponent 1, rebate_rate 0.25
                  fee = shares × rate × [p(1−p)]^exponent   [VERIFIED Phase 4]
                  makers pay 0; band is 1.25¢ at p=0.5, 0.24¢ at p=0.05
Min resting time: 3.5 s to qualify for rewards (rule effective 17 Mar 2026)
Observed size:    Jul 30 2026 market — $151k volume, $55k liquidity
```
