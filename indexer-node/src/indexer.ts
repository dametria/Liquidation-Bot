/**
 * Aave V3 Borrower Event Indexer (production-hardened)
 *
 * Features:
 *  - Checkpointed backfill (resumes after crash)
 *  - Adaptive chunk size + exponential backoff on RPC errors
 *  - Atomic users.json writes
 *  - WebSocket auto-reconnect with resubscribe
 *  - HTTP polling fallback when WS is unavailable
 *  - Graceful SIGINT / SIGTERM shutdown
 *  - Structured logging + basic metrics
 *
 * Usage:
 *   cd indexer-node && npm i && npm run dev
 *   npm run backfill          # one-shot backfill only
 *
 * Env (project root .env):
 *   RPC_URL, AAVE_POOL, USERS_FILE
 *   INDEXER_LOOKBACK_BLOCKS   (default 100000)
 *   INDEXER_CHUNK_SIZE        (default 2000)
 *   INDEXER_SAVE_EVERY        (default 50)
 *   INDEXER_POLL_INTERVAL_MS  (default 12000)  – HTTP live fallback
 *   INDEXER_MAX_RETRIES       (default 8)
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
const CHECKPOINT_FILE =
  process.env.INDEXER_CHECKPOINT_FILE ||
  path.resolve(__dirname, "../../shared/indexer-checkpoint.json");

const LOOKBACK = Number(process.env.INDEXER_LOOKBACK_BLOCKS || "100000");
const INITIAL_CHUNK = Number(process.env.INDEXER_CHUNK_SIZE || "2000");
const SAVE_EVERY = Number(process.env.INDEXER_SAVE_EVERY || "50");
const POLL_INTERVAL_MS = Number(process.env.INDEXER_POLL_INTERVAL_MS || "12000");
const MAX_RETRIES = Number(process.env.INDEXER_MAX_RETRIES || "8");
const BACKFILL_ONLY = process.argv.includes("--backfill-only");

const POOL_ABI = [
  "event Borrow(address indexed reserve, address user, address indexed onBehalfOf, uint256 amount, uint8 interestRateMode, uint256 borrowRate, uint16 indexed referralCode)",
  "event Repay(address indexed reserve, address indexed user, address indexed repayer, uint256 amount, bool useATokens)",
  "event LiquidationCall(address indexed collateralAsset, address indexed debtAsset, address indexed user, uint256 debtToCover, uint256 liquidatedCollateralAmount, address liquidator, bool receiveAToken)",
];

// ─── Logging ───────────────────────────────────────────────────────
type LogLevel = "debug" | "info" | "warn" | "error";
const LOG_LEVEL = (process.env.LOG_LEVEL || "info") as LogLevel;
const LEVELS: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

function log(level: LogLevel, msg: string, extra?: Record<string, unknown>) {
  if (LEVELS[level] < LEVELS[LOG_LEVEL]) return;
  const line = {
    ts: new Date().toISOString(),
    level,
    msg,
    ...extra,
  };
  const out = level === "error" ? console.error : console.log;
  out(JSON.stringify(line));
}

// ─── State ───────────────────────────────────────────────────────────
const borrowers = new Set<string>();
let dirtyCount = 0;
let lastSave = 0;
let lastProcessedBlock = 0;
let shuttingDown = false;
let totalEvents = 0;
let liveMode: "ws" | "poll" | "none" = "none";

// ─── Persistence ──────────────────────────────────────────────────
function atomicWrite(filePath: string, data: string) {
  const dir = path.dirname(filePath);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = `${filePath}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, data, { encoding: "utf8" });
  fs.renameSync(tmp, filePath);
}

function loadExisting() {
  try {
    if (fs.existsSync(USERS_FILE)) {
      const raw = JSON.parse(fs.readFileSync(USERS_FILE, "utf8"));
      if (Array.isArray(raw)) {
        for (const a of raw) {
          if (typeof a === "string" && /^0x[a-fA-F0-9]{40}$/.test(a)) {
            try {
              borrowers.add(ethers.getAddress(a));
            } catch {
              /* skip */
            }
          }
        }
      }
    }
  } catch (e: any) {
    log("warn", "Failed to load users.json, starting fresh", {
      error: e.message,
    });
  }

  try {
    if (fs.existsSync(CHECKPOINT_FILE)) {
      const cp = JSON.parse(fs.readFileSync(CHECKPOINT_FILE, "utf8"));
      if (typeof cp.lastProcessedBlock === "number" && cp.lastProcessedBlock > 0) {
        lastProcessedBlock = cp.lastProcessedBlock;
      }
    }
  } catch (e: any) {
    log("warn", "Failed to load checkpoint", { error: e.message });
  }

  log("info", "State loaded", {
    borrowers: borrowers.size,
    lastProcessedBlock,
    usersFile: USERS_FILE,
  });
}

