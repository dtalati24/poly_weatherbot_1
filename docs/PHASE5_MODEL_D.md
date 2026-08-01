# Phase 5 — Model D (intraday nowcast)

**Status: beats every previous model decisively. Still does not beat the market
until ~21:00, and by then the mispricing is too small to trade.**

Model D was the best remaining candidate after Phase 4, for a specific reason:
it is the only model whose information is *timely* rather than merely accurate.
Part-way through the settlement day the running maximum M is not an estimate of
the answer, it is a piece of the answer already in hand.

The structural thesis holds completely. The trading conclusion does not.

| local hour | n | market | Model D | Model D′ | Model D″ | Model B | best vs market |
|---|---|---|---|---|---|---|---|
| 06 | 230 | **0.05716** | 0.19389 | 0.14596 | 0.08050 | 0.08067 | −40.8% |
| 09 | 226 | **0.05522** | 0.17894 | 0.12373 | 0.08077 | 0.08095 | −46.3% |
| 11 | 212 | **0.05213** | 0.13443 | 0.08971 | 0.08173 | 0.08295 | −56.8% |
| 13 | 185 | **0.04404** | 0.10020 | 0.06491 | 0.07602 | 0.08532 | −47.4% |
| 15 | 129 | **0.02989** | 0.05467 | 0.04360 | 0.06630 | 0.08885 | −45.9% |
| 17 | 91 | **0.00962** | 0.01551 | 0.01461 | 0.04813 | 0.08914 | −51.8% |
| 19 | 75 | **0.00282** | 0.00344 | 0.00372 | 0.02020 | 0.09112 | −21.8% |
| **21** | 72 | 0.00137 | **0.00003** | 0.00006 | 0.00170 | 0.08901 | **+97.8%** |

Three variants, each isolating one idea:

- **D** — rise distribution conditioned on local hour only.
- **D′** — plus the gap between the running maximum and the day's forecast.
- **D″** — `Y = max(M, X)`, with X predicted MOS-style from the forecast's own
  maximum over the *remaining* hours.

---

## 1. The structure is right

`Y = max(M, X)` is an identity, not an approximation: `running_max` and
`remaining_max` partition the day using the same settlement operator, so their
maximum reconstructs the settled value exactly. That is asserted in the tests
rather than assumed.

The lock-in curve behaves exactly as Phase 0 predicted, and was verified
independently over **4,576 days** (2014–2026):

| local hour | 11 | 13 | 15 | 17 | 19 | 21 |
|---|---|---|---|---|---|---|
| P(maximum already set) | 0.155 | 0.394 | 0.749 | 0.932 | 0.972 | 0.983 |

By 17:00 the answer is already fixed on 93% of days. **Model D″ beats Model B at
every single hour**, and D beats it from 13:00 — so the intraday information is
worth a great deal relative to a forecast. It is just not worth anything
relative to the price.

Two findings from the independent climatology that changed the model:

- **The curve is steep**, running 19.7% at 11:00 to 96.3% at 17:00. The first
  implementation used a 1.5-hour kernel with a 6-hour cutoff, which smeared that
  transition badly — fitted P(locked) at 15:00 came out 0.710 against a true
  0.877. Narrowed to 0.6 h and fitted on all 24 hours rather than the 8
  evaluation hours.
- **The seasonal curves cross.** Winter is only 91.5% locked at 17:00 against
  summer's 98.5%, because DJF is bimodal — a fat overnight-carryover mode *and*
  a fat evening advective tail — while JJA is purely insolation-driven and is
  100% locked by 20:00 across 1,163 summer days. Model D has no season term yet;
  this is the clearest single upgrade available.

## 2. Why it loses anyway

Model D alone throws the forecast away, so at 06:00 it knows the overnight low
and the *typical* rise from there while the market knows today's forecast. That
is not a tuning problem and no amount of smoothing fixes it — hence D″, which
keeps both. D″ duly beats Model B everywhere and closes most of the gap early,
but the market is still ~40–50% ahead through the middle of the day.

The market, in other words, is already doing the nowcast. It has the same
observations we do.

## 3. The 21:00 result is real but not tradeable

The one cell where we win is large: **+97.8%** at 21:00, and holding prices
forward through the evening (see §5) it is +93% at 21:00 and +97% at 23:00 on a
doubled sample. That is a genuine, repeatable edge in RPS.

It is not money. Two measurements say so:

**We are not better on the winning bucket.** Mean probability assigned to the
bucket that actually settled, model minus market:

| hour | 17 | 18 | 19 | 20 | 21 | 22 | 23 |
|---|---|---|---|---|---|---|---|
| edge (¢) | −0.80 | −1.94 | −1.28 | −0.86 | −0.27 | +0.18 | +0.43 |

Negative until 22:00. The RPS win comes from being *sharper*, not from
disagreeing usefully.

**The buckets the market should have ruled out are already priced out.**
Probability the market leaves on buckets the running maximum has made
impossible:

| hour | 15 | 17 | 19 | 21 | 23 |
|---|---|---|---|---|---|
| median (¢) | 0.10 | 0.20 | 0.20 | 0.20 | 0.20 |
| p90 (¢) | 0.29 | 0.35 | 0.35 | 0.35 | 0.35 |

A median of **0.2¢**, with the 1.57¢ mean being a single outlier day. Against
any realistic cost of trading that is nothing. The market has already done this
subtraction.

