"""Polymarket fee and rebate arithmetic.

The formula the plan carried until now was **wrong**. It assumed

    fee = rate x min(p, 1-p) x shares

The published formula is a product, not a minimum:

    fee = shares x rate x [p (1 - p)] ^ exponent          (exponent is 1 today)

The old form overstates the fee **everywhere**, by a factor of `1 / (1 - p)`
for p below a half. That is 2x at the midpoint and shrinks toward 1x deep in
the tails -- so the distortion is worst exactly where the mass sits, not in the
wings:

    p      old (100 sh, rate .05)   true    overstated by
    0.50            2.50c           1.25c        2.00x
    0.30            1.50c           1.05c        1.43x
    0.10            0.50c           0.45c        1.11x
    0.01            0.050c          0.0495c      1.01x

The consequence is the opposite of comforting. PLAN.md argued that a ~2.5c
taker fee at the midpoint creates "an enormous no-trade band that *protects*
resting maker quotes". The real band is **1.25c**, half as wide, so a resting
quote is half as protected against being picked off by better information as
the plan assumed. The tail conclusion survives -- tails really are cheap to
cross, at ~0.24c at p = 0.05 -- but they were never the protected part.

Two further corrections worth stating plainly:

**Makers pay nothing.** Docs: "Makers are never charged fees. Only takers pay
fees." Every weather market sampled carries `takerOnly: true`.

**`maker_base_fee` / `taker_base_fee` / `/fee-rate` are inert.** They return
1000 for every market in every category, including markets whose real rates
differ (0.07 vs 0.04). Keying a fee model off them yields a constant. The
authoritative per-market source is gamma's `feeSchedule` object.

Rate table (taker / maker / rebate), from docs.polymarket.com/trading/fees:

    Crypto                                   0.07  0  20%
    Sports                                   0.05  0  15%
    Finance, Politics, Mentions, Tech        0.04  0  25%
    Economics, Culture, WEATHER, Other       0.05  0  25%
    Geopolitics                              0     0   --
"""

from __future__ import annotations

from dataclasses import dataclass

# London temperature markets fall in the Weather category.
WEATHER_TAKER_RATE = 0.05
WEATHER_REBATE_RATE = 0.25
DEFAULT_EXPONENT = 1


@dataclass(frozen=True)
class FeeSchedule:
    """A market's fee terms, as published in gamma's `feeSchedule`."""

    rate: float = WEATHER_TAKER_RATE
    exponent: int = DEFAULT_EXPONENT
    taker_only: bool = True
    rebate_rate: float = WEATHER_REBATE_RATE

    @classmethod
    def from_gamma(cls, market: dict) -> "FeeSchedule":
        """Read the schedule off a Gamma market payload.

        Falls back to the Weather defaults when the field is absent, which is
        the case for markets created before fees were enabled.
        """
        schedule = market.get("feeSchedule") or {}
        return cls(
            rate=float(schedule.get("rate", WEATHER_TAKER_RATE)),
            exponent=int(schedule.get("exponent", DEFAULT_EXPONENT)),
            taker_only=bool(schedule.get("takerOnly", True)),
            rebate_rate=float(schedule.get("rebateRate", WEATHER_REBATE_RATE)),
        )


def taker_fee(
    shares: float, price: float, schedule: FeeSchedule | None = None
) -> float:
    """Fee paid by the aggressor, in dollars.

    Symmetric in `price` about 0.5, which is the point of the p(1-p) form: it
    costs the same to cross for a 10c outcome as for a 90c one.
    """
    schedule = schedule or FeeSchedule()
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price must be in [0, 1], got {price}")
    return shares * schedule.rate * (price * (1.0 - price)) ** schedule.exponent


def maker_fee(shares: float, price: float, schedule: FeeSchedule | None = None) -> float:
    """Fee paid by the resting side. Zero wherever `taker_only` holds."""
    schedule = schedule or FeeSchedule()
    if schedule.taker_only:
        return 0.0
    return taker_fee(shares, price, schedule)


def fee_equivalent(
    shares: float, price: float, schedule: FeeSchedule | None = None
) -> float:
    """The quantity a maker's rebate share is computed from.

    The maker rebate is pool-based, not per-fill: for each filled maker order
    Polymarket computes this value, and pays out

        rebate = (your fee_equivalent / total fee_equivalent) x rebate pool

    per market, daily, with a $1 minimum.
    """
    schedule = schedule or FeeSchedule()
    return shares * schedule.rate * (price * (1.0 - price)) ** schedule.exponent


def approximate_maker_rebate(
    shares: float, price: float, schedule: FeeSchedule | None = None
) -> float:
    """Rebate a filled maker order earns, assuming a uniform market.

    NOT the exact payout. The pool is `rebate_rate` x total taker fees in the
    market and is shared out by the same fee curve, so the pool size and the
    denominator cancel and a maker's share reduces to this. That algebra holds
    only if every fill in the market carries the same rate -- true per market as
    far as sampled, but not verified against actual payouts. Treat as an upper
    bound on a small quantity rather than a number to underwrite risk with.
    """
    schedule = schedule or FeeSchedule()
    return schedule.rebate_rate * fee_equivalent(shares, price, schedule)


def no_trade_half_width(price: float, schedule: FeeSchedule | None = None) -> float:
    """Edge per share a taker must have before crossing is worth it.

    This is what protects a resting quote: an opposing taker needs at least this
    much perceived edge to hit you, so it is the natural floor on how tight
    quoting can be before the maker is simply picked off by better information.
    """
    return taker_fee(1.0, price, schedule)
