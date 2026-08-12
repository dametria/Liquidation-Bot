# Aave V3 Liquidation Bot (Balancer / Aave Flash Loans)

Capital-efficient liquidation bot that:

1. Scans Aave V3 positions for **health factor < 1**
2. Borrows the debt asset via **Balancer V2 flash loan (0 % fee)** or **Aave V3 flashLoanSimple**
3. Calls `liquidationCall`, receives collateral + liquidation bonus
4. Swaps collateral → debt asset on Uniswap V3
5. Repays the flash loan
6. Sends remaining profit to the **deployer wallet**

All steps are atomic in a single transaction. Only gas is required.

## Location

```
/home/workdir/artifacts/liquidation-bot/
```

## Layout

```
liquidation-bot/
├── contracts/                  # Foundry
│   ├── src/LiquidationBot.sol  # Core executor
│   ├── script/Deploy.s.sol
│   └── foundry.toml
├── analyzer-python/
│   └── src/scanner.py          # HF checks + profit estimate
├── executor-node/
│   └── src/index.ts            # Simulation + submission
├── shared/users.json
├── .env.example
└── README.md
```

## Quick Start

```bash
# Install
foundryup
cd executor-node && npm i && cd ..
cd analyzer-python && pip install -r requirements.txt && cd ..

# Config
cp .env.example .env
# set PRIVATE_KEY, RPC_URL, AAVE_ADDRESSES_PROVIDER, BALANCER_VAULT

# Deploy
cd contracts
forge install foundry-rs/forge-std OpenZeppelin/openzeppelin-contracts aave/aave-v3-core --no-commit
forge script script/Deploy.s.sol:Deploy \
  --rpc-url $RPC_URL \
  --broadcast \
  --private-key $PRIVATE_KEY \
  -vvvv
# → copy printed address into .env as LIQUIDATION_BOT

# Run
cd executor-node && npm run dev
```

## Flow

```
Owner → liquidate(params)
         │
         ▼
   Balancer.flashLoan  (or Aave.flashLoanSimple)
         │
         ▼
   receiveFlashLoan / executeOperation
         │
         ├─ approve Pool
         ├─ Pool.liquidationCall(...)
         ├─ receive collateral (+ bonus)
         ├─ Uniswap V3 exactInputSingle
         ├─ repay flash loan
         └─ transfer leftover debtAsset → DEPLOYER
```

## Security

- Keys only in `.env` (gitignored)
- Only owner can trigger liquidations
- `minProfitWei` check inside callback
- Routers must be approved
- Always simulate before broadcasting
- Prefer Balancer (0 % fee) when liquidity allows

## Disclaimer

Educational / research software. Test thoroughly on forks before mainnet use. You are responsible for gas, keys, and compliance.
