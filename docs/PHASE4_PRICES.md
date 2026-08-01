# Phase 4 — the reality check

**Status: the model does not beat the market. There is no tradeable edge yet.**

Phases 2 and 3 scored the model against statistical baselines and it won
comfortably — Model B beat positional climatology by 38% at lead 1. That
answers "does the model know something about the weather." It does not answer
the question that decides whether to trade, which is whether the model knows
something *the price does not*.

It does not. Scored against the market's own midpoints, at the same instant, on
the same buckets, over **382 settled markets**:

| Lead | n | market | Model B | Model B′ | positional | best model vs market |
|---|---|---|---|---|---|---|
| **1 day** | 382 | **0.06731** | 0.08137 | 0.08255 | 0.11282 | **−20.9%** |
| **2 days** | 300 | **0.07971** | 0.09485 | 0.09414 | 0.11259 | **−18.1%** |

The market is ~20% sharper than our best model at both tradeable leads. Quoting
around this model would not be market making; it would be donating.

---

## 1. Three Phase 3 claims that turned out to be wrong

Phase 3's headline was a hypothesis, explicitly flagged as needing a test
against quoted prices rather than settled outcomes. Tested now, it fails — and
so do the two claims it rested on.

### The forecast bias is not constant. It is not even the same sign.

Phase 3 measured mean error (observed − forecast) of **+0.519 °C at lead 1** and,
finding it near-identical across leads, concluded it was *"a fixed offset
between the grid point and the settled station… the raw forecast runs about
half a degree cold, always."*

Stability across **lead** is real. Stability across **time** was never checked,
and it does not hold. Mean error at lead 1 for July:

| July | ECMWF | GFS |
|---|---|---|
| 2024 | **+0.968** | +0.159 |
| 2025 | +0.165 | −0.534 |
| 2026 | **−0.490** | −1.477 |

A swing of ~1.5 °C, directional rather than noisy, and present in two
independent forecast models. Over the recent evaluation window the bias is
**+0.146 °C**, not +0.5.

So the +0.519 figure was not estimating a constant. It was averaging a moving
quantity over a window dominated by 2024–25 and reporting a number that
describes no particular period. Applied to July 2026 it corrects by +0.5 °C
where the truth wanted −0.5 °C — an error of roughly one full market bucket.

### The market has *not* inherited the forecast's bias.

Phase 3 observed that the market's bucket window sat +0.41 °C cold while the raw
forecast sat +0.50 °C cold, and hypothesised that *"Polymarket appears to centre
its bucket window on an uncorrected forecast… correcting for it is precisely the
edge."*

Measured against actual quoted prices:

| Lead | n | obs − implied | obs − forecast | implied − forecast |
|---|---|---|---|---|
| 1 | 192 | **−0.027** | +0.146 | +0.173 |
| 2 | 164 | **+0.097** | +0.128 | +0.031 |

The market-implied centre is within **0.1 °C** of the observed value. It is the
*raw forecast* that is biased, and the market has already corrected it. Had the
market inherited the bias, `implied − forecast` would be ≈ 0; instead the market
sits above the forecast by about the amount the forecast is wrong.

Two independently measured numbers agreeing was suggestive. It was also a
coincidence of one window, and it did not survive.

### Fixing the non-stationarity does not close the gap.

Model B′ refits the error distribution on a trailing 180-day window, so the
correction tracks the current regime. It is the obvious fix and it barely moves:
0.08255 vs 0.08137 at lead 1 (slightly worse), 0.09414 vs 0.09485 at lead 2
(slightly better). The bias drift is real, but it is not what the ~20% gap is
made of.

## 2. Does the model hold *any* information the market lacks?

Losing head-to-head would not matter if the model's errors were orthogonal to
the market's — a blend would beat both. In-sample, that looks true:

| Lead | best w | blend RPS | vs market |
|---|---|---|---|
| 1 | 0.20 | 0.06658 | +1.1% |
| 2 | 0.25 | 0.07823 | +1.9% |

It does not survive. Choosing `w` on the first half of the days and scoring on
the second, with a paired bootstrap over days (10,000 resamples):

| Lead | w | market | blend | gain | 95% CI | verdict |
|---|---|---|---|---|---|---|
| 1 | 0.25 | 0.05798 | 0.05816 | **−0.3%** | [−0.00175, +0.00143] | not distinguishable from zero |
| 2 | 0.35 | 0.06715 | 0.06951 | **−3.5%** | [−0.00559, +0.00079] | not distinguishable from zero |

