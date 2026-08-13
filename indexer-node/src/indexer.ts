/**
 * Aave V3 Borrower Event Indexer
 *
 * - Backfills recent Borrow events from the Aave Pool
 * - Listens live for Borrow / Repay / LiquidationCall
 * - Maintains a set of addresses that have interacted as borrowers
 * - Writes them to shared/users.json for the scanner/executor
 *
 * Usage:
 *   cd indexer-node && npm i && npm run dev
 *
 * Env (from project root .env):
 *   RPC_URL              – HTTP or WS RPC (WS preferred for live)
 *   AAVE_POOL            – Aave V3 Pool address
 *   USERS_FILE           – path to users.json (default ../shared/users.json)
 *   INDEXER_LOOKBACK_BLOCKS – how many blocks to backfill (default 50000)
 *   INDEXER_SAVE_EVERY   – write users.json every N new addresses (default 25)
 */

import { config } from "dotenv";
import { ethers } from "ethers";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

config({ path: path.resolve(__dirname, "../../.env") });

// ─── Config ───────────────────────────────────────────────────────────
const RPC_URL = process.env.RPC_URL || "https://arb1.arbitrum.io/rpc";
const AAVE_POOL = ethers.getAddress(
  process.env.AAVE_POOL || "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
);
const USERS_FILE =
  process.env.USERS_FILE ||
  path.resolve(__dirname, "../../shared/users.json");
const LOOKBACK = Number(process.env.INDEXER_LOOKBACK_BLOCKS || "50000");
const SAVE_EVERY = Number(process.env.INDEXER_SAVE_EVERY || "25");
const BACKFILL_ONLY = process.argv.includes("--backfill-only");

// Minimal Aave V3 Pool ABI – only the events we care about
const POOL_ABI = [
  "event Borrow(address indexed reserve, address user, address indexed onBehalfOf, uint256 amount, uint8 interestRateMode, uint256 borrowRate, uint16 indexed referralCode)",
  "event Repay(address indexed reserve, address indexed user, address indexed repayer, uint256 amount, bool useATokens)",
  "event LiquidationCall(address indexed collateralAsset, address indexed debtAsset, address indexed user, uint256 debtToCover, uint256 liquidatedCollateralAmount, address liquidator, bool receiveAToken)",
];

// ─── State ────────────────────────────────────────────────────────────
const borrowers = new Set<string>();
let dirtyCount = 0;
let lastSave = Date.now();

function loadExisting() {
  try {
    if (fs.existsSync(USERS_FILE)) {
      const raw = JSON.parse(fs.readFileSync(USERS_FILE, "utf8"));
      if (Array.isArray(raw)) {
        for (const a of raw) {
          if (typeof a === "string" && a.startsWith("0x") && a.length === 42) {
            borrowers.add(ethers.getAddress(a));
          }
        }
      }
    }
  } catch {
    // ignore corrupt file
  }
  console.log(`Loaded ${borrowers.size} existing addresses from ${USERS_FILE}`);
}

function saveUsers(force = false) {
  if (!force && dirtyCount < SAVE_EVERY && Date.now() - lastSave < 30_000) {
    return;
  }
  const list = Array.from(borrowers).sort();
  fs.mkdirSync(path.dirname(USERS_FILE), { recursive: true });
  fs.writeFileSync(USERS_FILE, JSON.stringify(list, null, 2) + "\n");
  console.log(`Saved ${list.length} borrowers → ${USERS_FILE}`);
  dirtyCount = 0;
  lastSave = Date.now();
}

function addBorrower(addr: string) {
  try {
    const checksummed = ethers.getAddress(addr);
    if (checksummed === ethers.ZeroAddress) return;
    if (!borrowers.has(checksummed)) {
      borrowers.add(checksummed);
      dirtyCount++;
      if (dirtyCount >= SAVE_EVERY) saveUsers();
    }
  } catch {
    // invalid address
  }
}

// ─── Backfill ───────────────────────────────────────────────────────
async function backfill(provider: ethers.Provider, pool: ethers.Contract) {
  const latest = await provider.getBlockNumber();
  const fromBlock = Math.max(0, latest - LOOKBACK);
  console.log(`Backfilling Borrow events from block ${fromBlock} → ${latest} …`);

  // Chunk to avoid RPC limits (especially on public endpoints)
  const CHUNK = 2000;
  let processed = 0;

  for (let start = fromBlock; start <= latest; start += CHUNK) {
    const end = Math.min(start + CHUNK - 1, latest);
    try {
      const filter = pool.filters.Borrow();
      const events = await pool.queryFilter(filter, start, end);
      for (const ev of events) {
        // onBehalfOf is the actual borrower in most cases
        const onBehalfOf = (ev as any).args?.onBehalfOf as string | undefined;
        const user = (ev as any).args?.user as string | undefined;
        if (onBehalfOf) addBorrower(onBehalfOf);
        if (user) addBorrower(user);
      }
      processed += events.length;
      process.stdout.write(
        `\r  blocks ${start}-${end} | events so far: ${processed} | unique: ${borrowers.size}`
      );
    } catch (err: any) {
      console.warn(
        `\n  chunk ${start}-${end} failed: ${err.shortMessage || err.message}`
      );
      // continue with next chunk
    }
  }
  console.log(`\nBackfill done. Unique borrowers: ${borrowers.size}`);
  saveUsers(true);
}

// ─── Live listener ──────────────────────────────────────────────────
function startLive(provider: ethers.Provider, pool: ethers.Contract) {
  console.log("Starting live event listener …");

  pool.on(
    pool.filters.Borrow(),
    (reserve, user, onBehalfOf, amount, _mode, _rate, _ref, event) => {
      addBorrower(onBehalfOf);
      addBorrower(user);
      console.log(
        `[Borrow] ${onBehalfOf} borrowed on ${reserve} | total unique: ${borrowers.size}`
      );
      saveUsers();
    }
  );

  // Optional: still track users who repay / get liquidated so the set stays useful
  pool.on(pool.filters.Repay(), (_reserve, user) => {
    addBorrower(user);
  });

  pool.on(pool.filters.LiquidationCall(), (_c, _d, user) => {
    addBorrower(user);
  });

  // Periodic save even if no new addresses
  setInterval(() => saveUsers(true), 60_000);

  console.log("Listening for new Borrow / Repay / LiquidationCall events …");
}

// ─── Main ─────────────────────────────────────────────────────────
async function main() {
  console.log("Aave V3 Borrower Indexer");
  console.log("  RPC      :", RPC_URL);
  console.log("  Pool     :", AAVE_POOL);
  console.log("  Users file:", USERS_FILE);
  console.log("  Lookback :", LOOKBACK, "blocks");

  loadExisting();

  // Prefer WebSocket if the URL looks like one; otherwise HTTP
  let provider: ethers.Provider;
  if (RPC_URL.startsWith("ws://") || RPC_URL.startsWith("wss://")) {
    provider = new ethers.WebSocketProvider(RPC_URL);
  } else {
    provider = new ethers.JsonRpcProvider(RPC_URL);
  }

  const pool = new ethers.Contract(AAVE_POOL, POOL_ABI, provider);

  await backfill(provider, pool);

  if (BACKFILL_ONLY) {
    console.log("--backfill-only set, exiting.");
    process.exit(0);
  }

  startLive(provider, pool);

  // Keep process alive
  process.on("SIGINT", () => {
    console.log("\nShutting down …");
    saveUsers(true);
    process.exit(0);
  });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
