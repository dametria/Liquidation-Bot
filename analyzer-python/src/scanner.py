#!/usr/bin/env python3
"""
Aave V3 Liquidation Opportunity Scanner
- Computes health factors
- Filters HF < 1.0 and debt above dust threshold
- Estimates profit after flash-loan fee + swap slippage + gas
- Outputs JSON opportunities for the Node executor
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import List

from web3 import AsyncWeb3, Web3
from web3.providers import AsyncHTTPProvider

RPC_URL = os.getenv("RPC_URL", "https://arb1.arbitrum.io/rpc")
AAVE_POOL = Web3.to_checksum_address(
    os.getenv("AAVE_POOL", "0x794a61358D6845594F94dc1DB02A252b5b4814aD")
)
AAVE_ORACLE = Web3.to_checksum_address(
    os.getenv("AAVE_ORACLE", "0xb56c2F0B653B2e0b10C9b928C8580Ac5Df78C934")
)
MIN_DEBT_USD = Decimal(os.getenv("MIN_DEBT_USD", "50"))
MIN_PROFIT_USD = Decimal(os.getenv("MIN_PROFIT_USD", "5"))
CLOSE_FACTOR = Decimal("0.5")
FLASH_LOAN_FEE_BPS = Decimal("5")
GAS_PRICE_GWEI = Decimal(os.getenv("GAS_PRICE_GWEI", "0.1"))
EST_GAS_UNITS = Decimal("450000")
ETH_PRICE_USD = Decimal(os.getenv("ETH_PRICE_USD", "3500"))

POOL_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserAccountData",
        "outputs": [
            {"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"},
            {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"},
            {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"},
            {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"},
            {"internalType": "uint256", "name": "ltv", "type": "uint256"},
            {"internalType": "uint256", "name": "healthFactor", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "asset", "type": "address"},
            {"internalType": "address", "name": "user", "type": "address"},
        ],
        "name": "getUserReserveData",
        "outputs": [
            {"internalType": "uint256", "name": "currentATokenBalance", "type": "uint256"},
            {"internalType": "uint256", "name": "currentStableDebt", "type": "uint256"},
            {"internalType": "uint256", "name": "currentVariableDebt", "type": "uint256"},
            {"internalType": "uint256", "name": "principalStableDebt", "type": "uint256"},
            {"internalType": "uint256", "name": "scaledVariableDebt", "type": "uint256"},
            {"internalType": "uint256", "name": "stableBorrowRate", "type": "uint256"},
            {"internalType": "uint256", "name": "liquidityRate", "type": "uint256"},
            {"internalType": "uint40", "name": "stableRateLastUpdated", "type": "uint40"},
            {"internalType": "bool", "name": "usageAsCollateralEnabled", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

ORACLE_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
        "name": "getAssetPrice",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

RESERVES = {
    "WETH": Web3.to_checksum_address("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"),
    "USDC": Web3.to_checksum_address("0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
    "USDT": Web3.to_checksum_address("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"),
    "WBTC": Web3.to_checksum_address("0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"),
    "DAI": Web3.to_checksum_address("0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"),
    "ARB": Web3.to_checksum_address("0x912CE59144191C1204E64559FE8253a0e49E6548"),
}


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


class AaveScanner:
    def __init__(self, rpc_url: str = RPC_URL):
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.pool = self.w3.eth.contract(address=AAVE_POOL, abi=POOL_ABI)
        self.oracle = self.w3.eth.contract(address=AAVE_ORACLE, abi=ORACLE_ABI)

    async def get_health_factor(self, user: str) -> tuple[float, int, int]:
        data = await self.pool.functions.getUserAccountData(
            Web3.to_checksum_address(user)
        ).call()
        hf = data[5] / 1e18
        return hf, data[0], data[1]

    async def get_user_debts_and_collaterals(
        self, user: str
    ) -> tuple[dict[str, int], dict[str, int]]:
        debts: dict[str, int] = {}
        colls: dict[str, int] = {}
        user = Web3.to_checksum_address(user)
        for _, addr in RESERVES.items():
            try:
                data = await self.pool.functions.getUserReserveData(addr, user).call()
                if data[2] > 0:
                    debts[addr] = data[2]
                if data[0] > 0 and data[8]:
                    colls[addr] = data[0]
            except Exception:
                continue
        return debts, colls

    async def estimate_profit(
        self,
        debt_asset: str,
        debt_to_cover: int,
        collateral_asset: str,
        coll_amount: int,
        use_balancer: bool = True,
    ) -> float:
        try:
            debt_price = await self.oracle.functions.getAssetPrice(debt_asset).call()
            coll_price = await self.oracle.functions.getAssetPrice(collateral_asset).call()
            debt_usd = Decimal(debt_to_cover) * Decimal(debt_price) / Decimal(10**18) / Decimal(1e8)
            bonus = Decimal("1.05")
            coll_usd = (
                Decimal(coll_amount)
                * Decimal(coll_price)
                / Decimal(10**18)
                / Decimal(1e8)
                * bonus
            )
            fee_bps = Decimal("0") if use_balancer else FLASH_LOAN_FEE_BPS
            fee_usd = debt_usd * fee_bps / Decimal(10000)
            gas_usd = (EST_GAS_UNITS * GAS_PRICE_GWEI * Decimal("1e9") / Decimal(1e18)) * ETH_PRICE_USD
            slippage = coll_usd * Decimal("0.003")
            profit = coll_usd - debt_usd - fee_usd - gas_usd - slippage
            return float(profit)
        except Exception:
            return -999.0

    async def scan_users(self, users: List[str]) -> List[Opportunity]:
        opps: List[Opportunity] = []
        for user in users:
            try:
                hf, _, debt_base = await self.get_health_factor(user)
                if hf >= 1.0 or debt_base == 0:
                    continue
                debt_usd = Decimal(debt_base) / Decimal(1e8)
                if debt_usd < MIN_DEBT_USD:
                    continue
                debts, colls = await self.get_user_debts_and_collaterals(user)
                if not debts or not colls:
                    continue
                debt_asset = max(debts, key=debts.get)
                coll_asset = max(colls, key=colls.get)
                debt_amount = debts[debt_asset]
                debt_to_cover = int(Decimal(debt_amount) * CLOSE_FACTOR)
                coll_amount = colls[coll_asset] // 2
                profit = await self.estimate_profit(
                    debt_asset, debt_to_cover, coll_asset, coll_amount, use_balancer=True
                )
                if profit < float(MIN_PROFIT_USD):
                    continue
                opps.append(
                    Opportunity(
                        user=user,
                        collateral_asset=coll_asset,
                        debt_asset=debt_asset,
                        debt_to_cover=debt_to_cover,
                        estimated_collateral=coll_amount,
                        health_factor=hf,
                        estimated_profit_usd=profit,
                        use_balancer=True,
                        min_collateral_out=int(coll_amount * 0.95),
                        min_debt_out_after_swap=int(debt_to_cover * 1.001),
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
        print(json.dumps({"status": "no_users", "message": "Pipe a JSON array of user addresses"}))
        return
    scanner = AaveScanner()
    opps = await scanner.scan_users(users)
    print(json.dumps([asdict(o) for o in opps], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
