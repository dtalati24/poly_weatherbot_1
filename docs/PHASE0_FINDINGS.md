# Phase 0 Findings — Settlement Reconstruction

**Status: complete, gate passed.**
Validated over **2025-01-01 → 2026-08-01**: 535 settled markets, 504 with a
pinned outcome (single-degree or two-degree range), 31 open-ended tails.

| Observation source | Pinned settlements reproduced | Rate |
|---|---|---|
| **IEM raw METAR** | **503 / 504** | **99.80%** |
| Weather Underground archive | 502 / 504 | 99.60% |

Every remaining mismatch is a documented feed anomaly
(`src/weatherbot/anomalies.py`). The gate fails on any *undocumented* mismatch,
so this is a real check, not a rounded-down pass.

---

## 1. The settlement rule

```
Y = max over observations, grouped by London-LOCAL calendar day,
    of the whole-degree Celsius temperature at EGLC
```

Verified component by component:

**Station: EGLC (London City Airport).** Royal Docks, ~6 m elevation. Not
Heathrow. Reports half-hourly at **:20 and :50**, 48 observations per day.

**Day boundary: LOCAL (Europe/London), not UTC.** Confirmed two ways:
- *Directly*: WU's history endpoint for a given date returns the block running
  00:20–23:50 in station-local time, shifting by an hour between GMT and BST.
  Requesting 2026-05-12 returns a block starting 2026-05-11 23:20Z = 00:20 BST.
- *Empirically*: local scores 534/535 vs UTC's 533/535 across all markets. The
  three days that discriminate between the two boundaries:

  | Date | Settled | Local | UTC | Agrees with |
  |---|---|---|---|---|
  | 2025-10-04 | 63°F or higher | 18°C | 15°C | **local** |
  | 2025-10-14 | 56-57°F | 14°C | 13°C | **local** |
  | 2026-05-27 | 24°C | 25°C | 24°C | UTC — but see §3 |

  All three are in BST, so this is not a DST artifact. The lone UTC-favouring
  case turned out to be a WU ingest gap, not a boundary rule.

**Rounding: none needed.** EGLC METARs encode temperature in whole degrees
Celsius natively, so `tmpc` is already integer-valued. The hypothesis that WU
stores Fahrenheit internally and double-rounds on display is **untestable and
harmless here**: integer Celsius survives a C→F→C round trip unchanged, so
`as_reported` and `via_fahrenheit` produce identical results on every day in
the sample.

**Report type: irrelevant.** All EGLC observations in the archive are automated
routine (`AUTO`) reports. Filtering SPECIs changes nothing.

---

## 2. The curfew hypothesis is FALSE

PLAN.md flagged a potentially large edge: London City Airport closes **Saturday
~12:30 → Sunday ~12:30 local** (confirmed in the UK AIP, EG-AD-2.EGLC). If
observations stopped during closure, Saturday maxima would settle
systematically low.

**They do not stop.** The observing system is fully automated and runs 24/7
straight through the closure.

| Day | Mean obs/day | Min | Mean last obs (local) |
|---|---|---|---|
| Mon–Fri | 47.9–48.0 | 43 | 23.8 |
| **Sat** | **47.7** | 25 | **23.7** |
| **Sun** | **48.0** | 46 | **23.8** |

Afternoon coverage (12:00–18:00 local) is uniform across all seven weekdays.

> **Truncation risk: 0 / 579 days (0.00%).** There is no day in the sample whose
> maximum fell at the final observation before 17:00 local. The record covers
> the diurnal peak every single day.

This hypothesis is closed. Do not spend more time on it.

---

## 3. The real anomaly: the settlement source is mutable

Polymarket settles against Weather Underground **as it stood at resolution
time** — a snapshot that cannot be recovered afterwards. WU sometimes drops
individual METARs, and its archive keeps changing after the fact. The result is
that *neither* feed reproduces settlement perfectly, and they fail on
**different days**:

