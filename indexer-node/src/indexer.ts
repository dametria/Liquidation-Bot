/**
 * Aave V3 Borrower Event Indexer (production-hardened)
 *
 * If you see 400 Bad Request even on small chunks (e.g. 10–50 blocks),
 * the RPC is rate-limiting or rejecting eth_getLogs. Switch to a paid
 * endpoint (Alchemy, QuickNode, Infura, LlamaRPC, etc.).
 */

import { config } from "dotenv";
import { ethers } from "ethers";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

config({ path: path.resolve(__dirname, "../../.env") });

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
const INITIAL_CHUNK = Number(process.env.INDEXER_CHUNK_SIZE || "500");
const MIN_CHUNK = Number(process.env.INDEXER_MIN_CHUNK || "10");
const SAVE_EVERY = Number(process.env.INDEXER_SAVE_EVERY || "50");
const POLL_INTERVAL_MS = Number(process.env.INDEXER_POLL_INTERVAL_MS || "12000");
const MAX_RETRIES = Number(process.env.INDEXER_MAX_RETRIES || "8");
const BACKFILL_ONLY = process.argv.includes("--backfill-only");

const POOL_ABI = [
  "event Borrow(address indexed reserve, address user, address indexed onBehalfOf, uint256 amount, uint8 interestRateMode, uint256 borrowRate, uint16 indexed referralCode)",
  "event Repay(address indexed reserve, address indexed user, address indexed repayer, uint256 amount, bool useATokens)",
  "event LiquidationCall(address indexed collateralAsset, address indexed debtAsset, address indexed user, uint256 debtToCover, uint256 liquidatedCollateralAmount, address liquidator, bool receiveAToken)",
];

type LogLevel = "debug" | "info" | "warn" | "error";
const LOG_LEVEL = (process.env.LOG_LEVEL || "info") as LogLevel;
const LEVELS: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

function log(level: LogLevel, msg: string, extra?: Record<string, unknown>) {
  if (LEVELS[level] < LEVELS[LOG_LEVEL]) return;
  const line = { ts: new Date().toISOString(), level, msg, ...extra };
  (level === "error" ? console.error : console.log)(JSON.stringify(line));
}

const borrowers = new Set<string>();
let dirtyCount = 0;
let lastSave = 0;
let lastProcessedBlock = 0;
let shuttingDown = false;
let totalEvents = 0;
let liveMode: "ws" | "poll" | "none" = "none";
let rateLimitStrikes = 0;

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
    log("warn", "Failed to load users.json, starting fresh", { error: e.message });
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
  if (!force && dirtyCount < SAVE_EVERY && Date.now() - lastSave < 15_000) return;
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
      if (dirtyCount >= SAVE_EVERY) saveUsers();
    }
  } catch {
    /* invalid */
  }
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

