"""
Pure-math port of Aave V3 LiquidationLogic.

Source of truth: aave-v3-origin
  src/contracts/protocol/libraries/logic/LiquidationLogic.sol
  src/contracts/protocol/libraries/math/PercentageMath.sol
  src/contracts/protocol/libraries/math/MathUtils.sol

All arithmetic is integer (token wei / oracle base units). Never use floats.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

# ---------------------------------------------------------------------------
# Constants (exact values from LiquidationLogic.sol)
# ---------------------------------------------------------------------------
PERCENTAGE_FACTOR: int = 10_000  # 1e4 → 100.00%

# Default close factor applied when HF > CLOSE_FACTOR_HF_THRESHOLD
# and position is above the base-currency thresholds.
DEFAULT_LIQUIDATION_CLOSE_FACTOR: int = 5_000  # 0.5e4 → 50%

# Health-factor threshold at/below which full (100%) close factor is allowed.
CLOSE_FACTOR_HF_THRESHOLD: int = 950_000_000_000_000_000  # 0.95e18

# If both collateral and debt of the *reserve* are below this base-currency
# value, the close factor is forced to 100% regardless of HF.
# Assumes oracle prices are USD with 8 decimals.
MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD: int = 2000 * 10**8  # 2000e8

# Minimum leftover (in base currency) that must remain after a partial
# liquidation. Derived as MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD / 2.
MIN_LEFTOVER_BASE: int = MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD // 2  # 1000e8


# ---------------------------------------------------------------------------
# PercentageMath helpers (exact floor / ceil / half-up behaviour)
# ---------------------------------------------------------------------------
def percent_mul(value: int, percentage: int) -> int:
    """Classic Aave percentMul — round half-up."""
    if percentage == 0:
        return 0
    # (value * percentage + HALF) / PERCENTAGE_FACTOR
    return (value * percentage + PERCENTAGE_FACTOR // 2) // PERCENTAGE_FACTOR


def percent_mul_floor(value: int, percentage: int) -> int:
    """Floor variant used by _calculateAvailableCollateralToLiquidate."""
    if percentage == 0:
        return 0
    return (value * percentage) // PERCENTAGE_FACTOR


def percent_mul_ceil(value: int, percentage: int) -> int:
    """Ceil variant used for protocol fee."""
    if percentage == 0:
        return 0
    product = value * percentage
    return product // PERCENTAGE_FACTOR + (1 if product % PERCENTAGE_FACTOR else 0)


def percent_div(value: int, percentage: int) -> int:
    """Classic Aave percentDiv — round half-up."""
    if percentage == 0:
        raise ZeroDivisionError("percentage must be non-zero")
    return (value * PERCENTAGE_FACTOR + percentage // 2) // percentage


def percent_div_floor(value: int, percentage: int) -> int:
    """Floor variant."""
    if percentage == 0:
        raise ZeroDivisionError("percentage must be non-zero")
    return (value * PERCENTAGE_FACTOR) // percentage


def percent_div_ceil(value: int, percentage: int) -> int:
    """Ceil variant used when collateral is exhausted."""
    if percentage == 0:
        raise ZeroDivisionError("percentage must be non-zero")
    val = value * PERCENTAGE_FACTOR
    return val // percentage + (1 if val % percentage else 0)


def mul_div_ceil(a: int, b: int, c: int) -> int:
    """MathUtils.mulDivCeil — used for debt-in-base calculations."""
    if c == 0:
        raise ZeroDivisionError("divisor must be non-zero")
    product = a * b
    return product // c + (1 if product % c else 0)


# ---------------------------------------------------------------------------
# Core pure functions
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CollateralCalcResult:
    """Result of _calculateAvailableCollateralToLiquidate."""

    collateral_amount: int  # amount liquidator receives (after protocol fee)
    debt_amount_needed: int  # actual debt that will be covered
    liquidation_protocol_fee: int  # fee taken from the bonus → protocol


def calculate_available_collateral_to_liquidate(
    collateral_asset_price: int,
    collateral_asset_unit: int,
    debt_asset_price: int,
    debt_asset_unit: int,
    debt_to_cover: int,
    borrower_collateral_balance: int,
    liquidation_bonus: int,
    liquidation_protocol_fee_percentage: int,
) -> CollateralCalcResult:
    """
    Exact port of LiquidationLogic._calculateAvailableCollateralToLiquidate.

    Parameters
    ----------
    collateral_asset_price, debt_asset_price
        Oracle prices (normally 8-decimal USD).
    collateral_asset_unit, debt_asset_unit
        10 ** decimals of the respective tokens.
    debt_to_cover
        Debt amount the liquidator wants to repay (token wei).
    borrower_collateral_balance
        aToken / underlying balance of the collateral being seized.
    liquidation_bonus
        e.g. 10500 = 105 %.
    liquidation_protocol_fee_percentage
        e.g. 1000 = 10 % of the bonus.

    Returns
    -------
    CollateralCalcResult
        collateral_amount   – tokens the liquidator receives
        debt_amount_needed  – actual debt covered (may be < debt_to_cover)
        liquidation_protocol_fee – tokens taken from the bonus for the protocol
    """
    if debt_to_cover <= 0 or borrower_collateral_balance <= 0:
        return CollateralCalcResult(0, 0, 0)

    # baseCollateral = (debtPrice * debtToCover * collUnit) / (collPrice * debtUnit)
    # Solidity uses plain integer division (floor).
    base_collateral = (
        debt_asset_price * debt_to_cover * collateral_asset_unit
    ) // (collateral_asset_price * debt_asset_unit)

    max_collateral_to_liquidate = percent_mul_floor(base_collateral, liquidation_bonus)

    if max_collateral_to_liquidate > borrower_collateral_balance:
        collateral_amount = borrower_collateral_balance
        # debtAmountNeeded is rounded UP (percentDivCeil)
        debt_amount_needed = percent_div_ceil(
            (collateral_asset_price * collateral_amount * debt_asset_unit)
            // (debt_asset_price * collateral_asset_unit),
            liquidation_bonus,
        )
    else:
        collateral_amount = max_collateral_to_liquidate
        debt_amount_needed = debt_to_cover

    liquidation_protocol_fee = 0
    if liquidation_protocol_fee_percentage != 0 and collateral_amount > 0:
        # bonus portion of the collateral
        bonus_collateral = collateral_amount - percent_div_floor(
            collateral_amount, liquidation_bonus
        )
        liquidation_protocol_fee = percent_mul_ceil(
            bonus_collateral, liquidation_protocol_fee_percentage
        )
        # Safety: never let fee exceed collateral
        if liquidation_protocol_fee > collateral_amount:
            liquidation_protocol_fee = collateral_amount
        collateral_amount -= liquidation_protocol_fee

    return CollateralCalcResult(
        collateral_amount=collateral_amount,
        debt_amount_needed=debt_amount_needed,
        liquidation_protocol_fee=liquidation_protocol_fee,
    )


def compute_max_liquidatable_debt(
    borrower_reserve_debt: int,
    borrower_reserve_debt_in_base: int,
    borrower_reserve_collateral_in_base: int,
    total_debt_in_base_currency: int,
    health_factor: int,
    debt_asset_price: int,
    debt_asset_unit: int,
) -> int:
    """
    Port of the close-factor logic inside executeLiquidationCall.

    By default the whole reserve debt is liquidatable.
    The 50 % close factor is applied only when:
      - both collateral and debt of *this reserve* are ≥ MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD
      - health factor > CLOSE_FACTOR_HF_THRESHOLD
    and even then the limit is 50 % of the *user's total debt*, not of the reserve.
    """
    if borrower_reserve_debt <= 0:
        return 0

    max_liquidatable_debt = borrower_reserve_debt  # default = 100 %

    if (
        borrower_reserve_collateral_in_base >= MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD
        and borrower_reserve_debt_in_base >= MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD
        and health_factor > CLOSE_FACTOR_HF_THRESHOLD
    ):
        # 50 % of the whole user debt (across all reserves)
        total_default_liquidatable_debt_in_base = percent_mul(
            total_debt_in_base_currency, DEFAULT_LIQUIDATION_CLOSE_FACTOR
        )

        if borrower_reserve_debt_in_base > total_default_liquidatable_debt_in_base:
            # convert the base-currency limit back into debt-token units (floor)
            max_liquidatable_debt = (
                total_default_liquidatable_debt_in_base * debt_asset_unit
            ) // debt_asset_price

    return max_liquidatable_debt


def would_leave_dust(
    actual_debt_to_liquidate: int,
    borrower_reserve_debt: int,
    actual_collateral_to_liquidate: int,
    liquidation_protocol_fee_amount: int,
    borrower_collateral_balance: int,
    debt_asset_price: int,
    debt_asset_unit: int,
    collateral_asset_price: int,
    collateral_asset_unit: int,
) -> bool:
    """
    Returns True if the liquidation would leave dust and therefore revert
    on-chain with MustNotLeaveDust().

    The check is only applied when *neither* debt nor collateral is fully cleared.
    """
    if actual_debt_to_liquidate <= 0:
        return False

    debt_fully_cleared = actual_debt_to_liquidate >= borrower_reserve_debt
    coll_fully_cleared = (
        actual_collateral_to_liquidate + liquidation_protocol_fee_amount
        >= borrower_collateral_balance
    )

    if debt_fully_cleared or coll_fully_cleared:
        return False

    remaining_debt_base = mul_div_ceil(
        borrower_reserve_debt - actual_debt_to_liquidate,
        debt_asset_price,
        debt_asset_unit,
    )
    remaining_collateral_base = (
        (
            borrower_collateral_balance
            - actual_collateral_to_liquidate
            - liquidation_protocol_fee_amount
        )
        * collateral_asset_price
    ) // collateral_asset_unit

    return (
        remaining_debt_base < MIN_LEFTOVER_BASE
        or remaining_collateral_base < MIN_LEFTOVER_BASE
    )


# ---------------------------------------------------------------------------
# High-level convenience wrapper for scanners
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LiquidationParams:
    """All inputs needed to size a liquidation for one (collateral, debt) pair."""

    health_factor: int
    total_debt_in_base: int  # whole-user debt in base currency
    borrower_reserve_debt: int  # debt of this specific reserve (token wei)
    borrower_collateral_balance: int  # aToken balance of this collateral
    debt_asset_price: int
    debt_asset_unit: int
    collateral_asset_price: int
    collateral_asset_unit: int
    liquidation_bonus: int  # e.g. 10500
    liquidation_protocol_fee_pct: int  # e.g. 1000 = 10 %
    requested_debt_to_cover: int  # what the bot wants to repay


def compute_liquidation_amounts(
    p: LiquidationParams,
) -> Tuple[int, int, int, bool]:
    """
    Full pure pipeline that a scanner should call:

    1. Apply close-factor → max debt
    2. Cap requested amount
    3. Calculate collateral + protocol fee
    4. Check dust / leftover rule

    Returns
    -------
    (actual_collateral, actual_debt, protocol_fee, is_valid)

    is_valid == False means the transaction would revert on-chain
    (zero amounts or MustNotLeaveDust).
    """
    if (
        p.requested_debt_to_cover <= 0
        or p.borrower_reserve_debt <= 0
        or p.borrower_collateral_balance <= 0
        or p.health_factor >= 10**18  # still healthy
    ):
        return 0, 0, 0, False

    # 1. close-factor limit
    borrower_debt_base = mul_div_ceil(
        p.borrower_reserve_debt, p.debt_asset_price, p.debt_asset_unit
    )
    borrower_coll_base = (
        p.borrower_collateral_balance * p.collateral_asset_price
    ) // p.collateral_asset_unit

    max_debt = compute_max_liquidatable_debt(
        borrower_reserve_debt=p.borrower_reserve_debt,
        borrower_reserve_debt_in_base=borrower_debt_base,
        borrower_reserve_collateral_in_base=borrower_coll_base,
        total_debt_in_base_currency=p.total_debt_in_base,
        health_factor=p.health_factor,
        debt_asset_price=p.debt_asset_price,
        debt_asset_unit=p.debt_asset_unit,
    )

    actual_debt = min(p.requested_debt_to_cover, max_debt)
    if actual_debt <= 0:
        return 0, 0, 0, False

    # 2. collateral calculation
    calc = calculate_available_collateral_to_liquidate(
        collateral_asset_price=p.collateral_asset_price,
        collateral_asset_unit=p.collateral_asset_unit,
        debt_asset_price=p.debt_asset_price,
        debt_asset_unit=p.debt_asset_unit,
        debt_to_cover=actual_debt,
        borrower_collateral_balance=p.borrower_collateral_balance,
        liquidation_bonus=p.liquidation_bonus,
        liquidation_protocol_fee_percentage=p.liquidation_protocol_fee_pct,
    )

    # 3. dust check
    leaves_dust = would_leave_dust(
        actual_debt_to_liquidate=calc.debt_amount_needed,
        borrower_reserve_debt=p.borrower_reserve_debt,
        actual_collateral_to_liquidate=calc.collateral_amount,
        liquidation_protocol_fee_amount=calc.liquidation_protocol_fee,
        borrower_collateral_balance=p.borrower_collateral_balance,
        debt_asset_price=p.debt_asset_price,
        debt_asset_unit=p.debt_asset_unit,
        collateral_asset_price=p.collateral_asset_price,
        collateral_asset_unit=p.collateral_asset_unit,
    )

    is_valid = (
        calc.debt_amount_needed > 0
        and calc.collateral_amount > 0
        and not leaves_dust
    )

    return (
        calc.collateral_amount,
        calc.debt_amount_needed,
        calc.liquidation_protocol_fee,
        is_valid,
    )
