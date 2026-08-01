# Phase 3 — Model B (forecast MOS)

**Status: beats the benchmark decisively at tradeable lead times.**

Same held-out window as Phase 2 — **207 settled markets, 2026-01-01 → 2026-08-01**
— so the numbers are directly comparable. Error distributions fitted only on
days before the evaluation window, separately per lead.

| Lead | RPS | log loss | vs positional (0.11760) | vs uniform |
|---|---|---|---|---|
| **1 day** | **0.07261** | 1.5562 | **+38.3%** | +46.2% |
| **2 days** | **0.08566** | 1.7020 | **+27.2%** | +36.5% |
| 3 days | 0.09004 | 1.7507 | +23.4% | +33.3% |
| 4 days | 0.11293 | 1.9645 | +4.0% | +16.3% |
| 5 days | 0.13429 | 2.1702 | **−14.2%** | +0.5% |

**Markets are created ~2 days ahead**, so leads 1–2 are the tradeable region —
exactly where the model is strongest, at **+27% to +38%** over the benchmark.

Beyond four days the forecast is worse than knowing nothing about the weather.
At lead 5 the structural baseline wins by 14%. That crossover is a real
operational boundary: **for anything more than four days out, quote positional
climatology, not the forecast.**

---

## 1. The model

```
Y = forecast_daily_max + e,   e ~ empirical error distribution for this lead
```

That is the whole thing. It learns one quantity — how wrong this forecast tends
to be, and in which direction — and that single mechanism absorbs three
distinct effects at once:

- **Grid-vs-station bias.** Open-Meteo's grid point is not EGLC.
- **The settlement operator.** The target is a maximum over discrete
  half-hourly *integer* observations; the forecast is a maximum over smooth
  hourly values. Phase 0 established that this is a fixed distortion; here it
  is learned rather than derived.
- **Lead-dependent spread**, by fitting a separate error distribution per lead.

## 2. The bias is large, stable, and probably in the market's price

Fitted error (observed − forecast) on 671 training pairs per lead:

| Lead | Mean error | SD |
|---|---|---|
| 1 | **+0.519** | 1.178 |
| 2 | **+0.501** | 1.314 |
| 3 | **+0.509** | 1.487 |
| 4 | **+0.501** | 1.694 |
| 5 | **+0.488** | 1.955 |

Two things stand out.

**The bias is ~+0.5 °C and essentially constant across leads**, while the
spread grows exactly as it should (1.18 → 1.96). A bias that does not decay with
lead is not a forecast-skill problem — it is a fixed offset between the grid
point and the settled station. The raw Open-Meteo ECMWF forecast **runs about
half a degree cold** for EGLC settlement, always.

**And it lines up with the market.** Phase 2 measured the bucket window centre
sitting **+0.41 °C** below the observed value. Model B measures the raw forecast
sitting **+0.50 °C** below it. Those are close enough to suggest a single
explanation:

> Polymarket appears to centre its bucket window on an **uncorrected** forecast,
> inheriting the same cold bias.

If that holds, the bias is not merely a modelling nuisance to remove — it is
present in the market's own framing, and correcting for it is precisely the edge.
This is a hypothesis consistent with two independently measured numbers, **not**
a verified causal claim; it deserves a direct test against quoted prices rather
than settled outcomes.

## 3. Calibration

Lead 1, pooled bucket probabilities:

| forecast range | n | mean forecast | observed | gap |
|---|---|---|---|---|
| 0.0–0.1 | 1284 | 0.0215 | 0.0187 | +0.0028 |
| 0.1–0.2 | 296 | 0.1487 | 0.1081 | +0.0406 |
| 0.2–0.3 | 334 | 0.2587 | 0.2934 | −0.0347 |
| 0.3–0.4 | 117 | 0.3092 | 0.3590 | −0.0497 |
| 0.4–1.0 | 18 | — | — | noisy, n too small |

Good in the populated bins, with mild **under**confidence in the middle — the
model is slightly too cautious where it should commit. That is the safer
direction to be wrong, and a candidate for the calibration layer in Model E.

## 4. What this model deliberately does not do

- **No ensemble information.** It uses a single deterministic forecast, so it
  cannot say *"today is unusually uncertain."* The spread it applies is
  climatological per lead, not situational. The ensemble archive is currently
  two slots deep; this is the main upgrade once it fills.
- **No synoptic conditioning.** No wind direction, cloud, or the estuary/sea-
  breeze effects hypothesised in PLAN.md.
- **No use of the bucket window**, despite §2 showing it carries information.
- **One model only** (`ecmwf_ifs025`). No multi-model blending.

Each is a separate, testable increment rather than a caveat.

## 5. Reproducing

```bash
py scripts/evaluate_model_b.py
py scripts/evaluate_model_b.py --model gfs_seamless
py -m pytest                                        # 141 tests
```

---

## Next

1. **Model C** — condition the error distribution on ensemble spread, so
   uncertainty becomes situational instead of climatological.
2. **Model D** — the intraday nowcast. Phase 0 found the daily maximum is
   already set by 01:00 local on 6.4% of days, and the running maximum bounds
   the answer from below all day. This is likely the single largest remaining
   edge.
3. **Test the §2 hypothesis** against quoted prices rather than settled
   outcomes: if the market's window really is built on an uncorrected forecast,
   the mispricing should be visible in the order book, not just in hindsight.
