// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Script, console2} from "forge-std/Script.sol";
import {LiquidationBot} from "../src/LiquidationBot.sol";

contract Deploy is Script {
    function run() external {
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
        address addressesProvider = vm.envAddress("AAVE_ADDRESSES_PROVIDER");
        address balancerVault = vm.envAddress("BALANCER_VAULT");

        vm.startBroadcast(deployerPrivateKey);

        LiquidationBot bot = new LiquidationBot(
            addressesProvider,
            balancerVault,
            msg.sender
        );

        console2.log("LiquidationBot deployed at:", address(bot));
        console2.log("Owner / profit receiver:", msg.sender);

        // Uniswap V3 SwapRouter (common on many L2s)
        address uniV3Router = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
        bot.setApprovedRouter(uniV3Router, true);
        console2.log("Approved Uniswap V3 router:", uniV3Router);

        vm.stopBroadcast();
    }
}