function isRangeLimitError(err: any): boolean {
  const msg = String(err?.shortMessage || err?.message || err || "").toLowerCase();
  return (
    msg.includes("400") ||
    msg.includes("bad request") ||
    msg.includes("block range") ||
    msg.includes("query returned more") ||
    msg.includes("response size") ||
    msg.includes("log response size") ||
    msg.includes("exceed") ||
    msg.includes("too many") ||
    msg.includes("limit exceeded") ||
    msg.includes("rate limit") ||
    msg.includes("429") ||
    (msg.includes("eth_getlogs") && msg.includes("limited"))
  );
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
      // Don't spin on range/rate-limit errors — caller will shrink or back off
      if (isRangeLimitError(err)) throw err;
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

function isWsUrl(url: string) {
  return url.startsWith("ws://") || url.startsWith("wss://");
}

function createHttpProvider(): ethers.JsonRpcProvider {
  return new ethers.JsonRpcProvider(RPC_URL, undefined, { staticNetwork: true });
}

async function backfill(provider: ethers.Provider) {
  const pool = new ethers.Contract(AAVE_POOL, POOL_ABI, provider);
  const latest = await withRetry("getBlockNumber", () => provider.getBlockNumber());

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
    chunk: INITIAL_CHUNK,
    rpc: RPC_URL.includes("arbitrum.io") ? "public-arbitrum (often rate-limits getLogs)" : "custom",
  });

  if (RPC_URL.includes("arb1.arbitrum.io") || RPC_URL.includes("arbitrum.io/rpc")) {
    log("warn", "Public Arbitrum RPC frequently returns 400 on eth_getLogs. Prefer Alchemy/QuickNode/LlamaRPC.", {
      hint: "Set RPC_URL in .env to a paid or higher-limit endpoint",
    });
  }

  let chunk = INITIAL_CHUNK;
  let processed = 0;
  let start = fromBlock;

  while (start <= latest && !shuttingDown) {
    const end = Math.min(start + chunk - 1, latest);
    try {
      const events = await withRetry(
        `queryFilter ${start}-${end}`,
        () => pool.queryFilter(pool.filters.Borrow(), start, end),
        2
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
      rateLimitStrikes = 0;

      if (end === latest || processed % 50 === 0 || (end - fromBlock) % 5000 < chunk) {
        log("info", "Backfill progress", {
          from: start,
          to: end,
          events: processed,
          unique: borrowers.size,
          chunk,
        });
      }

      if (chunk < INITIAL_CHUNK) {
        chunk = Math.min(INITIAL_CHUNK, Math.max(chunk + 25, Math.floor(chunk * 1.2)));
      }

      start = end + 1;
    } catch (err: any) {
      const msg = err.shortMessage || err.message || "";
      const rangeErr = isRangeLimitError(err);

      if (rangeErr && chunk <= Math.max(MIN_CHUNK, 50)) {
        // Tiny range still failing → rate limit / RPC ban, not range size
        rateLimitStrikes++;
        const backoff = Math.min(120_000, 5_000 * Math.pow(2, Math.min(rateLimitStrikes, 5)));
        log("warn", "RPC rejecting even small getLogs ranges — backing off (likely rate limit)", {
          start,
          end,
          chunk,
          strike: rateLimitStrikes,
          backoffMs: backoff,
          error: msg,
          tip: "Switch RPC_URL to Alchemy, QuickNode, Infura, or ankr",
        });

        if (rateLimitStrikes >= 8) {
          log("error", "Too many rate-limit responses. Saving progress and exiting so you can change RPC.", {
            lastProcessedBlock,
            borrowers: borrowers.size,
          });
          saveUsers(true);
          saveCheckpoint(lastProcessedBlock);
          process.exit(2);
        }

        await sleep(backoff);
        // Do not shrink below MIN_CHUNK; do not advance start
        chunk = Math.max(MIN_CHUNK, Math.min(chunk, 50));
        continue;
      }

      log("warn", "Chunk failed, shrinking", {
        start,
        end,
        chunk,
        rangeLimit: rangeErr,
        error: msg,
      });

      if (chunk <= MIN_CHUNK) {
        log("error", "Skipping block range after repeated failure at min chunk", {
          from: start,
          to: end,
        });
        saveCheckpoint(end);
        start = end + 1;
        chunk = Math.max(MIN_CHUNK, Math.floor(INITIAL_CHUNK / 5));
        await sleep(2000);
      } else {
        chunk = rangeErr
          ? Math.max(MIN_CHUNK, Math.floor(chunk / 4))
          : Math.max(MIN_CHUNK, Math.floor(chunk / 2));
        await sleep(rangeErr ? 500 : 1000);
      }
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

async function startWsLive() {
  liveMode = "ws";
  log("info", "Starting WebSocket live listener");

  let wsProvider: ethers.WebSocketProvider | null = null;
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
    const pool = new ethers.Contract(AAVE_POOL, POOL_ABI, wsProvider);

    pool.on(pool.filters.Borrow(), (_r, user, onBehalfOf) => {
      addBorrower(onBehalfOf);
      addBorrower(user);
      totalEvents++;
      log("info", "Borrow", { user: onBehalfOf, unique: borrowers.size });
      saveUsers();
    });
    pool.on(pool.filters.Repay(), (_r, user) => addBorrower(user));
    pool.on(pool.filters.LiquidationCall(), (_c, _d, user) => addBorrower(user));

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
    log("info", "Reconnecting WebSocket", { attempt: reconnectAttempt, delayMs: delay });
    setTimeout(() => {
      attach().catch((e) => {
        log("error", "Reconnect failed", { error: e.message });
        scheduleReconnect();
      });
    }, delay);
  };

  await attach();

  const http = createHttpProvider();
  const catchChunk = Math.min(INITIAL_CHUNK, 200);
  setInterval(async () => {
    if (shuttingDown) return;
    try {
      const latest = await http.getBlockNumber();
      if (latest <= lastProcessedBlock) return;
      const from = lastProcessedBlock + 1;
      const to = Math.min(latest, from + catchChunk - 1);
      const catchPool = new ethers.Contract(AAVE_POOL, POOL_ABI, http);
      const events = await catchPool.queryFilter(catchPool.filters.Borrow(), from, to);
      for (const ev of events) {
        const args = (ev as any).args;
        if (args) {
          addBorrower(args.onBehalfOf);
          addBorrower(args.user);
        }
      }
      if (events.length > 0) {
        log("info", "WS catch-up", { from, to, events: events.length });
        saveUsers();
      }
      saveCheckpoint(to);
    } catch (e: any) {
      log("debug", "Catch-up poll failed", { error: e.message });
    }
  }, Math.max(POLL_INTERVAL_MS, 15_000));
}

async function startHttpPolling() {
  liveMode = "poll";
  log("info", "Starting HTTP polling live mode", { intervalMs: POLL_INTERVAL_MS });

  const provider = createHttpProvider();
  const pool = new ethers.Contract(AAVE_POOL, POOL_ABI, provider);
  const pollChunk = Math.min(INITIAL_CHUNK, 200);

  const tick = async () => {
    if (shuttingDown) return;
    try {
      const latest = await withRetry("poll.getBlockNumber", () => provider.getBlockNumber());
      if (latest <= lastProcessedBlock) return;

      const from = lastProcessedBlock + 1;
      const to = Math.min(latest, from + pollChunk - 1);

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
      log("error", "WS live failed, falling back to HTTP poll", { error: e.message });
      await startHttpPolling();
    }
  } else {
    await startHttpPolling();
  }

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