| Date | Settled | IEM | WU | Matched | What happened |
|---|---|---|---|---|---|
| 2025-03-30 | 63-64°F | 17°C | 11°C | IEM | WU endpoint returns a broken **2-observation** block |
| 2026-05-12 | 17°C | 17°C | 16°C | IEM | WU lost the 13:50Z daily peak *after* resolution |
| 2026-05-27 | 24°C | 25°C | 24°C | **WU** | WU was already missing the 23:20Z obs at resolution |

2026-05-12 and 2026-05-27 are mirror images: in one, WU lost the peak after
settling; in the other, the gap was present when it settled.

The 2025-03-30 break is **isolated, not a DST bug** — the other spring-forward
date in range (2026-03-29) returns a normal 46-observation block. Exactly one
WU day-block in 579 has fewer than 40 observations.

### Why this matters for trading

- **Use IEM raw METAR as the primary reconstruction.** It wins 503/504 vs
  502/504, and its archive is stable and goes back decades.
- **Poll both feeds live.** Days where they disagree are days where the settled
  value is genuinely uncertain between two adjacent buckets. That is a signal
  to *widen the distribution and cut size*, not to quote confidently.
- **The disagreement is detectable in real time**, and anyone pricing off a
  single feed carries an unhedged one-bucket error on ~0.4% of days.

---

## 4. Traps found in the market data

**Slugs do not identify a date.** Polymarket has used a year-suffixed form
(`...-july-30-2026`) and an unsuffixed one (`...-may-17`), inconsistently across
years. `highest-temperature-in-london-on-may-17` is the **2025** market. Matching
on slug alone silently pairs one year's observations with another year's
settlement — this produced a plausible-looking but meaningless 97.8% on the
first run. Every event's target date is now verified against `eventDate` /
`endDate` before use.

**Bucket formats are not stable.** Observed across the sample:

| Era | Outcomes | Format |
|---|---|---|
| 2025 (parts) | 7 | Two-degree Fahrenheit ranges — `54-55°F`, `64–65°F` |
| 2026-01 | 7 | Single-degree Celsius |
| 2026-03 | 9 | Single-degree Celsius |
| 2026-07/08 | 11 | Single-degree Celsius |

Both hyphen and en-dash separators appear. A naive integer scan reads `54-55°F`
as `54` and `-55`, because the separator looks like a minus sign. Bucket edges
and units must be read per market and never hardcoded.

---

## 5. Incidental findings worth carrying into Phase 1

**The daily maximum is not always in the afternoon.** Hour of maximum, local,
over 579 days:

| Hour | Share |
|---|---|
| 12:00–15:00 | 66.5% |
| 11:00 | 7.1% |
| **00:00** | **6.4%** (37 days) |

On roughly 1 day in 16 the maximum is set **just after local midnight**, on
warm-air carryover from the previous evening. The intraday nowcast model
(PLAN.md Model D) must not assume the peak is still ahead — on those days the
running maximum is already final by 01:00, which is a large and very cheap edge
against anyone assuming an afternoon peak.

**Market economics** (from the Gamma API, Aug 2 2026 event): 11 outcomes,
$55.7k liquidity, $38.8k volume, $13.2k open interest; the resolved Jul 30 event
did $151k volume. Fees are `taker_only, rate 0.05, exponent 1, rebate_rate 0.25`
— takers pay, makers earn a 25% rebate. Liquidity rewards run ~$100/day per
event at 4.5¢ max spread and $20–100 min size, concentrated in the near-the-money
buckets.

> Fee formula still **unverified**: assumed `0.05 × min(p, 1−p) × shares`.
> Confirm before sizing anything.

---

## 6. Reproducing this

```bash
py scripts/validate_resolve.py --source iem            # 503/504, gate passes
py scripts/validate_resolve.py --source wunderground   # 502/504, gate passes
py scripts/compare_sources.py                          # feed divergence detail
py scripts/analyze_cadence.py --source iem             # curfew / truncation
py -m pytest                                           # 33 tests
```

First run downloads ~580 days from each feed and takes several minutes;
everything is cached under `data/` thereafter.

---

## Next: Phase 1

Start the **forecast harvester immediately**. MOGREPS-UK on AWS is a **30-day
rolling archive** and most Open-Meteo model archives begin only in April 2026.
Every day without harvesting is training data permanently lost — that is the one
part of this project that cannot be caught up later.
