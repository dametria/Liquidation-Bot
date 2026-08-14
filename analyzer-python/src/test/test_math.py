"""
Unit tests for aave_v3_liquidation_math.

These tests encode the exact semantics of Aave V3 LiquidationLogic
(post-v3.3). They are intentionally written against the pure-math layer
so they can run without an RPC or Foundry fork.
"""

from __future__ import annotations

import pytest

from aave_v3_liquidation_math import (
    PERCENTAGE_FACTOR,
    DEFAULT_LIQUIDATION_CLOSE_FACTOR,
    CLOSE_FACTOR_HF_THRESHOLD,
    MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD,
    MIN_LEFTOVER_BASE,
    percent_mul,
    percent_mul_floor,
    percent_mul_ceil,
    percent_div,
    percent_div_floor,
    percent_div_ceil,
    mul_div_ceil,
    calculate_available_collateral_to_liquidate,
    compute_max_liquidatable_debt,
    would_leave_dust,
    LiquidationParams,
    compute_liquidation_amounts,
)


# ---------------------------------------------------------------------------
# PercentageMath
# ---------------------------------------------------------------------------
class TestPercentageMath:
    def test_percent_mul_half_up(self):
        # 100 * 50% = 50
        assert percent_mul(100, 5_000) == 50
        # classic half-up: 1 * 50% with HALF → rounds to 1
        assert percent_mul(1, 5_000) == 1
        # zero percentage
        assert percent_mul(1000, 0) == 0

    def test_percent_mul_floor(self):
        assert percent_mul_floor(100, 5_000) == 50
        # floor: 1 * 50% = 0
        assert percent_mul_floor(1, 5_000) == 0
        assert percent_mul_floor(199, 5_000) == 99

    def test_percent_mul_ceil(self):
        assert percent_mul_ceil(100, 5_000) == 50
        # ceil: 1 * 50% = 1
        assert percent_mul_ceil(1, 5_000) == 1
        assert percent_mul_ceil(101, 5_000) == 51

    def test_percent_div_half_up(self):
        assert percent_div(50, 5_000) == 100
        assert percent_div(1, 5_000) == 2  # half-up

    def test_percent_div_floor(self):
        assert percent_div_floor(50, 5_000) == 100
        assert percent_div_floor(1, 5_000) == 2  # still 2 because of the *1e4

    def test_percent_div_ceil(self):
        assert percent_div_ceil(50, 5_000) == 100
        # force a remainder
        assert percent_div_ceil(1, 3) == 3334  # ceil(10000/3) = 3334

    def test_zero_percentage_raises(self):
        with pytest.raises(ZeroDivisionError):
            percent_div(100, 0)
        with pytest.raises(ZeroDivisionError):
            percent_div_floor(100, 0)
        with pytest.raises(ZeroDivisionError):
            percent_div_ceil(100, 0)


class TestMulDivCeil:
    def test_exact(self):
        assert mul_div_ceil(10, 20, 5) == 40

    def test_remainder(self):
        assert mul_div_ceil(10, 20, 6) == 34  # 200/6 = 33.333 → 34

    def test_zero_divisor(self):
        with pytest.raises(ZeroDivisionError):
            mul_div_ceil(1, 1, 0)


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------
class TestConstants:
    def test_values(self):
        assert PERCENTAGE_FACTOR == 10_000
        assert DEFAULT_LIQUIDATION_CLOSE_FACTOR == 5_000
        assert CLOSE_FACTOR_HF_THRESHOLD == 950_000_000_000_000_000
        assert MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD == 2000 * 10**8
        assert MIN_LEFTOVER_BASE == 1000 * 10**8
        assert MIN_LEFTOVER_BASE == MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD // 2


