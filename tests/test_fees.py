"""Tests for the fee formula.

Pinned against the worked examples in Polymarket's published fee table, and
against the specific way the previously assumed `min(p, 1-p)` form was wrong.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from weatherbot.fees import (  # noqa: E402
    FeeSchedule,
    approximate_maker_rebate,
    maker_fee,
    no_trade_half_width,
    taker_fee,
)


class TestPublishedExamples:
    """100 shares at rate 0.07, from docs.polymarket.com/trading/fees."""

    crypto = FeeSchedule(rate=0.07, rebate_rate=0.20)

    def test_at_fifty_cents(self):
        assert taker_fee(100, 0.50, self.crypto) == pytest.approx(1.75)

    def test_at_ten_cents(self):
        assert taker_fee(100, 0.10, self.crypto) == pytest.approx(0.63)

    def test_is_symmetric_about_half(self):
        assert taker_fee(100, 0.10, self.crypto) == pytest.approx(
            taker_fee(100, 0.90, self.crypto)
        )


class TestAgainstTheOldWrongFormula:
    """The old `min(p, 1-p)` form overstates everywhere, worst at the middle."""

    @staticmethod
    def old(p, shares=100, rate=0.05):
        return rate * min(p, 1 - p) * shares

    def test_it_is_twice_too_high_at_the_midpoint(self):
        assert self.old(0.50) == pytest.approx(2.50)
        assert taker_fee(100, 0.50) == pytest.approx(1.25)

    def test_it_overstates_at_every_price(self):
        for p in (0.01, 0.05, 0.10, 0.25, 0.40, 0.50):
            assert taker_fee(100, p) < self.old(p)

    def test_the_error_shrinks_toward_the_tails(self):
        def ratio(p):
            return self.old(p) / taker_fee(100, p)

        assert ratio(0.50) == pytest.approx(2.0)
        assert ratio(0.50) > ratio(0.25) > ratio(0.10) > ratio(0.01)
        assert ratio(0.01) == pytest.approx(1.0, abs=0.02)

    def test_the_ratio_is_one_over_one_minus_p(self):
        for p in (0.05, 0.2, 0.45):
            assert self.old(p) / taker_fee(100, p) == pytest.approx(1 / (1 - p))


class TestWeatherDefaults:
    def test_defaults_are_the_weather_category(self):
        s = FeeSchedule()
        assert (s.rate, s.rebate_rate, s.taker_only) == (0.05, 0.25, True)

    def test_makers_pay_nothing(self):
        assert maker_fee(1000, 0.5) == 0.0

    def test_makers_pay_when_taker_only_is_false(self):
        s = FeeSchedule(taker_only=False)
        assert maker_fee(100, 0.5, s) == pytest.approx(taker_fee(100, 0.5, s))

    def test_rebate_is_a_quarter_of_the_fee_equivalent(self):
        assert approximate_maker_rebate(100, 0.5) == pytest.approx(
            0.25 * taker_fee(100, 0.5)
        )


class TestFromGamma:
    def test_reads_the_schedule(self):
        s = FeeSchedule.from_gamma(
            {"feeSchedule": {"rate": 0.04, "exponent": 1, "takerOnly": True,
                             "rebateRate": 0.25}}
        )
        assert s.rate == 0.04 and s.rebate_rate == 0.25

    def test_absent_schedule_falls_back_to_weather_defaults(self):
        assert FeeSchedule.from_gamma({}).rate == 0.05

    def test_the_inert_base_fee_fields_are_ignored(self):
        # These return 1000 for every market in every category.
        s = FeeSchedule.from_gamma({"makerBaseFee": 1000, "takerBaseFee": 1000})
        assert s.rate == 0.05, "base fee fields must not influence the rate"


class TestNoTradeBand:
    def test_is_the_per_share_taker_fee(self):
        assert no_trade_half_width(0.5) == pytest.approx(taker_fee(1.0, 0.5))

    def test_is_widest_at_the_midpoint(self):
        assert no_trade_half_width(0.5) > no_trade_half_width(0.2)
        assert no_trade_half_width(0.2) > no_trade_half_width(0.05)

    def test_tail_quotes_are_cheap_to_hit(self):
        # 0.24c at p=0.05 versus 1.25c at the midpoint: tails are ~5x cheaper
        # to cross, so a resting tail quote is far less protected.
        assert no_trade_half_width(0.05) * 100 == pytest.approx(0.2375)
        assert no_trade_half_width(0.50) * 100 == pytest.approx(1.25)


class TestValidation:
    def test_rejects_an_impossible_price(self):
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            taker_fee(100, 1.5)
