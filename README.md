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
  sources/
    wunderground.py      The settlement source. Train and validate on this.
    iem.py               Raw METAR archive (decades of history, free).
    polymarket.py        Gamma API: settled markets, bucket parsing.
  analysis/
    cadence.py           Observation cadence and truncation analysis.

scripts/
  validate_resolve.py    Phase 0 gate: replay every settled market.
  compare_sources.py     Quantify Weather Underground vs raw METAR divergence.
  analyze_cadence.py     Reporting cadence, curfew and truncation checks.

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
| Polymarket Gamma API | Settled markets, buckets, volumes | Free, no key |
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

**Next — Phase 2:** feature table joining forecasts to settled outcomes, then
Model A (climatology) as the benchmark everything later must beat.