# ---------------------------------------------------------------------------
# _calculateAvailableCollateralToLiquidate
# ---------------------------------------------------------------------------
class TestCalculateAvailableCollateral:
    """
    Realistic numbers (USDC debt, WETH collateral).

    USDC  = 6 decimals, price ≈ $1 → 1e8
    WETH  = 18 decimals, price ≈ $3000 → 3000e8
    liquidation_bonus = 10500 (5 %)
    protocol_fee      = 1000  (10 % of bonus)
    """

    USDC_UNIT = 10**6
    WETH_UNIT = 10**18
    USDC_PRICE = 10**8          # $1
    WETH_PRICE = 3000 * 10**8   # $3000
    BONUS = 10_500              # 105 %
    FEE_PCT = 1_000             # 10 %

    def test_normal_partial_liquidation(self):
        # Cover 1000 USDC of debt
        debt_to_cover = 1000 * self.USDC_UNIT
        # Plenty of collateral
        coll_balance = 10 * self.WETH_UNIT

        result = calculate_available_collateral_to_liquidate(
            collateral_asset_price=self.WETH_PRICE,
            collateral_asset_unit=self.WETH_UNIT,
            debt_asset_price=self.USDC_PRICE,
            debt_asset_unit=self.USDC_UNIT,
            debt_to_cover=debt_to_cover,
            borrower_collateral_balance=coll_balance,
            liquidation_bonus=self.BONUS,
            liquidation_protocol_fee_percentage=self.FEE_PCT,
        )

        # baseCollateral ≈ (1e8 * 1000e6 * 1e18) / (3000e8 * 1e6) = 1e18 / 3 ≈ 0.333... WETH
        # maxCollateral = base * 1.05 (floor)
        # protocol fee = 10 % of the 5 % bonus
        assert result.debt_amount_needed == debt_to_cover
        assert result.collateral_amount > 0
        assert result.liquidation_protocol_fee > 0
        # liquidator receives less than the full bonus amount
        assert result.collateral_amount < percent_mul_floor(
            (self.USDC_PRICE * debt_to_cover * self.WETH_UNIT)
            // (self.WETH_PRICE * self.USDC_UNIT),
            self.BONUS,
        )

    def test_collateral_exhausted(self):
        # Very large debt relative to collateral → collateral is the limiting factor
        debt_to_cover = 1_000_000 * self.USDC_UNIT  # $1M
        coll_balance = 1 * self.WETH_UNIT            # 1 WETH ≈ $3000

        result = calculate_available_collateral_to_liquidate(
            collateral_asset_price=self.WETH_PRICE,
            collateral_asset_unit=self.WETH_UNIT,
            debt_asset_price=self.USDC_PRICE,
            debt_asset_unit=self.USDC_UNIT,
            debt_to_cover=debt_to_cover,
            borrower_collateral_balance=coll_balance,
            liquidation_bonus=self.BONUS,
            liquidation_protocol_fee_percentage=self.FEE_PCT,
        )

        # When collateral is exhausted, debt_amount_needed is recalculated
        # with percentDivCeil and will be < requested debt_to_cover
        assert result.debt_amount_needed < debt_to_cover
        assert result.debt_amount_needed > 0
        # collateral_amount + fee should equal (or be extremely close to) balance
        assert (
            result.collateral_amount + result.liquidation_protocol_fee
            == coll_balance
        )

    def test_zero_protocol_fee(self):
        debt_to_cover = 1000 * self.USDC_UNIT
        coll_balance = 10 * self.WETH_UNIT

        result = calculate_available_collateral_to_liquidate(
            collateral_asset_price=self.WETH_PRICE,
            collateral_asset_unit=self.WETH_UNIT,
            debt_asset_price=self.USDC_PRICE,
            debt_asset_unit=self.USDC_UNIT,
            debt_to_cover=debt_to_cover,
            borrower_collateral_balance=coll_balance,
            liquidation_bonus=self.BONUS,
            liquidation_protocol_fee_percentage=0,
        )
        assert result.liquidation_protocol_fee == 0
        assert result.debt_amount_needed == debt_to_cover

    def test_zero_inputs(self):
        result = calculate_available_collateral_to_liquidate(
            collateral_asset_price=self.WETH_PRICE,
            collateral_asset_unit=self.WETH_UNIT,
            debt_asset_price=self.USDC_PRICE,
            debt_asset_unit=self.USDC_UNIT,
            debt_to_cover=0,
            borrower_collateral_balance=10 * self.WETH_UNIT,
            liquidation_bonus=self.BONUS,
            liquidation_protocol_fee_percentage=self.FEE_PCT,
        )
        assert result.collateral_amount == 0
        assert result.debt_amount_needed == 0
        assert result.liquidation_protocol_fee == 0