## 4. The lower bound: audited, and it does break

`Y ≥ M` is what gives the model its power and is the one assumption that could
make it catastrophically overconfident. Audited over 1,673 days and 535 settled
markets:

- WU's daily maximum falls below IEM's on **6 / 1,673 days (0.36%)**; four are
  DST archive truncation, two are genuine ingest drops.
- The bound would have **zeroed the actual winner once: 2026-05-27** — 1 / 535 =
  **0.19%** (Wilson 95%: 0.03%–1.05%).
- **On the evaluation window the bound never failed once** across 226 days.

The single failure has the worst available shape: the false bound was set by the
*first observation of the local day* (23:20Z carried into BST 27 May) and held
for 23.5 hours — the entire trading session, not a harmless late-evening blip.

So the bound is softened rather than asserted. `below_bound_mass = 0.005`,
because expected log loss under a floor ε is minimised at ε = the true violation
rate and the penalty is violently asymmetric: too large costs ~ε nats linearly,
too small costs **infinity** the first time it bites. 0.5% costs 0.005 nats on
the 99.8% of days the bound holds, against ~1.0–1.5 nats typical for an
eleven-bucket market.

Placement matters more than size, and the original implementation got it wrong:
it spread the floor uniformly over every bin down to −15 °C, spending almost all
of it on temperatures with no historical support. Every observed violation was
exactly −1 °C, and conditional on the peak observation vanishing the maximum
falls by 1 °C with ~99.7% probability. The floor is now **90% / 9% / 1%** across
`M−1`, `M−2`, and everything below.

**Unresolved and worth stating.** 2026-05-27 is also the one day out of 535 that
discriminates the UTC day boundary *in favour of UTC* — only three days
discriminate at all, and the LOCAL boundary rests on a 2–1 vote. So that day has
two equally consistent readings: WU dropped the METAR, or settlement excludes
the BST carryover window. The available data cannot separate them. Both point to
the same mitigation.

## 5. Corrections

**The 2025-03-30 anomaly is a DST bug.** `anomalies.py` recorded it as *"isolated
to this one day… so this is not a DST bug"*, on the strength of two
spring-forward dates. With five in view, **four are truncated** to exactly two
observations at the 01:00Z transition (2022-03-27, 2023-03-26, 2024-03-31,
2025-03-30); only 2026-03-29 is clean. 2023-03-26 escapes the mismatch list
purely because its maximum fell in the surviving observations. It reproduces on
re-fetch, so it is a permanent archive defect. Fall-back Sundays are affected
more mildly (WU 46 observations against IEM's 50, silently losing the repeated
local hour). **Consequence: WU-derived labels are wrong by 3–6 °C on
spring-forward Sundays and must be screened on block size before training.**

**`is_special` is dead at EGLC.** Zero SPECI reports in the entire IEM archive,
2005–2026, ~330k observations. 99.996% are at :20/:50. No day's maximum in 1,673
is carried by an off-cycle report, so the `ReportFilter.ROUTINE_ONLY` strategy
branch is a no-op at this station and SPECI cannot be used as a risk flag.

**WU and IEM never disagree on a value, only on presence.** Of 79,927 shared
timestamps, exactly one differs. All WU temperatures are integers. So the
`via_fahrenheit` double-rounding hypothesis carries no live risk, and every
divergence is a missing observation.

**A DST bug in `local_midnight`, caught by a test.** Subtracting two aware
datetimes that share a `tzinfo` object makes Python skip the inter-zone
adjustment, so two "London midnights" spanning a DST change appeared exactly 24
hours apart when they are 23 or 25. The docstring claimed the opposite of what
the code did. `local_midnight` now returns UTC, and `local_instant` is elapsed
time from local midnight — which also removes the possibility of being asked for
01:00 on the spring-forward day, a wall-clock time that does not exist.

## 6. What this changes

**The market prices this contract well at every horizon we can now measure** —
two days out (Phase 4), one day out (Phase 4), and hour by hour through the
settlement day (here). That is three independent attempts, and the honest
reading is that the edge is not in better estimation of the same public
information.

Things genuinely not yet tried, in the order I would now rank them:

1. **Season-conditioned lock-in.** The clearest known defect: winter evenings
   are far less locked than the pooled curve says, and the model is currently
   overconfident exactly there.
2. **Latency, not accuracy.** Every comparison here is at a fixed instant with
   observations filtered on *valid* time. The market reacts to a new METAR at
   some speed; if that speed is slower than ours, the edge is in the minutes
   after :20 and :50, which no hourly comparison can see. This is a different
   question from the one Phases 4 and 5 asked, and it is the one still open.
3. **Model C — ensemble spread.** Untouched, and the archive is filling.
4. **Multi-model blending.** Eight ensembles harvested, one used.

**Operational item worth doing regardless.** The audit could not bound one risk:
WU's archive is mutable, so any day where WU-live dropped a peak, settled low,
and later restored the observation is invisible to a retrospective study.
Snapshotting the WU live block hourly alongside the forecast harvest would turn
`1/535` from an inference on contaminated data into a measurement within months.

## 7. Reproducing

```bash
py scripts/evaluate_model_d.py
py scripts/evaluate_model_d.py --hours 15 17 19 21 23
py -m pytest                                        # 243 tests
```
