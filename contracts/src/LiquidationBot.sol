// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IPool} from "@aave/core-v3/contracts/interfaces/IPool.sol";
import {IPoolAddressesProvider} from "@aave/core-v3/contracts/interfaces/IPoolAddressesProvider.sol";
import {IFlashLoanSimpleReceiver} from "@aave/core-v3/contracts/flashloan/interfaces/IFlashLoanSimpleReceiver.sol";

interface IBalancerVault {
    function flashLoan(
        address recipient,
        IERC20[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external;
}

interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params)
        external
        payable
        returns (uint256 amountOut);
}

/**
 * @title LiquidationBot
 * @notice Atomic flash-loan liquidator for Aave V3.
 *         Prefers Balancer V2 (0% fee). Falls back to Aave V3 flashLoanSimple.
 *
 * Flow (single tx):
 *  1. Owner calls liquidate() → triggers flash loan (Balancer or Aave)
 *  2. Callback receives debtAsset
 *  3. Approve + liquidationCall on Aave Pool → receive collateral (+ bonus)
 *  4. Swap collateral → debtAsset via Uniswap V3
 *  5. Repay flash loan (+ premium if Aave)
 *  6. Remaining profit is sent to owner (deployer)
 */
contract LiquidationBot is IFlashLoanSimpleReceiver, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    IPoolAddressesProvider public immutable ADDRESSES_PROVIDER_;
    IPool public immutable POOL_;
    IBalancerVault public immutable BALANCER_VAULT;
    address public immutable DEPLOYER;

    mapping(address => bool) public approvedRouters;
    uint256 public minProfitWei;
    bool public paused;

    event LiquidationExecuted(
        address indexed user,
        address indexed collateralAsset,
        address indexed debtAsset,
        uint256 debtCovered,
        uint256 collateralReceived,
        uint256 profit,
        bool usedBalancer
    );
    event ProfitWithdrawn(address token, uint256 amount, address to);
    event RouterApproved(address router, bool approved);
    event MinProfitUpdated(uint256 newMin);
    event Paused(bool status);

    error Unauthorized();
    error ContractPaused();
    error UnapprovedRouter();
    error InsufficientProfit(uint256 actual, uint256 required);
    error InvalidFlashLoanInitiator();
    error ZeroAddress();
    error SwapFailed();

    struct LiquidationParams {
        address collateralAsset;
        address debtAsset;
        address user;
        uint256 debtToCover;
        address swapRouter;
        uint24 poolFee;
        uint256 minCollateralOut;
        uint256 minDebtOutAfterSwap;
        bool useBalancer;
    }

    constructor(
        address addressesProvider,
        address balancerVault,
        address initialOwner
    ) Ownable(initialOwner) {
        if (addressesProvider == address(0) || balancerVault == address(0)) {
            revert ZeroAddress();
        }
        ADDRESSES_PROVIDER_ = IPoolAddressesProvider(addressesProvider);
        POOL_ = IPool(ADDRESSES_PROVIDER_.getPool());
        BALANCER_VAULT = IBalancerVault(balancerVault);
        DEPLOYER = initialOwner;
        minProfitWei = 0;
    }

    function setApprovedRouter(address router, bool approved) external onlyOwner {
        approvedRouters[router] = approved;
        emit RouterApproved(router, approved);
    }

    function setMinProfit(uint256 _minProfitWei) external onlyOwner {
        minProfitWei = _minProfitWei;
        emit MinProfitUpdated(_minProfitWei);
    }

    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit Paused(_paused);
    }

    function withdraw(address token, uint256 amount) external onlyOwner {
        IERC20(token).safeTransfer(DEPLOYER, amount);
        emit ProfitWithdrawn(token, amount, DEPLOYER);
    }

    function liquidate(LiquidationParams calldata params)
        external
        onlyOwner
        nonReentrant
    {
        if (paused) revert ContractPaused();
        if (!approvedRouters[params.swapRouter]) revert UnapprovedRouter();
        if (
            params.user == address(0) ||
            params.collateralAsset == address(0) ||
            params.debtAsset == address(0)
        ) {
            revert ZeroAddress();
        }

        bytes memory encoded = abi.encode(params);

        if (params.useBalancer) {
            IERC20[] memory tokens = new IERC20[](1);
            tokens[0] = IERC20(params.debtAsset);
            uint256[] memory amounts = new uint256[](1);
            amounts[0] = params.debtToCover;
            BALANCER_VAULT.flashLoan(address(this), tokens, amounts, encoded);
        } else {
            POOL_.flashLoanSimple(
                address(this),
                params.debtAsset,
                params.debtToCover,
                encoded,
                0
            );
        }
    }

    function receiveFlashLoan(
        IERC20[] memory,
        uint256[] memory amounts,
        uint256[] memory,
        bytes memory userData
    ) external {
        if (msg.sender != address(BALANCER_VAULT)) revert Unauthorized();
        LiquidationParams memory p = abi.decode(userData, (LiquidationParams));
        _executeLiquidation(p, amounts[0], 0, true);
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        if (msg.sender != address(POOL_)) revert Unauthorized();
        if (initiator != address(this)) revert InvalidFlashLoanInitiator();

        LiquidationParams memory p = abi.decode(params, (LiquidationParams));
        require(asset == p.debtAsset, "asset mismatch");
        require(amount == p.debtToCover, "amount mismatch");

        _executeLiquidation(p, amount, premium, false);
        return true;
    }

    function _executeLiquidation(
        LiquidationParams memory p,
        uint256 borrowed,
        uint256 premium,
        bool usedBalancer
    ) internal {
        uint256 collBefore = IERC20(p.collateralAsset).balanceOf(address(this));

        IERC20(p.debtAsset).forceApprove(address(POOL_), borrowed);

        POOL_.liquidationCall(
            p.collateralAsset,
            p.debtAsset,
            p.user,
            p.debtToCover,
            false
        );

        uint256 collReceived =
            IERC20(p.collateralAsset).balanceOf(address(this)) - collBefore;
        if (collReceived < p.minCollateralOut) {
            revert InsufficientProfit(collReceived, p.minCollateralOut);
        }

        uint256 debtBeforeSwap = IERC20(p.debtAsset).balanceOf(address(this));
        _swap(
            p.swapRouter,
            p.collateralAsset,
            p.debtAsset,
            collReceived,
            p.poolFee,
            p.minDebtOutAfterSwap
        );
        uint256 debtAfterSwap = IERC20(p.debtAsset).balanceOf(address(this));

        uint256 totalOwed = borrowed + premium;
        if (debtAfterSwap < totalOwed) {
            revert InsufficientProfit(debtAfterSwap, totalOwed);
        }

        if (usedBalancer) {
            IERC20(p.debtAsset).safeTransfer(address(BALANCER_VAULT), borrowed);
        } else {
            IERC20(p.debtAsset).forceApprove(address(POOL_), totalOwed);
        }

        uint256 profit = IERC20(p.debtAsset).balanceOf(address(this));
        if (profit < minProfitWei) {
            revert InsufficientProfit(profit, minProfitWei);
        }

        if (profit > 0) {
            IERC20(p.debtAsset).safeTransfer(DEPLOYER, profit);
        }

        emit LiquidationExecuted(
            p.user,
            p.collateralAsset,
            p.debtAsset,
            borrowed,
            collReceived,
            profit,
            usedBalancer
        );
    }

    function _swap(
        address router,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint24 fee,
        uint256 amountOutMinimum
    ) internal {
        IERC20(tokenIn).forceApprove(router, amountIn);

        ISwapRouter.ExactInputSingleParams memory swapParams = ISwapRouter
            .ExactInputSingleParams({
                tokenIn: tokenIn,
                tokenOut: tokenOut,
                fee: fee,
                recipient: address(this),
                deadline: block.timestamp,
                amountIn: amountIn,
                amountOutMinimum: amountOutMinimum,
                sqrtPriceLimitX96: 0
            });

        uint256 amountOut = ISwapRouter(router).exactInputSingle(swapParams);
        if (amountOut < amountOutMinimum) revert SwapFailed();
    }

    function ADDRESSES_PROVIDER()
        external
        view
        override
        returns (IPoolAddressesProvider)
    {
        return ADDRESSES_PROVIDER_;
    }

    function POOL() external view override returns (IPool) {
        return POOL_;
    }

    receive() external payable {}
}
