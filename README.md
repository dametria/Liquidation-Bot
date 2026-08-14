# Aave V3 Liquidation Bot (Balancer / Aave Flash Loans)

Capital-efficient liquidation bot that:

1. **Indexes** Aave V3 borrowers via on-chain events
2. Scans those positions for **health factor < 1**
3. Borrows the debt asset via **Balancer V2 flash loan (0 % fee)** or **Aave V3 flashLoanSimple**
4. Calls `liquidationCall`, receives collateral + liquidation bonus
5. Swaps collateral → debt asset on Uniswap V3
6. Repays the flash loan
7. Sends remaining profit to the **deployer wallet**

All liquidation steps are atomic in a single transaction. Only gas is required.

## Layout

```
Liquidation-Bot/
├── contracts/                  # Foundry – LiquidationBot.sol
├── indexer-node/               # Event indexer (backfill + live)
│   └── src/indexer.ts
├── analyzer-python/            # HF scanner + profit estimate
│   ├── src/scanner.py
│   ├── src/aave_v3_liquidation_math.py   # exact LiquidationLogic math
│   └── tests/
├── executor-node/              # Simulation + tx submission
│   └── src/index.ts
├── shared/users.json           # Populated by the indexer
├── .env.example
└── README.md
```

## Quick Start

### 1. Install tools

```bash
curl -L https://foundry.paradigm.xyz | bash && foundryup

cd executor-node && npm i && cd ..
cd indexer-node && npm i && cd ..
cd analyzer-python && pip install -r requirements.txt && cd ..
```

### 2. Install Solidity dependencies

```bash
cd contracts
forge install foundry-rs/forge-std OpenZeppelin/openzeppelin-contracts aave/aave-v3-core --no-commit
forge build
```

### 3. Config

```bash
cp .env.example .env
# set PRIVATE_KEY, RPC_URL, AAVE_*, LIQUIDATION_BOT (after deploy)
```

### 4. Deploy the contract

```bash
cd contracts
export PRIVATE_KEY=0x...
export AAVE_ADDRESSES_PROVIDER=0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb
export BALANCER_VAULT=0xBA12222222228d8Ba445958a75a0704d566BF2C8
export RPC_URL=https://arb1.arbitrum.io/rpc

forge script script/Deploy.s.sol:Deploy \
  --rpc-url $RPC_URL --broadcast --private-key $PRIVATE_KEY -vvvv
```

Copy the printed address into `.env` as `LIQUIDATION_BOT`.

### 5. Index borrowers (run this first / leave running)

```bash
cd indexer-node
npm run dev
```

This will:
- Backfill recent `Borrow` events (default last ~50k blocks)
- Write addresses to `shared/users.json`
- Keep listening for new borrows live

One-shot backfill only:

```bash
npm run backfill
```

### 6. Run the liquidation cycle

```bash
cd executor-node
npm run dev
```

The executor reads `shared/users.json`, asks the Python scanner for opportunities (HF < 1 + min profit), simulates, then submits.

## Analyzer (exact LiquidationLogic math)

```bash
cd analyzer-python
pip install -r requirements.txt pytest
PYTHONPATH=src pytest tests/ -q   # 28 tests
```

The scanner uses `aave_v3_liquidation_math.compute_liquidation_amounts` for close-factor, dust prevention, and protocol-fee sizing before profit filtering.

## Indexer details

| Setting | Env var | Default |
|---------|---------|--------|
| Lookback blocks | `INDEXER_LOOKBACK_BLOCKS` | 50000 |
| Save every N new addresses | `INDEXER_SAVE_EVERY` | 25 |
| Users file | `USERS_FILE` | `./shared/users.json` |
| Pool | `AAVE_POOL` | Arbitrum V3 Pool |

For faster live updates use a **WebSocket** RPC:

```bash
RPC_URL=wss://arb-mainnet.g.alchemy.com/v2/YOUR_KEY
```

## Common errors

**`@aave/core-v3/... not found`**

```bash
cd contracts
forge install aave/aave-v3-core --no-commit
```

**`vm.envUint: environment variable "PRIVATE_KEY" not found`**

```bash
export PRIVATE_KEY=0x...
export AAVE_ADDRESSES_PROVIDER=...
export BALANCER_VAULT=...
```

**`No users.json` / Scanning 0 users**

Run the indexer first so it populates `shared/users.json`.

## Security

- Keys only in `.env` (gitignored)
- Only owner can trigger liquidations
- `minProfitWei` / off-chain `$3` filter
- Always simulate before broadcasting
- Prefer Balancer (0 % fee) when liquidity allows

## Disclaimer

Educational / research software. Test thoroughly on forks before mainnet use. You are responsible for gas, keys, and compliance.
