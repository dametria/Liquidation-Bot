# aave-v3-liquidation-math

Pure-Python port of the **pure calculation functions** from Aave V3 `LiquidationLogic`
(close-factor logic, dust / leftover prevention, and `_calculateAvailableCollateralToLiquidate`).

Faithful to the current `aave-v3-origin` codebase (post-v3.3 / Umbrella).

## Why this exists

Liquidation bots that approximate close-factor and dust rules waste gas on reverts
or miss profitable opportunities. This library gives you the exact on-chain math
so your scanner can size liquidations correctly **before** you simulate or broadcast.

## Install

```bash
# from the package root
pip install -e ".[dev]"
```

Or simply copy `math.py` into your scanner.

## Quick usage

```python
from aave_v3_liquidation_math import LiquidationParams, compute_liquidation_amounts

params = LiquidationParams(
    health_factor=980_000_000_000_000_000,          # 0.98e18
    total_debt_in_base=20_000 * 10**8,             # $20k (8-dec USD)
    borrower_reserve_debt=15_000 * 10**6,          # 15k USDC (6-dec)
    borrower_collateral_balance=10 * 10**18,       # 10 WETH
    debt_asset_price=10**8,                        # $1
    debt_asset_unit=10**6,
    collateral_asset_price=3000 * 10**8,           # $3000
    collateral_asset_unit=10**18,
    liquidation_bonus=10_500,                      # 105 %
    liquidation_protocol_fee_pct=1_000,            # 10 % of bonus
    requested_debt_to_cover=15_000 * 10**6,
)

collateral_out, debt_in, protocol_fee, is_valid = compute_liquidation_amounts(params)

if not is_valid:
    # would revert on-chain (dust or zero amounts)
    ...
else:
    # size your flash-loan + liquidationCall with these numbers
    ...
```

## What is ported

| Solidity | Python |
|----------|--------|
| `DEFAULT_LIQUIDATION_CLOSE_FACTOR` | same |
| `CLOSE_FACTOR_HF_THRESHOLD` | same |
| `MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD` | same (2000e8) |
| `MIN_LEFTOVER_BASE` | same (1000e8) |
| `PercentageMath.percentMul / Floor / Ceil` | exact |
| `PercentageMath.percentDiv / Floor / Ceil` | exact |
| `MathUtils.mulDivCeil` | exact |
| close-factor branch inside `executeLiquidationCall` | `compute_max_liquidatable_debt` |
| dust check (`MustNotLeaveDust`) | `would_leave_dust` |
| `_calculateAvailableCollateralToLiquidate` | `calculate_available_collateral_to_liquidate` |

## Production notes

- All arithmetic is integer. Never pass floats.
- Prices must be the exact oracle prices Aave uses (normally Chainlink 8-decimal USD).
- After sizing with this library, still run a full `callStatic` / Foundry-fork simulation
  of the real `liquidationCall` + swap path before broadcasting.
- The close-factor uses the **user’s total debt**, not just the single reserve debt.
  That is intentional and matches the on-chain code.

## Tests

```bash
pytest -q
```

## License

MIT