function saveUsers(force = false) {
  if (shuttingDown && !force) return;
  if (!force && dirtyCount < SAVE_EVERY && Date.now() - lastSave < 15_000) {
    return;
  }
  try {
    const list = Array.from(borrowers).sort();
    atomicWrite(USERS_FILE, JSON.stringify(list, null, 2) + "\n");
    dirtyCount = 0;
    lastSave = Date.now();
    log("debug", "Saved users", { count: list.length });
  } catch (e: any) {
    log("error", "Failed to save users.json", { error: e.message });
  }
}

function saveCheckpoint(block: number) {
  lastProcessedBlock = block;
  try {
    atomicWrite(
      CHECKPOINT_FILE,
      JSON.stringify(
        {
          lastProcessedBlock: block,
          borrowers: borrowers.size,
          updatedAt: new Date().toISOString(),
        },
        null,
        2
      ) + "\n"
    );
  } catch (e: any) {
    log("error", "Failed to save checkpoint", { error: e.message });
  }
}

function addBorrower(addr: string | undefined | null) {
  if (!addr) return;
  try {
    const checksummed = ethers.getAddress(addr);
    if (checksummed === ethers.ZeroAddress) return;
    if (!borrowers.has(checksummed)) {
      borrowers.add(checksummed);
      dirtyCount++;
      if (dirtyCount >= SAVE_EVERY) {
        saveUsers();
      }
    }
  } catch {
    // invalid address – ignore
  }
}

// ─── Retry helper ───────────────────────────────────────────────
function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

async function withRetry<T>(
  label: string,
  fn: () => Promise<T>,
  maxRetries = MAX_RETRIES
): Promise<T> {
  let lastErr: any;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (err: any) {
      lastErr = err;
      if (attempt === maxRetries) break;
      const delay = Math.min(30_000, 500 * Math.pow(2, attempt));
      log("warn", `${label} failed, retrying`, {
        attempt: attempt + 1,
        delayMs: delay,
        error: err.shortMessage || err.message,
      });
      await sleep(delay);
    }
  }
  throw lastErr;
}

// ─── Provider factory ──────────────────────────────────────────
function isWsUrl(url: string) {
  return url.startsWith("ws://") || url.startsWith("wss://");
}

function createHttpProvider(): ethers.JsonRpcProvider {
  return new ethers.JsonRpcProvider(RPC_URL, undefined, {
    staticNetwork: true,
  });
}