The in-sample gain was the weight fitting itself to the evaluation days. On
held-out days the blend is no better than the market and possibly worse.

**Conclusion: the model currently adds nothing to the price.**

## 3. What it costs to trade

The bucket prices sum to more than 1 by the market's own cost of trading:

| Lead | median overround | min | max |
|---|---|---|---|
| 1 | 1.0505 (**5.05¢**) | 0.7655 | 1.8900 |
| 2 | 1.1310 (**13.10¢**) | 0.7985 | 3.6400 |

At lead 2 the book is more than twice as wide — the market has just opened and
has not been competed tight yet. That is where a maker is paid most, and also
where the price is least informative, so it is the natural place to look for an
edge once there is one.

For scale: the model would need to beat the market by more than half the
overround before a maker quoting inside the spread earns anything net.

## 4. A correction to the archive commit

The commit that created `archive/prices/` claimed CLOB price history is
retained for a rolling ~31 days and is therefore perishable. **That is wrong.**

The measurement was real — every bucket of every market older than ~31 days
returned zero points — but the cause was the query, not retention.
`interval=max` returns nothing for settled markets past about a month, and a
degraded ~10-minute series for recent ones. With an explicit `startTs`/`endTs`
window the same tokens return full **1-minute** history:

| market day | `interval=max` | `startTs`/`endTs` |
|---|---|---|
| 2025-06-10 | 0 points | 6599 points, 60 s |
| 2025-12-05 | 0 points | 3357 points, 60 s |
| 2026-05-01 | 0 points | 3514 points, 60 s |
| 2026-07-15 | 406 points | 4058 points, 60 s |

The archive has been rebuilt: **536 market days, 2.16 M change-points, 9.7 MB**,
back to the first London market on 2025-02-01, at 1-minute resolution. Storage
is change-points plus explicit coverage bounds, which is lossless for
`price_at` and about a tenth the size of storing every sample.

Nothing was lost, and there is no longer a deadline. The harvest workflow now
exists for freshness and as insurance, not because a missed run costs data.

## 5. Also corrected: the fee formula

Carried since Phase 0 as an explicit assumption. It was wrong — see
`src/weatherbot/fees.py`, `PLAN.md` §6 and `PHASE0_FINDINGS.md`.

```
assumed:  fee = rate x min(p, 1-p) x shares
actual:   fee = shares x rate x [p (1-p)]^exponent
```

The old form overstates everywhere by `1/(1−p)`: 2× at the midpoint, converging
to correct in the tails. The practical consequence runs against us — the
no-trade band protecting a resting quote at p = 0.5 is **1.25¢, not 2.5¢**, so a
maker is half as protected from being picked off as PLAN.md argued. Makers pay
no fee at all and earn a 25% pool-share rebate.

## 6. What this changes

**The quoter is premature.** It was already scheduled after the modelling work;
this makes the ordering non-negotiable. There is nothing to quote around until a
model beats the mid.

**The benchmark changes permanently.** Positional climatology and RPS-vs-uniform
have done their job. From here the only score that means anything is RPS against
the market at the same instant. A model that improves on Model B but still sits
above 0.0673 at lead 1 has not made progress worth trading.

**The remaining ideas are unaffected and now have a real target.** None of
Models C, D or E have been tried, and the gap to close is now a concrete number
rather than an aspiration:

1. **Model D — intraday nowcast.** Still the best candidate. Phase 0 found the
   daily maximum is already set by 01:00 local on 6.4% of days, and the running
   maximum bounds the answer from below all day. Crucially it is the one model
   whose information is *timely* rather than merely accurate, and the comparison
   above deliberately looks only at fixed instants a day or two ahead, where the
   market has had time to price everything public. Intraday is where a slow
   market is most likely to be beatable.
2. **Model C — situational spread.** The ensemble archive is now filling. Model
   B applies a climatological spread per lead; the market plainly does not.
3. **Multi-model blending.** Model B uses one deterministic run. Eight ensembles
   are being harvested and none are in the model yet.

## 7. Reproducing

```bash
py scripts/harvest_prices.py --all --skip-existing   # rebuild the archive
py scripts/analyze_prices.py                         # model vs market
py scripts/blend_significance.py                     # is the blend real?
py -m pytest                                         # 196 tests
```

---

## Honest summary

The modelling so far is sound and the infrastructure is sound. The edge is not
there. Three claims that looked like edge in Phase 3 — a constant bias, a market
inheriting it, and a correction worth trading — were each artifacts of a short
window or an untested assumption, and all three failed the moment they met real
prices.

That is the reality check working as intended, at the cost of one phase rather
than of capital.
