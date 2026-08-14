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
