#!/usr/bin/env python3
"""
Aave V3 Liquidation Opportunity Scanner (production)

Qualification metrics:
  - HF < 1.0
  - Debt USD >= MIN_DEBT_USD
  - Close factor:
      * 100% if HF < 0.95 OR totalCollateralBase < $2k OR totalDebtBase < $2k
      * else 50%
  - Per-asset liquidationBonus from reserve config (eMode override when applicable)
  - Net bonus after Liquidation Protocol Fee (LPF)
  - Estimated profit >= MIN_PROFIT_USD after flash fee, gas, slippage
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from web3 import AsyncWeb3, Web3
from web3.providers import AsyncHTTPProvider

# ─── Env ───────────────────────────────────────────────────
RPC_URL = os.getenv("RPC_URL", "https://arb1.arbitrum.io/rpc")
AAVE_POOL = Web3.to_checksum_address(
    os.getenv("AAVE_POOL", "0x794a61358D6845594F94dc1DB02A252b5b4814aD")
)
AAVE_ORACLE = Web3.to_checksum_address(
    os.getenv("AAVE_ORACLE", "0xb56c2F0B653B2e0b10C9b928C8580Ac5Df78C934")
)
MIN_DEBT_USD = Decimal(os.getenv("MIN_DEBT_USD", "50"))
MIN_PROFIT_USD = Decimal(os.getenv("MIN_PROFIT_USD", "3"))
GAS_PRICE_GWEI = Decimal(os.getenv("GAS_PRICE_GWEI", "0.1"))
EST_GAS_UNITS = Decimal(os.getenv("EST_GAS_UNITS", "450000"))
ETH_PRICE_USD = Decimal(os.getenv("ETH_PRICE_USD", "3500"))
SLIPPAGE_BPS = Decimal(os.getenv("SLIPPAGE_BPS", "30"))  # 0.30%
FLASH_LOAN_FEE_BPS = Decimal(os.getenv("FLASH_LOAN_FEE_BPS", "5"))  # Aave; Balancer=0

# Aave V3 constants (LiquidationLogic.sol)
CLOSE_FACTOR_HF_THRESHOLD = Decimal("0.95")
DEFAULT_CLOSE_FACTOR = Decimal("0.5")
MAX_CLOSE_FACTOR = Decimal("1.0")
# $2000 in base currency (8 decimals)
MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD = Decimal("2000") * Decimal(10**8)

# ReserveConfiguration bit layout (Aave V3)
LIQ_BONUS_START = 32
LIQ_BONUS_MASK = 0xFFFF
LIQ_PROTOCOL_FEE_START = 152
LIQ_PROTOCOL_FEE_MASK = 0xFFFF
DECIMALS_START = 48
DECIMALS_MASK = 0xFF

# ─── ABIs ────────────────────────────────────────────────────
POOL_ABI = [
    {
        "inputs": [{"name": "user", "type": "address"}],
        "name": "getUserAccountData",
        "outputs": [
            {"name": "totalCollateralBase", "type": "uint256"},
            {"name": "totalDebtBase", "type": "uint256"},
            {"name": "availableBorrowsBase", "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "name": "getUserReserveData",
        "outputs": [
            {"name": "currentATokenBalance", "type": "uint256"},
            {"name": "currentStableDebt", "type": "uint256"},
            {"name": "currentVariableDebt", "type": "uint256"},
            {"name": "principalStableDebt", "type": "uint256"},
            {"name": "scaledVariableDebt", "type": "uint256"},
            {"name": "stableBorrowRate", "type": "uint256"},
            {"name": "liquidityRate", "type": "uint256"},
            {"name": "stableRateLastUpdated", "type": "uint40"},
            {"name": "usageAsCollateralEnabled", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "getConfiguration",
        "outputs": [{"components": [{"name": "data", "type": "uint256"}], "name": "", "type": "tuple"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "user", "type": "address"}],
        "name": "getUserEMode",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "id", "type": "uint8"}],
        "name": "getEModeCategoryData",
        "outputs": [
            {
                "components": [
                    {"name": "ltv", "type": "uint16"},
                    {"name": "liquidationThreshold", "type": "uint16"},
                    {"name": "liquidationBonus", "type": "uint16"},
                    {"name": "priceSource", "type": "address"},
                    {"name": "label", "type": "string"},
                ],
                "name": "", "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

ORACLE_ABI = [
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "getAssetPrice",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

# Arbitrum Aave V3 majors
RESERVES: Dict[str, str] = {
    "WETH": Web3.to_checksum_address("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"),
    "USDC": Web3.to_checksum_address("0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
    "USDT": Web3.to_checksum_address("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"),
    "WBTC": Web3.to_checksum_address("0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"),
    "DAI": Web3.to_checksum_address("0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"),
    "ARB": Web3.to_checksum_address("0x912CE59144191C1204E64559FE8253a0e49E6548"),
    "wstETH": Web3.to_checksum_address("0x5979D7b546E38E414F7E9822514be443A4800529"),
    "weETH": Web3.to_checksum_address("0x35751007a407ca6FEFfE80b3cB397736D2cf4dbe"),
    "LINK": Web3.to_checksum_address("0xf97f4df75117a78c1A5a0DBb814Af92458539FB4"),
}


@dataclass
class ReserveParams:
    liquidation_bonus_bps: int  # e.g. 10500 = 105%
    liquidation_protocol_fee_bps: int  # e.g. 1000 = 10% of the bonus
    decimals: int


@dataclass
class Opportunity:
    user: str
    collateral_asset: str
    debt_asset: str
    debt_to_cover: int
    estimated_collateral: int
    health_factor: float
    estimated_profit_usd: float
    use_balancer: bool = True
    pool_fee: int = 3000
    min_collateral_out: int = 0
    min_debt_out_after_swap: int = 0
    close_factor: float = 0.5
    net_bonus_factor: float = 1.05
    liquidation_bonus_bps: int = 10500
    lpf_bps: int = 1000


def decode_config(data: int) -> ReserveParams:
    bonus = (data >> LIQ_BONUS_START) & LIQ_BONUS_MASK
    lpf = (data >> LIQ_PROTOCOL_FEE_START) & LIQ_PROTOCOL_FEE_MASK
    decimals = (data >> DECIMALS_START) & DECIMALS_MASK
    if bonus < 10000:
        # Mis-decode or disabled — fall back to 5%
        bonus = 10500
    return ReserveParams(
        liquidation_bonus_bps=int(bonus),
        liquidation_protocol_fee_bps=int(lpf),
        decimals=int(decimals) if decimals else 18,
    )


def net_bonus_factor(bonus_bps: int, lpf_bps: int) -> Decimal:
    """
    Gross bonus factor is bonus_bps/10000 (e.g. 1.05).
    LPF is a share of the *bonus portion* (bonus - 100%), not of full collateral.
    Net factor for liquidator:
        1 + (bonus_factor - 1) * (1 - lpf)
    """
    gross = Decimal(bonus_bps) / Decimal(10000)
    lpf = Decimal(lpf_bps) / Decimal(10000)
    bonus_portion = gross - Decimal(1)
    if bonus_portion < 0:
        bonus_portion = Decimal(0)
    net = Decimal(1) + bonus_portion * (Decimal(1) - lpf)
    return net


def choose_close_factor(
    hf: Decimal, total_collateral_base: int, total_debt_base: int
) -> Decimal:
    if hf < CLOSE_FACTOR_HF_THRESHOLD:
        return MAX_CLOSE_FACTOR
    if Decimal(total_collateral_base) < MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD:
        return MAX_CLOSE_FACTOR
    if Decimal(total_debt_base) < MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD:
        return MAX_CLOSE_FACTOR
    return DEFAULT_CLOSE_FACTOR


class AaveScanner:
    def __init__(self, rpc_url: str = RPC_URL):
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.pool = self.w3.eth.contract(address=AAVE_POOL, abi=POOL_ABI)
        self.oracle = self.w3.eth.contract(address=AAVE_ORACLE, abi=ORACLE_ABI)
        self._config_cache: Dict[str, ReserveParams] = {}
        self._emode_bonus_cache: Dict[int, int] = {}

    async def get_reserve_params(self, asset: str) -> ReserveParams:
        asset = Web3.to_checksum_address(asset)
        if asset in self._config_cache:
            return self._config_cache[asset]
        raw = await self.pool.functions.getConfiguration(asset).call()
        # raw may be (data,) tuple or int depending on web3 version
        data = raw[0] if isinstance(raw, (tuple, list)) else int(raw)
        params = decode_config(int(data))
        self._config_cache[asset] = params
        return params

    async def get_emode_bonus(self, category_id: int) -> Optional[int]:
        if category_id <= 0:
            return None
        if category_id in self._emode_bonus_cache:
            return self._emode_bonus_cache[category_id]
        try:
            data = await self.pool.functions.getEModeCategoryData(category_id).call()
            # (ltv, liqThreshold, liqBonus, priceSource, label)
            bonus = int(data[2])
            if bonus < 10000:
                bonus = 10500
            self._emode_bonus_cache[category_id] = bonus
            return bonus
        except Exception:
            return None

    async def get_health_factor(
        self, user: str
    ) -> Tuple[Decimal, int, int]:
        data = await self.pool.functions.getUserAccountData(
            Web3.to_checksum_address(user)
        ).call()
        hf = Decimal(data[5]) / Decimal(10**18)
        return hf, int(data[0]), int(data[1])

    async def get_user_debts_and_collaterals(
        self, user: str
    ) -> Tuple[Dict[str, int], Dict[str, int]]:
        debts: Dict[str, int] = {}
        colls: Dict[str, int] = {}
        user = Web3.to_checksum_address(user)
        for _, addr in RESERVES.items():
            try:
                data = await self.pool.functions.getUserReserveData(addr, user).call()
                variable_debt = int(data[2])
                stable_debt = int(data[1])
                total_debt = variable_debt + stable_debt
                if total_debt > 0:
                    debts[addr] = total_debt
                a_bal = int(data[0])
                if a_bal > 0 and bool(data[8]):
                    colls[addr] = a_bal
            except Exception:
                continue
        return debts, colls

    async def estimate_profit_usd(
        self,
        debt_asset: str,
        debt_to_cover: int,
        collateral_asset: str,
        coll_amount: int,
        net_bonus: Decimal,
        use_balancer: bool,
        debt_decimals: int,
        coll_decimals: int,
    ) -> float:
        try:
            debt_price = await self.oracle.functions.getAssetPrice(
                Web3.to_checksum_address(debt_asset)
            ).call()
            coll_price = await self.oracle.functions.getAssetPrice(
                Web3.to_checksum_address(collateral_asset)
            ).call()
            # Oracle prices are USD with 8 decimals
            debt_usd = (
                Decimal(debt_to_cover)
                * Decimal(debt_price)
                / Decimal(10**debt_decimals)
                / Decimal(10**8)
            )
            # coll_amount already includes protocol-applied bonus on-chain;
            # for *estimate* we value the tokens we expect to receive.
            coll_usd = (
                Decimal(coll_amount)
                * Decimal(coll_price)
                / Decimal(10**coll_decimals)
                / Decimal(10**8)
            )
            fee_bps = Decimal(0) if use_balancer else FLASH_LOAN_FEE_BPS
            fee_usd = debt_usd * fee_bps / Decimal(10000)
            gas_usd = (
                EST_GAS_UNITS * GAS_PRICE_GWEI * Decimal("1e9") / Decimal("1e18")
            ) * ETH_PRICE_USD
            slippage = coll_usd * SLIPPAGE_BPS / Decimal(10000)
            profit = coll_usd - debt_usd - fee_usd - gas_usd - slippage
            return float(profit)
        except Exception as e:
            print(f"[warn] profit estimate failed: {e}", file=sys.stderr)
            return -999.0

    async def scan_users(self, users: List[str]) -> List[Opportunity]:
        opps: List[Opportunity] = []
        for user in users:
            try:
                hf, coll_base, debt_base = await self.get_health_factor(user)
                if hf >= Decimal(1) or debt_base == 0:
                    continue

                debt_usd = Decimal(debt_base) / Decimal(10**8)
                if debt_usd < MIN_DEBT_USD:
                    continue

                close_factor = choose_close_factor(hf, coll_base, debt_base)

                debts, colls = await self.get_user_debts_and_collaterals(user)
                if not debts or not colls:
                    continue

                # Prefer largest debt; among collaterals prefer highest *net* bonus
                debt_asset = max(debts, key=debts.get)
                debt_amount = debts[debt_asset]
                debt_to_cover = int(Decimal(debt_amount) * close_factor)
                if debt_to_cover <= 0:
                    continue

                try:
                    emode_id = int(
                        await self.pool.functions.getUserEMode(
                            Web3.to_checksum_address(user)
                        ).call()
                    )
                except Exception:
                    emode_id = 0
                emode_bonus = await self.get_emode_bonus(emode_id)

                best_coll = None
                best_net = Decimal(0)
                best_params: Optional[ReserveParams] = None
                best_bonus_bps = 10500
                best_lpf = 1000

                for coll_asset, coll_bal in colls.items():
                    params = await self.get_reserve_params(coll_asset)
                    bonus_bps = params.liquidation_bonus_bps
                    # eMode bonus overrides when user is in eMode and category defines one
                    if emode_bonus is not None:
                        bonus_bps = emode_bonus
                    lpf_bps = params.liquidation_protocol_fee_bps
                    net = net_bonus_factor(bonus_bps, lpf_bps)
                    if best_coll is None or net > best_net:
                        best_coll = coll_asset
                        best_net = net
                        best_params = params
                        best_bonus_bps = bonus_bps
                        best_lpf = lpf_bps

                if best_coll is None or best_params is None:
                    continue

                debt_params = await self.get_reserve_params(debt_asset)

                # Estimate collateral received: base debt value * net_bonus, in coll units
                try:
                    debt_price = await self.oracle.functions.getAssetPrice(
                        debt_asset
                    ).call()
                    coll_price = await self.oracle.functions.getAssetPrice(
                        best_coll
                    ).call()
                    if coll_price == 0:
                        continue
                    # debt value in base (1e8 USD) then to coll wei
                    debt_value_base = (
                        Decimal(debt_to_cover)
                        * Decimal(debt_price)
                        / Decimal(10**debt_params.decimals)
                    )
                    coll_amount = int(
                        debt_value_base
                        * best_net
                        * Decimal(10**best_params.decimals)
                        / Decimal(coll_price)
                    )
                    # Cap by user balance
                    coll_amount = min(coll_amount, colls[best_coll])
                except Exception:
                    coll_amount = colls[best_coll] // max(1, int(1 / float(close_factor)))

                use_balancer = True
                profit = await self.estimate_profit_usd(
                    debt_asset,
                    debt_to_cover,
                    best_coll,
                    coll_amount,
                    best_net,
                    use_balancer,
                    debt_params.decimals,
                    best_params.decimals,
                )

                if profit < float(MIN_PROFIT_USD):
                    continue

                opps.append(
                    Opportunity(
                        user=user,
                        collateral_asset=best_coll,
                        debt_asset=debt_asset,
                        debt_to_cover=debt_to_cover,
                        estimated_collateral=coll_amount,
                        health_factor=float(hf),
                        estimated_profit_usd=profit,
                        use_balancer=use_balancer,
                        min_collateral_out=int(coll_amount * 0.95),
                        min_debt_out_after_swap=int(debt_to_cover * 1.001),
                        close_factor=float(close_factor),
                        net_bonus_factor=float(best_net),
                        liquidation_bonus_bps=best_bonus_bps,
                        lpf_bps=best_lpf,
                    )
                )
            except Exception as e:
                print(f"[warn] user {user}: {e}", file=sys.stderr)
                continue
        return opps


async def main():
    users: List[str] = []
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            users = json.loads(raw)
    if not users:
        print(
            json.dumps(
                {
                    "status": "no_users",
                    "message": "Pipe a JSON array of user addresses",
                }
            )
        )
        return

    scanner = AaveScanner()
    opps = await scanner.scan_users(users)
    print(json.dumps([asdict(o) for o in opps], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