// ─── Backfill (checkpointed, adaptive chunks) ──────────────────────
async function backfill(provider: ethers.Provider) {
  const pool = new ethers.Contract(AAVE_POOL, POOL_ABI, provider);
  const latest = await withRetry("getBlockNumber", () =>
    provider.getBlockNumber()
  );

  // Resume from checkpoint if it is within the lookback window
  const lookbackStart = Math.max(0, latest - LOOKBACK);
  let fromBlock =
    lastProcessedBlock > 0
      ? Math.max(lastProcessedBlock + 1, lookbackStart)
      : lookbackStart;

  if (fromBlock > latest) {
    log("info", "Already up to date", { lastProcessedBlock, latest });
    return latest;
  }

  log("info", "Backfill starting", {
    fromBlock,
    toBlock: latest,
    span: latest - fromBlock + 1,
  });

  let chunk = INITIAL_CHUNK;
  let processed = 0;
  let start = fromBlock;

  while (start <= latest && !shuttingDown) {
    const end = Math.min(start + chunk - 1, latest);
    try {
      const events = await withRetry(
        `queryFilter ${start}-${end}`,
        () => pool.queryFilter(pool.filters.Borrow(), start, end),
        5
      );

      for (const ev of events) {
        const args = (ev as any).args;
        if (args) {
          addBorrower(args.onBehalfOf);
          addBorrower(args.user);
        }
      }

      processed += events.length;
      totalEvents += events.length;
      saveCheckpoint(end);

      if (processed % 100 === 0 || end === latest) {
        log("info", "Backfill progress", {
          from: start,
          to: end,
          events: processed,
          unique: borrowers.size,
          chunk,
        });
      }

      // Slowly grow chunk size after successes
      if (chunk < INITIAL_CHUNK) {
        chunk = Math.min(INITIAL_CHUNK, Math.floor(chunk * 1.5) || 1);
      }

      start = end + 1;
    } catch (err: any) {
      // Shrink chunk on persistent failure (common with public RPCs)
      const msg = err.shortMessage || err.message || "";
      log("warn", "Chunk failed, shrinking", {
        start,
        end,
        chunk,
        error: msg,
      });
      if (chunk <= 1) {
        // Skip the single bad block rather than hang forever
        log("error", "Skipping single block after repeated failure", {
          block: start,
        });
        saveCheckpoint(start);
        start += 1;
        chunk = Math.max(1, Math.floor(INITIAL_CHUNK / 4));
      } else {
        chunk = Math.max(1, Math.floor(chunk / 2));
      }
      await sleep(1000);
    }
  }

  saveUsers(true);
  log("info", "Backfill complete", {
    unique: borrowers.size,
    events: processed,
    lastProcessedBlock,
  });
  return latest;
}

// ─── Live: WebSocket with reconnect ─────────────────────────────
async function startWsLive() {
  liveMode = "ws";
  log("info", "Starting WebSocket live listener");

  let wsProvider: ethers.WebSocketProvider | null = null;
  let pool: ethers.Contract | null = null;
  let reconnectAttempt = 0;

  const attach = async () => {
    if (shuttingDown) return;

    if (wsProvider) {
      try {
        await wsProvider.destroy();
      } catch {
        /* ignore */
      }
    }

    wsProvider = new ethers.WebSocketProvider(RPC_URL);
    pool = new ethers.Contract(AAVE_POOL, POOL_ABI, wsProvider);

    const onBorrow = (
      _reserve: string,
      user: string,
      onBehalfOf: string
    ) => {
      addBorrower(onBehalfOf);
      addBorrower(user);
      totalEvents++;
      log("info", "Borrow", {
        user: onBehalfOf,
        unique: borrowers.size,
      });
      saveUsers();
    };

    const onRepay = (_reserve: string, user: string) => {
      addBorrower(user);
    };

    const onLiq = (_c: string, _d: string, user: string) => {
      addBorrower(user);
    };

    pool.on(pool.filters.Borrow(), onBorrow);
    pool.on(pool.filters.Repay(), onRepay);
    pool.on(pool.filters.LiquidationCall(), onLiq);

    // ethers v6 websocket: listen for provider errors / close
    const websocket = (wsProvider as any).websocket;
    if (websocket && typeof websocket.on === "function") {
      websocket.on("close", () => {
        log("warn", "WebSocket closed, will reconnect");
        scheduleReconnect();
      });
      websocket.on("error", (err: Error) => {
        log("warn", "WebSocket error", { error: err.message });
      });
    }

    // Also watch for provider-level errors
    wsProvider.on("error", (err) => {
      log("warn", "Provider error", { error: String(err) });
    });

    reconnectAttempt = 0;
    log("info", "WebSocket subscribed to Borrow/Repay/LiquidationCall");
  };

  const scheduleReconnect = () => {
    if (shuttingDown) return;
    reconnectAttempt++;
    const delay = Math.min(60_000, 1000 * Math.pow(2, reconnectAttempt));
    log("info", "Reconnecting WebSocket", {
      attempt: reconnectAttempt,
      delayMs: delay,
    });
    setTimeout(() => {
      attach().catch((e) => {
        log("error", "Reconnect failed", { error: e.message });
        scheduleReconnect();
      });
    }, delay);
  };

  await attach();

  // Safety: periodic HTTP catch-up in case WS missed events
  const http = createHttpProvider();
  setInterval(async () => {
    if (shuttingDown) return;
    try {
      const latest = await http.getBlockNumber();
      if (latest > lastProcessedBlock) {
        const from = lastProcessedBlock + 1;
        const catchPool = new ethers.Contract(AAVE_POOL, POOL_ABI, http);
        const events = await catchPool.queryFilter(
          catchPool.filters.Borrow(),
          from,
          latest
        );
        for (const ev of events) {
          const args = (ev as any).args;
          if (args) {
            addBorrower(args.onBehalfOf);
            addBorrower(args.user);
          }
        }
        if (events.length > 0) {
          log("info", "WS catch-up", {
            from,
            to: latest,
            events: events.length,
          });
          saveUsers();
        }
        saveCheckpoint(latest);
      }
    } catch (e: any) {
      log("debug", "Catch-up poll failed", { error: e.message });
    }
  }, Math.max(POLL_INTERVAL_MS, 15_000));
}

