# Phase 1 — Forecast Data Pipeline

**Status: harvester live, backfill complete.**

Two facts shaped every decision here, both verified empirically rather than
assumed:

1. **Ensemble history cannot be backfilled.** Open-Meteo's ensemble endpoint
   returns ~4 days of data no matter what you request — `past_days=60` yields
   1464 timestamps and 96 non-null values. Ensemble training data can only be
   accumulated forward.
2. **Deterministic history *can* be backfilled, at known lead times.** The
   `<var>_previous_dayN` variables work on the Historical Forecast API, not just
   the live endpoint. That yields an honest training set today instead of in a
   year.

---

## 1. What we harvest, and why

### Ensembles — 4x daily, forward only

| Model | Members | Horizon | Role |
|---|---|---|---|
| `ecmwf_ifs025` | 51 | 15 d | **Primary.** Harvest failure here fails the job |
| `ecmwf_aifs025` | 51 | 15 d | ECMWF's ML model — errors partly independent of IFS |
| `gfs025` | 31 | 10.4 d | NCEP GEFS |
| `icon_eu` | 40 | 5.3 d | DWD regional |
| `icon_d2` | 20 | 2.0 d | 2.2 km, but London sits near the domain edge |
| `gem_global` | 21 | 15 d | Environment Canada |
| `ukmo_global_ensemble_20km` | 18 | 10.3 d | MOGREPS-G |
| `ukmo_uk_ensemble_2km` | **3** | 5.6 d | Nominally MOGREPS-UK — see below |

> **`ukmo_uk_ensemble_2km` is not real MOGREPS-UK.** Open-Meteo exposes only 2
> perturbed members plus the mean. Genuine MOGREPS-UK carries ~18. Useful as a
> high-resolution signal, useless as a distribution.

Only `temperature_2m` is requested at member resolution. The ensemble endpoint
returns every variable for every member, so each added variable multiplies the
payload by the member count. Auxiliary features come from a single deterministic
call instead.

**Cost:** ~54 KB per harvest slot, ~78 MB/year at 4x daily. Committed to git.

### Met Office IMPROVER spot percentiles — the free MOGREPS-UK route

MOGREPS-UK *gridded* is a paid DataHub product. But the Met Office publishes its
post-processed blend on AWS Open Data, **anonymous, no key, no requester-pays**:

```
bucket : met-office-uk-spot-percentiles   (eu-west-2)
key    : uk-spot-percentiles/{YYYY}/{MM}/{DD}/T{HHMM}Z/
         {validTime}-B{blendTime}-temperature_at_screen_level_max-PT12H.nc
```

Verified directly:
- `air_temperature`, shape **(15 percentiles, 8667 sites)**, kelvin, `cell_methods = "time: maximum"`
- percentiles **5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 75, 80, 85, 90, 95**
- `mosg__model_configuration = "ecgl_ens gl_ens uk_ens"` — **`uk_ens` is MOGREPS-UK**, blended and calibrated in
- `title = "IMPROVER Post-Processed Multi-Model Blend UK Spot Values"`
- nearest site to EGLC: **51.5048 / 0.0580, altitude 5 m, ~305 m away**
- refreshed every 15 minutes

This is arguably *better* than raw MOGREPS-UK for our purpose: already
bias-corrected, already blended, already spot-extracted at the resolving station,
already in percentile form, and free. It costs ~1.1 KB per harvest.

> ### ⚠️ The window is NOT our target — do not confuse them
>
> These are maxima over a **12-hour window**, not the local calendar day. From
> `time_bnds`, the file valid at 18:00Z covers **06:00Z–18:00Z** = 07:00–19:00
> local BST.
>
> Measured against 579 days of EGLC observations, the true daily maximum falls
> inside 07:00–19:00 local on **90.3%** of days. On the other **9.7%** it does
> not — dominated by the just-after-local-midnight carryover case, which is
> **6.4%** of days on its own.
>
> Treat these percentiles as a strong **feature**, never as the target. A model
> that equates them with the settlement variable will be wrong roughly one day
> in ten, and biased **low** when it is wrong.

Storage exception: the raw NetCDF is ~450 KB because it carries all 8667 UK
sites. We archive only the single-site extraction plus the source object key,
which keeps records ~1 KB and stays reproducible while the object is inside its
30-day window.

### Deterministic backfill — the training set that exists today

`temperature_2m` plus `temperature_2m_previous_day1..5`, so every valid hour
carries the forecast as issued 1–5 days earlier. **Training on the lead time you
actually trade is the difference between an honest model and one that backtests
brilliantly and loses money live.**

| Model | From | Chunks | Size |
|---|---|---|---|
| `ecmwf_ifs025` | 2024-03-01 | 8 | 0.34 MB |
| `gfs_seamless` | 2022-01-01 | 14 | 0.64 MB |

ECMWF's archive genuinely begins ~March 2024 — 2024-01-01 returns all nulls,
2024-03-01 is complete. GFS reaches back to 2022, giving a longer if weaker
history.

---

## 2. Leakage controls

Every archived record carries **`harvested_at_utc`**. That is the leakage anchor:
the instant we could first have known the forecast. No backtest may use a record
to predict anything at or before its own harvest time.

Rules for Phase 2:

- **Never use ERA5 reanalysis as a live feature.** It is built from future
  observations and is not available in real time. Safe for learning the
  station-vs-gridcell relationship offline; catastrophic as a model input.
- **Use `previous_dayN` at the lead you trade**, not the base series (which is
  effectively near-analysis).
- **No random train/test splits.** Weather is heavily autocorrelated. Blocked,
  walk-forward splits by date; hold out whole seasons.
- **Fit calibration out-of-fold**, or it memorises the test set.
- **Benchmark against the market**, not climatology.

---

## 3. Storage layout

```
archive/                 tracked in git — IRREPLACEABLE
  forecasts/{model}/{YYYY}/{YYYY-MM-DD}THHZ.json.gz
  backfill/{model}/{start}_{end}.json.gz
data/                    gitignored — reproducible caches
```

Records are gzipped JSON with the **raw upstream payload preserved verbatim**
(except the Met Office extraction noted above). Any normalisation applied today
is a guess about what the model will need; the raw response is the only thing
certain to be complete. Writes are atomic via temp-file-then-rename, and gzip
mtime is pinned to 0 so identical content produces identical bytes rather than
spurious git diffs.

---

## 4. Scheduling

`.github/workflows/harvest.yml` — cron `0 3,9,15,21 * * *`, a few hours after the
00/06/12/18Z model cycles. Commits the archive back to the repo.

- Reruns within the same UTC hour are **idempotent** (archive paths are
  hour-resolution), so a retried workflow cannot double-write.
- Exit code is 0 if the primary model landed even when secondary models failed —
  a red build should mean something is actually wrong, not that one upstream
  feed hiccuped.
- Push conflicts rebase and retry up to 3 times, so two overlapping runs cannot
  drop a record.
- The workflow's own commits keep the repo active, which prevents GitHub from
  auto-disabling the schedule for inactivity.

---

## 5. Reproducing

```bash
py scripts/harvest_forecasts.py                       # one harvest slot
py scripts/backfill_deterministic.py --model ecmwf_ifs025
py scripts/backfill_deterministic.py --model gfs_seamless --start 2022-01-01
py -m pytest                                          # 59 tests
```

---

## Next: Phase 2

Build the feature table joining archived forecasts to settled outcomes, then
Model A (climatology) as the benchmark every later model must beat. The
deterministic backfill is already sufficient to start; the ensemble archive
accumulates in parallel.
