/**
 * Liquidation Bot Executor
 * - Loads opportunities from Python scanner
 * - Simulates via callStatic
 * - Submits liquidate() – profit auto-sent to deployer by the contract
 */

import { config } from "dotenv";
import { ethers } from "ethers";
import { spawn } from "child_process";
import * as fs from "fs";
import * as path from "path";

config({ path: path.resolve(__dirname, "../../.env") });

const BOT_ABI = [
  "function liquidate((address collateralAsset,address debtAsset,address user,uint256 debtToCover,address swapRouter,uint24 poolFee,uint256 minCollateralOut,uint256 minDebtOutAfterSwap,bool useBalancer) params) external",
  "function setApprovedRouter(address router, bool approved) external",
  "function setMinProfit(uint256 _minProfitWei) external",
  "function paused() view returns (bool)",
  "function owner() view returns (address)",
  "event LiquidationExecuted(address indexed user,address indexed collateralAsset,address indexed debtAsset,uint256 debtCovered,uint256 collateralReceived,uint256 profit,bool usedBalancer)",
];

const SWAP_ROUTER = process.env.SWAP_ROUTER || "0xE592427A0AEce92De3Edee1F18E0157C05861564";

interface Opportunity {
  user: string;
  collateral_asset: string;
  debt_asset: string;
  debt_to_cover: number | string;
  estimated_collateral: number | string;
  health_factor: number;
  estimated_profit_usd: number;
  use_balancer: boolean;
  pool_fee: number;
  min_collateral_out: number | string;
  min_debt_out_after_swap: number | string;
}

async function runPythonScanner(users: string[]): Promise<Opportunity[]> {
  return new Promise((resolve, reject) => {
    const py = spawn("python3", ["src/scanner.py"], {
      cwd: path.resolve(__dirname, "../../analyzer-python"),
      env: { ...process.env },
    });
    let stdout = "";
    let stderr = "";
    py.stdout.on("data", (d) => (stdout += d.toString()));
    py.stderr.on("data", (d) => (stderr += d.toString()));
    py.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`scanner exited ${code}: ${stderr}`));
        return;
      }
      try {
        const parsed = JSON.parse(stdout);
        resolve(Array.isArray(parsed) ? parsed : []);
      } catch (e) {
        reject(e);
      }
    });
    py.stdin.write(JSON.stringify(users));
    py.stdin.end();
  });
}

async function main() {
  const rpc = process.env.RPC_URL;
  const pk = process.env.PRIVATE_KEY;
  const botAddress = process.env.LIQUIDATION_BOT;

  if (!rpc || !pk || !botAddress) {
    console.error("Missing RPC_URL / PRIVATE_KEY / LIQUIDATION_BOT in .env");
    process.exit(1);
  }

  const provider = new ethers.JsonRpcProvider(rpc);
  const wallet = new ethers.Wallet(pk, provider);
  const bot = new ethers.Contract(botAddress, BOT_ABI, wallet);

  console.log("Executor wallet:", wallet.address);
  console.log("Bot contract  :", botAddress);

  const usersPath =
    process.env.USERS_FILE || path.resolve(__dirname, "../../shared/users.json");
  let users: string[] = [];
  if (fs.existsSync(usersPath)) {
    users = JSON.parse(fs.readFileSync(usersPath, "utf8"));
  } else {
    console.warn("No users.json found");
  }

  console.log(`Scanning ${users.length} users…`);
  const opps = await runPythonScanner(users);
  console.log(`Found ${opps.length} candidate opportunities`);

  for (const opp of opps) {
    if (opp.estimated_profit_usd < Number(process.env.MIN_PROFIT_USD || "3")) {
      console.log(`Skip ${opp.user} – profit $${opp.estimated_profit_usd.toFixed(2)}`);
      continue;
    }

    const params = {
      collateralAsset: opp.collateral_asset,
      debtAsset: opp.debt_asset,
      user: opp.user,
      debtToCover: BigInt(opp.debt_to_cover),
      swapRouter: SWAP_ROUTER,
      poolFee: opp.pool_fee || 3000,
      minCollateralOut: BigInt(opp.min_collateral_out || 0),
      minDebtOutAfterSwap: BigInt(opp.min_debt_out_after_swap || 0),
      useBalancer: opp.use_balancer !== false,
    };

    try {
      console.log(
        `Simulating liquidation of ${opp.user} (HF=${opp.health_factor.toFixed(4)})…`
      );
      await bot.liquidate.staticCall(params);
      console.log("Simulation OK – submitting tx");

      const tx = await bot.liquidate(params, { gasLimit: 800_000n });
      console.log("Tx hash:", tx.hash);
      const receipt = await tx.wait();
      console.log("Confirmed in block", receipt?.blockNumber);
    } catch (err: any) {
      console.error(`Failed for ${opp.user}:`, err.shortMessage || err.message);
    }
  }

  console.log("Cycle complete");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