# ---------------------------------------------------------------------------
# Close-factor logic
# ---------------------------------------------------------------------------
class TestCloseFactor:
    def test_full_close_when_hf_low(self):
        # HF ≤ 0.95 → always 100 % of the reserve debt
        max_debt = compute_max_liquidatable_debt(
            borrower_reserve_debt=10_000 * 10**6,
            borrower_reserve_debt_in_base=10_000 * 10**8,
            borrower_reserve_collateral_in_base=20_000 * 10**8,
            total_debt_in_base_currency=10_000 * 10**8,
            health_factor=CLOSE_FACTOR_HF_THRESHOLD,  # == 0.95
            debt_asset_price=10**8,
            debt_asset_unit=10**6,
        )
        assert max_debt == 10_000 * 10**6

    def test_full_close_when_position_small(self):
        # Position below MIN_BASE threshold → 100 % even if HF high
        max_debt = compute_max_liquidatable_debt(
            borrower_reserve_debt=500 * 10**6,          # $500
            borrower_reserve_debt_in_base=500 * 10**8,
            borrower_reserve_collateral_in_base=800 * 10**8,
            total_debt_in_base_currency=500 * 10**8,
            health_factor=990_000_000_000_000_000,         # 0.99
            debt_asset_price=10**8,
            debt_asset_unit=10**6,
        )
        assert max_debt == 500 * 10**6

    def test_fifty_percent_close_factor(self):
        # Large position, HF > 0.95 → limited to 50 % of *total* user debt
        total_debt_base = 20_000 * 10**8          # $20k total
        reserve_debt = 15_000 * 10**6             # this reserve is $15k
        reserve_debt_base = 15_000 * 10**8

        max_debt = compute_max_liquidatable_debt(
            borrower_reserve_debt=reserve_debt,
            borrower_reserve_debt_in_base=reserve_debt_base,
            borrower_reserve_collateral_in_base=30_000 * 10**8,
            total_debt_in_base_currency=total_debt_base,
            health_factor=980_000_000_000_000_000,   # 0.98
            debt_asset_price=10**8,
            debt_asset_unit=10**6,
        )
        # 50 % of total = $10k → less than the $15k reserve debt
        expected = (percent_mul(total_debt_base, DEFAULT_LIQUIDATION_CLOSE_FACTOR) * 10**6) // 10**8
        assert max_debt == expected
        assert max_debt < reserve_debt

    def test_fifty_percent_does_not_limit_when_reserve_already_small(self):
        # Reserve debt already ≤ 50 % of total → no further reduction
        total_debt_base = 20_000 * 10**8
        reserve_debt = 8_000 * 10**6              # $8k < $10k
        reserve_debt_base = 8_000 * 10**8

        max_debt = compute_max_liquidatable_debt(
            borrower_reserve_debt=reserve_debt,
            borrower_reserve_debt_in_base=reserve_debt_base,
            borrower_reserve_collateral_in_base=30_000 * 10**8,
            total_debt_in_base_currency=total_debt_base,
            health_factor=980_000_000_000_000_000,
            debt_asset_price=10**8,
            debt_asset_unit=10**6,
        )
        assert max_debt == reserve_debt


# ---------------------------------------------------------------------------
# Dust / leftover check
# ---------------------------------------------------------------------------
class TestDust:
    def test_full_debt_clear_is_ok(self):
        # Clearing the entire debt → no dust check
        assert not would_leave_dust(
            actual_debt_to_liquidate=1000 * 10**6,
            borrower_reserve_debt=1000 * 10**6,
            actual_collateral_to_liquidate=1 * 10**18,
            liquidation_protocol_fee_amount=0,
            borrower_collateral_balance=2 * 10**18,
            debt_asset_price=10**8,
            debt_asset_unit=10**6,
            collateral_asset_price=3000 * 10**8,
            collateral_asset_unit=10**18,
        )

    def test_full_collateral_clear_is_ok(self):
        assert not would_leave_dust(
            actual_debt_to_liquidate=500 * 10**6,
            borrower_reserve_debt=1000 * 10**6,
            actual_collateral_to_liquidate=1 * 10**18,
            liquidation_protocol_fee_amount=0,
            borrower_collateral_balance=1 * 10**18,
            debt_asset_price=10**8,
            debt_asset_unit=10**6,
            collateral_asset_price=3000 * 10**8,
            collateral_asset_unit=10**18,
        )

    def test_leaves_dust_debt(self):
        # Leaves only $200 of debt (< $1000 MIN_LEFTOVER)
        assert would_leave_dust(
            actual_debt_to_liquidate=800 * 10**6,          # leave $200
            borrower_reserve_debt=1000 * 10**6,
            actual_collateral_to_liquidate=1 * 10**17,     # leave plenty of coll
            liquidation_protocol_fee_amount=0,
            borrower_collateral_balance=10 * 10**18,
            debt_asset_price=10**8,
            debt_asset_unit=10**6,
            collateral_asset_price=3000 * 10**8,
            collateral_asset_unit=10**18,
        )

    def test_leaves_enough_on_both_sides(self):
        # Leaves $2000 debt and plenty of collateral → ok
        assert not would_leave_dust(
            actual_debt_to_liquidate=8000 * 10**6,
            borrower_reserve_debt=10_000 * 10**6,
            actual_collateral_to_liquidate=1 * 10**18,
            liquidation_protocol_fee_amount=0,
            borrower_collateral_balance=10 * 10**18,
            debt_asset_price=10**8,
            debt_asset_unit=10**6,
            collateral_asset_price=3000 * 10**8,
            collateral_asset_unit=10**18,
        )


