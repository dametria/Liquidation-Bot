# Aave V3 Liquidation Bot (Balancer / Aave Flash Loans)

Capital-efficient liquidation bot that:

1. Scans Aave V3 positions for **health factor < 1**
2. Borrows the debt asset via **Balancer V2 flash loan (0 % fee)** or **Aave V3 flashLoanSimple**
3. Calls `liquidationCall`, receives collateral + liquidation bonus
4. Swaps collateral → debt asset on Uniswap V3
5. Repays the flash loan
6. Sends remaining profit to the **deployer wallet**

All steps are atomic in a single transaction. Only gas is required.

## Layout

```
Liquidation-Bot/
├── contracts/                  # Foundry
│   ├── src/LiquidationBot.sol  # Core executor
│   ├── script/Deploy.s.sol
│   ├── foundry.toml
│   └── remappings.txt
├── analyzer-python/
│   └── src/scanner.py          # HF checks + profit estimate
├── executor-node/
│   └── src/index.ts            # Simulation + submission
├── shared/users.json
├── .env.example
└── README.md
```

## Quick Start

### 1. Install tools

```bash
# Foundry
curl -L https://foundry.paradigm.xyz | bash
foundryup

# Node
cd executor-node && npm i && cd ..

# Python
cd analyzer-python && pip install -r requirements.txt && cd ..
```

### 2. Install Solidity dependencies (required)

```bash
cd contracts

# Install the three libraries the contract needs
forge install foundry-rs/forge-std OpenZeppelin/openzeppelin-contracts aave/aave-v3-core --no-commit

# Verify the Aave interface is present
ls lib/aave-v3-core/contracts/flashloan/interfaces/IFlashLoanSimpleReceiver.sol
```

If the `ls` command succeeds, the import error is fixed.

### 3. Config

```bash
cp .env.example .env
# Edit PRIVATE_KEY, RPC_URL, AAVE_ADDRESSES_PROVIDER, BALANCER_VAULT
```

### 4. Build & Deploy

```bash
cd contracts
forge build          # should succeed after the install step above

forge script script/Deploy.s.sol:Deploy \
  --rpc-url $RPC_URL \
  --broadcast \
  --private-key $PRIVATE_KEY \
  -vvvv
```

Copy the printed `LiquidationBot` address into `.env` as `LIQUIDATION_BOT`.

### 5. Run the bot

```bash
cd executor-node && npm run dev
```

## Common error

```
Source "@aave/core-v3/contracts/flashloan/interfaces/IFlashLoanSimpleReceiver.sol" not found
```

→ You skipped the `forge install ... aave/aave-v3-core` step. Run it from the `contracts/` directory.

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