// ─── Live: HTTP polling fallback ───────────────────────────────
async function startHttpPolling() {
  liveMode = "poll";
  log("info", "Starting HTTP polling live mode", {
    intervalMs: POLL_INTERVAL_MS,
  });

  const provider = createHttpProvider();
  const pool = new ethers.Contract(AAVE_POOL, POOL_ABI, provider);

  const tick = async () => {
    if (shuttingDown) return;
    try {
      const latest = await withRetry("poll.getBlockNumber", () =>
        provider.getBlockNumber()
      );
      if (latest <= lastProcessedBlock) return;

      const from = lastProcessedBlock + 1;
      // Cap range to avoid huge queries if we were offline
      const to = Math.min(latest, from + INITIAL_CHUNK - 1);

      const events = await withRetry(`poll.query ${from}-${to}`, () =>
        pool.queryFilter(pool.filters.Borrow(), from, to)
      );

      for (const ev of events) {
        const args = (ev as any).args;
        if (args) {
          addBorrower(args.onBehalfOf);
          addBorrower(args.user);
        }
      }

      if (events.length > 0) {
        totalEvents += events.length;
        log("info", "Poll batch", {
          from,
          to,
          events: events.length,
          unique: borrowers.size,
        });
        saveUsers();
      }

      saveCheckpoint(to);
    } catch (e: any) {
      log("warn", "Poll tick failed", { error: e.shortMessage || e.message });
    }
  };

  await tick();
  setInterval(tick, POLL_INTERVAL_MS);
}

// ─── Shutdown ────────────────────────────────────────────────
function setupShutdown() {
  const halt = (signal: string) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log("info", `Received ${signal}, shutting down`);
    saveUsers(true);
    saveCheckpoint(lastProcessedBlock);
    log("info", "Final stats", {
      borrowers: borrowers.size,
      totalEvents,
      lastProcessedBlock,
      liveMode,
    });
    process.exit(0);
  };
  process.on("SIGINT", () => halt("SIGINT"));
  process.on("SIGTERM", () => halt("SIGTERM"));
}

// ─── Main ────────────────────────────────────────────────────────
async function main() {
  setupShutdown();

  log("info", "Aave V3 Borrower Indexer starting", {
    rpc: RPC_URL.replace(/\/v2\/[\w-]+/, "/v2/***"),
    pool: AAVE_POOL,
    lookback: LOOKBACK,
    chunk: INITIAL_CHUNK,
    backfillOnly: BACKFILL_ONLY,
  });

  loadExisting();

  // Always use HTTP for backfill (more reliable for large queryFilter ranges)
  const httpProvider = createHttpProvider();
  await backfill(httpProvider);

  if (BACKFILL_ONLY) {
    log("info", "Backfill-only complete, exiting");
    process.exit(0);
  }

  if (isWsUrl(RPC_URL)) {
    try {
      await startWsLive();
    } catch (e: any) {
      log("error", "WS live failed, falling back to HTTP poll", {
        error: e.message,
      });
      await startHttpPolling();
    }
  } else {
    await startHttpPolling();
  }

  // Heartbeat metrics
  setInterval(() => {
    if (shuttingDown) return;
    log("info", "Heartbeat", {
      borrowers: borrowers.size,
      totalEvents,
      lastProcessedBlock,
      liveMode,
    });
  }, 60_000);
}

main().catch((e) => {
  log("error", "Fatal", { error: e.message, stack: e.stack });
  process.exit(1);
});