# ---------------------------------------------------------------------------
# End-to-end convenience wrapper
# ---------------------------------------------------------------------------
class TestComputeLiquidationAmounts:
    def _base_params(self, **overrides) -> LiquidationParams:
        defaults = dict(
            health_factor=980_000_000_000_000_000,       # 0.98
            total_debt_in_base=20_000 * 10**8,          # $20k
            borrower_reserve_debt=15_000 * 10**6,       # $15k USDC
            borrower_collateral_balance=10 * 10**18,    # 10 WETH
            debt_asset_price=10**8,
            debt_asset_unit=10**6,
            collateral_asset_price=3000 * 10**8,
            collateral_asset_unit=10**18,
            liquidation_bonus=10_500,
            liquidation_protocol_fee_pct=1_000,
            requested_debt_to_cover=15_000 * 10**6,     # ask for everything
        )
        defaults.update(overrides)
        return LiquidationParams(**defaults)

    def test_healthy_position_rejected(self):
        p = self._base_params(health_factor=1_100_000_000_000_000_000)  # 1.1
        coll, debt, fee, ok = compute_liquidation_amounts(p)
        assert not ok
        assert coll == 0 and debt == 0

    def test_close_factor_applied(self):
        p = self._base_params()
        coll, debt, fee, ok = compute_liquidation_amounts(p)
        assert ok
        # 50 % of $20k total = $10k → debt should be capped
        assert debt == 10_000 * 10**6
        assert coll > 0
        assert fee > 0

    def test_low_hf_full_close(self):
        p = self._base_params(
            health_factor=CLOSE_FACTOR_HF_THRESHOLD,  # 0.95 → full close allowed
            requested_debt_to_cover=15_000 * 10**6,
        )
        coll, debt, fee, ok = compute_liquidation_amounts(p)
        assert ok
        assert debt == 15_000 * 10**6  # full reserve debt

    def test_zero_request(self):
        p = self._base_params(requested_debt_to_cover=0)
        coll, debt, fee, ok = compute_liquidation_amounts(p)
        assert not ok


# ---------------------------------------------------------------------------
# Deterministic regression (exact expected numbers)
# ---------------------------------------------------------------------------
class TestRegression:
    """Hard-coded expected values so future refactors cannot silently drift."""

    def test_known_scenario(self):
        # Same numbers as the __main__ demo
        p = LiquidationParams(
            health_factor=980_000_000_000_000_000,
            total_debt_in_base=20_000 * 10**8,
            borrower_reserve_debt=15_000 * 10**6,
            borrower_collateral_balance=10 * 10**18,
            debt_asset_price=10**8,
            debt_asset_unit=10**6,
            collateral_asset_price=3000 * 10**8,
            collateral_asset_unit=10**18,
            liquidation_bonus=10_500,
            liquidation_protocol_fee_pct=1_000,
            requested_debt_to_cover=15_000 * 10**6,
        )
        coll, debt, fee, ok = compute_liquidation_amounts(p)
        assert ok is True
        assert debt == 10_000 * 10**6          # 50 % close-factor of $20k total
        # Exact collateral math:
        # base = (1e8 * 10000e6 * 1e18) // (3000e8 * 1e6) = 3333333333333333333
        # max  = percentMulFloor(base, 10500) = 3500000000000000000
        # bonus portion = max - percentDivFloor(max, 10500)
        # fee  = percentMulCeil(bonus, 1000)
        # coll = max - fee
        assert coll == 3_483_333_333_333_333_332
        assert fee == 16_666_666_666_666_667
