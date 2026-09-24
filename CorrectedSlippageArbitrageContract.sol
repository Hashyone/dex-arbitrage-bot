// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/**
 * @title CorrectedSlippageArbitrageContract
 * @notice Balancer-flashloan arbitrage: QuickSwap V2, SushiSwap, Uniswap V3,
 *         Curve, 1inch, and Algebra (QuickSwap V3) routers.
 *
 * Security: executeArbitrage is onlyOwner — only the deployer wallet can
 *           initiate trades. Profit is forwarded to tx.origin (== owner
 *           when called directly; safe because onlyOwner enforces it).
 */

// ─── External interfaces ────────────────────────────────────────────────────

interface IBalancerVault {
    function flashLoan(
        address recipient,
        address[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external;
}

interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

interface IUniswapV3Router {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params)
        external returns (uint256 amountOut);
}

/// @dev Algebra (QuickSwap V3) SwapRouter — same call signature as Uniswap V3
///      EXCEPT: no `fee` field; uses `limitSqrtPrice` instead of `sqrtPriceLimitX96`.
interface IAlgebraRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 limitSqrtPrice;
    }
    function exactInputSingle(ExactInputSingleParams calldata params)
        external payable returns (uint256 amountOut);
}

interface ICurvePool {
    function exchange(int128 i, int128 j, uint256 dx, uint256 min_dy)
        external returns (uint256);
    function exchange_underlying(int128 i, int128 j, uint256 dx, uint256 min_dy)
        external returns (uint256);
}

// ─── Main contract ──────────────────────────────────────────────────────────

contract CorrectedSlippageArbitrageContract is Ownable, ReentrancyGuard {

    // ── Enums & Structs ──────────────────────────────────────────────────

    enum RouterType {
        UNISWAP_V2,   // 0
        UNISWAP_V3,   // 1
        SUSHISWAP,    // 2
        QUICKSWAP,    // 3
        CURVE,        // 4
        ONE_INCH,     // 5
        ALGEBRA       // 6  ← QuickSwap V3 / Algebra
    }

    struct SwapParams {
        address tokenIn;
        address tokenOut;
        address router;
        RouterType routerType;
        uint256 expectedAmountOut;
        uint256 minAmountOut;
        uint256 maxSlippageBps;
        uint24  fee;               // V3 only; ignored for Algebra
        int128  curveTokenInIndex;
        int128  curveTokenOutIndex;
        bool    useUnderlying;
        bytes   oneInchData;
    }

    struct ArbitrageParams {
        address     flashloanToken;
        uint256     flashloanAmount;
        SwapParams[] swaps;
        uint256     expectedProfit;
        uint256     deadline;
    }

    // Memory-safe copy (no dynamic bytes — avoids stack-corruption with
    // large calldata structs)
    struct SwapData {
        address    tokenIn;
        address    tokenOut;
        address    router;
        RouterType routerType;
        uint256    expectedAmountOut;
        uint256    minAmountOut;
        uint256    maxSlippageBps;
        uint24     fee;
        int128     curveTokenInIndex;
        int128     curveTokenOutIndex;
        bool       useUnderlying;
    }

    // ── State ────────────────────────────────────────────────────────────

    IBalancerVault public constant BALANCER_VAULT =
        IBalancerVault(0xBA12222222228d8Ba445958a75a0704d566BF2C8);

    address public constant ONE_INCH_ROUTER =
        0x111111125421cA6dc452d289314280a0f8842A65;

    /// @notice QuickSwap V3 (Algebra) SwapRouter on Polygon mainnet
    address public constant ALGEBRA_ROUTER =
        0xf5b509bB0909a69B1c207E495f687a596C168E12;

    mapping(address => bool) public approvedCurvePools;

    // ── Events ───────────────────────────────────────────────────────────

    event ArbitrageCompleted(
        address indexed flashloanToken,
        uint256 flashloanAmount,
        uint256 profit,
        address indexed executor
    );
    event ArbitrageFailed(
        address indexed flashloanToken,
        uint256 flashloanAmount,
        uint256 finalAmount,
        uint256 shortfall,
        string  reason
    );
    event SwapExecuted(
        address indexed tokenIn,
        address indexed tokenOut,
        address indexed router,
        uint256 amountIn,
        uint256 amountOut,
        RouterType routerType
    );
    event CurveSwapExecuted(
        address indexed pool,
        int128 tokenInIndex,
        int128 tokenOutIndex,
        uint256 amountIn,
        uint256 amountOut,
        bool    useUnderlying
    );
    event OneInchSwapExecuted(address indexed tokenIn, address indexed tokenOut,
        uint256 amountIn, uint256 amountOut);
    event OneInchSwapFailed(address indexed tokenIn, address indexed tokenOut,
        uint256 amountIn, string reason);
    event CurvePoolAdded(address indexed pool);
    event CurvePoolRemoved(address indexed pool);

    // ── Constructor ──────────────────────────────────────────────────────

    constructor() Ownable(msg.sender) {
        approvedCurvePools[0x445FE580eF8d70FF569aB36e80c647af338db351] = true;
        approvedCurvePools[0x5B082Cb0a4C4b7FD71B5E98803A74AdA0beA5cD6] = true;
        approvedCurvePools[0x3A43a5851a3EaFa49A4e3fdC51B7D7eB623fef78] = true;
        approvedCurvePools[0xE7a24EF0C5e95Ffb0f6684b813A78F2a3AD7D171] = true;
        approvedCurvePools[0x751b1e21756bdBC307cbcC5084C042b6266a1d25] = true;
    }

    // ── Main entry point (SECURED) ────────────────────────────────────────

    /**
     * @notice Initiate a flashloan-backed arbitrage.
     * @dev    SECURED: onlyOwner — only the bot wallet can call this.
     *         This prevents front-runners and random callers from stealing
     *         the opportunity or draining profit.
     * @param  data ABI-encoded ArbitrageParams struct.
     */
    function executeArbitrage(bytes calldata data)
        external
        onlyOwner
        nonReentrant
    {
        ArbitrageParams memory params = abi.decode(data, (ArbitrageParams));

        require(params.deadline >= block.timestamp, "Deadline expired");
        require(params.swaps.length > 0, "No swaps provided");
        require(params.flashloanAmount > 0, "Invalid flashloan amount");

        address[] memory tokens  = new address[](1);
        uint256[] memory amounts = new uint256[](1);
        tokens[0]  = params.flashloanToken;
        amounts[0] = params.flashloanAmount;

        BALANCER_VAULT.flashLoan(address(this), tokens, amounts, data);
    }

    // ── Balancer flash-loan callback ──────────────────────────────────────

    function receiveFlashLoan(
        address[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes    memory userData
    ) external {
        require(msg.sender == address(BALANCER_VAULT), "Only Balancer Vault");
        require(tokens.length  == 1, "Single-token flashloans only");
        require(amounts.length == 1, "Invalid amounts array");

        ArbitrageParams memory params = abi.decode(userData, (ArbitrageParams));

        uint256 flashloanAmount = amounts[0];
        address flashloanToken  = tokens[0];

        uint256 swapCount = params.swaps.length;

        // Copy to memory-safe structs (prevents memory corruption with
        // dynamic-bytes fields in SwapParams)
        SwapData[] memory swapData       = new SwapData[](swapCount);
        bytes[]    memory oneInchDataArr = new bytes[](swapCount);

        for (uint256 i = 0; i < swapCount; i++) {
            SwapParams memory s = params.swaps[i];
            swapData[i] = SwapData({
                tokenIn:           s.tokenIn,
                tokenOut:          s.tokenOut,
                router:            s.router,
                routerType:        s.routerType,
                expectedAmountOut: s.expectedAmountOut,
                minAmountOut:      s.minAmountOut,
                maxSlippageBps:    s.maxSlippageBps,
                fee:               s.fee,
                curveTokenInIndex: s.curveTokenInIndex,
                curveTokenOutIndex:s.curveTokenOutIndex,
                useUnderlying:     s.useUnderlying
            });
            oneInchDataArr[i] = s.oneInchData;
        }

        uint256 currentAmount = flashloanAmount;

        for (uint256 i = 0; i < swapCount; i++) {
            SwapData memory swap = swapData[i];
            RouterType rt = swap.routerType;

            if (rt == RouterType.ONE_INCH) {
                currentAmount = _executeOneInchSwap(
                    swap.tokenIn, swap.tokenOut, oneInchDataArr[i], currentAmount
                );
            } else if (
                rt == RouterType.UNISWAP_V2 ||
                rt == RouterType.SUSHISWAP   ||
                rt == RouterType.QUICKSWAP
            ) {
                currentAmount = _executeV2Swap(swap, currentAmount, params.deadline);
            } else if (rt == RouterType.UNISWAP_V3) {
                currentAmount = _executeV3Swap(swap, currentAmount, params.deadline);
            } else if (rt == RouterType.ALGEBRA) {
                currentAmount = _executeAlgebraSwap(swap, currentAmount, params.deadline);
            } else if (rt == RouterType.CURVE) {
                currentAmount = _executeCurveSwap(swap, currentAmount);
            } else {
                revert("Unsupported router type");
            }
        }

        // ── Profitability check (Bachini-style: only at end) ─────────────
        uint256 repayAmount = flashloanAmount + feeAmounts[0];
        uint256 balanceAfter = IERC20(flashloanToken).balanceOf(address(this));

        if (balanceAfter < repayAmount) {
            uint256 shortfall = repayAmount - balanceAfter;
            emit ArbitrageFailed(
                flashloanToken, flashloanAmount, balanceAfter, shortfall,
                "Insufficient balance for flashloan repayment"
            );
            revert("Insufficient balance for flashloan repayment");
        }

        uint256 profit = balanceAfter - repayAmount;

        IERC20(flashloanToken).transfer(address(BALANCER_VAULT), repayAmount);

        if (profit > 0) {
            // tx.origin == owner() because executeArbitrage is onlyOwner
            IERC20(flashloanToken).transfer(tx.origin, profit);
        }

        emit ArbitrageCompleted(flashloanToken, flashloanAmount, profit, tx.origin);
    }

    // ── Swap implementations ─────────────────────────────────────────────

    function _executeV2Swap(
        SwapData memory swap,
        uint256 amountIn,
        uint256 deadline
    ) internal returns (uint256) {
        require(swap.router != address(0), "Invalid V2 router");
        require(amountIn > 0, "Invalid amountIn");

        IERC20(swap.tokenIn).approve(swap.router, 0);
        IERC20(swap.tokenIn).approve(swap.router, amountIn);

        address[] memory path = new address[](2);
        path[0] = swap.tokenIn;
        path[1] = swap.tokenOut;

        uint256[] memory amounts = IUniswapV2Router(swap.router)
            .swapExactTokensForTokens(amountIn, 1, path, address(this), deadline);

        require(amounts[1] > 0, "V2 swap returned zero");
        emit SwapExecuted(swap.tokenIn, swap.tokenOut, swap.router,
            amountIn, amounts[1], swap.routerType);
        return amounts[1];
    }

    function _executeV3Swap(
        SwapData memory swap,
        uint256 amountIn,
        uint256 deadline
    ) internal returns (uint256) {
        require(swap.router != address(0), "Invalid V3 router");
        require(swap.fee > 0, "V3 swap requires fee > 0");
        require(amountIn > 0, "Invalid amountIn");

        IERC20(swap.tokenIn).approve(swap.router, 0);
        IERC20(swap.tokenIn).approve(swap.router, amountIn);

        IUniswapV3Router.ExactInputSingleParams memory p =
            IUniswapV3Router.ExactInputSingleParams({
                tokenIn:            swap.tokenIn,
                tokenOut:           swap.tokenOut,
                fee:                swap.fee,
                recipient:          address(this),
                deadline:           deadline,
                amountIn:           amountIn,
                amountOutMinimum:   1,
                sqrtPriceLimitX96:  0
            });

        uint256 amountOut = IUniswapV3Router(swap.router).exactInputSingle(p);
        require(amountOut > 0, "V3 swap returned zero");
        emit SwapExecuted(swap.tokenIn, swap.tokenOut, swap.router,
            amountIn, amountOut, swap.routerType);
        return amountOut;
    }

    /**
     * @notice Execute a swap via the Algebra (QuickSwap V3) router.
     * @dev    Algebra's interface omits the `fee` field — the pool fee is
     *         dynamic and embedded in the pool itself, not the router call.
     *         We pass limitSqrtPrice=0 (no price limit).
     */
    function _executeAlgebraSwap(
        SwapData memory swap,
        uint256 amountIn,
        uint256 deadline
    ) internal returns (uint256) {
        require(amountIn > 0, "Invalid amountIn");

        IERC20(swap.tokenIn).approve(ALGEBRA_ROUTER, 0);
        IERC20(swap.tokenIn).approve(ALGEBRA_ROUTER, amountIn);

        IAlgebraRouter.ExactInputSingleParams memory p =
            IAlgebraRouter.ExactInputSingleParams({
                tokenIn:           swap.tokenIn,
                tokenOut:          swap.tokenOut,
                recipient:         address(this),
                deadline:          deadline,
                amountIn:          amountIn,
                amountOutMinimum:  1,
                limitSqrtPrice:    0
            });

        uint256 amountOut = IAlgebraRouter(ALGEBRA_ROUTER).exactInputSingle(p);
        require(amountOut > 0, "Algebra swap returned zero");
        emit SwapExecuted(swap.tokenIn, swap.tokenOut, ALGEBRA_ROUTER,
            amountIn, amountOut, RouterType.ALGEBRA);
        return amountOut;
    }

    function _executeCurveSwap(
        SwapData memory swap,
        uint256 amountIn
    ) internal returns (uint256) {
        require(approvedCurvePools[swap.router], "Curve pool not approved");
        require(swap.router != address(0), "Invalid Curve pool");
        require(amountIn > 0, "Invalid amountIn");

        IERC20(swap.tokenIn).approve(swap.router, 0);
        IERC20(swap.tokenIn).approve(swap.router, amountIn);

        uint256 balanceBefore = IERC20(swap.tokenOut).balanceOf(address(this));
        uint256 amountOut;

        if (swap.useUnderlying) {
            amountOut = ICurvePool(swap.router).exchange_underlying(
                swap.curveTokenInIndex, swap.curveTokenOutIndex, amountIn, 1
            );
        } else {
            amountOut = ICurvePool(swap.router).exchange(
                swap.curveTokenInIndex, swap.curveTokenOutIndex, amountIn, 1
            );
        }

        uint256 balanceAfter = IERC20(swap.tokenOut).balanceOf(address(this));
        amountOut = balanceAfter - balanceBefore;

        require(amountOut > 0, "Curve swap returned zero");
        emit CurveSwapExecuted(swap.router, swap.curveTokenInIndex,
            swap.curveTokenOutIndex, amountIn, amountOut, swap.useUnderlying);
        return amountOut;
    }

    function _executeOneInchSwap(
        address tokenIn,
        address tokenOut,
        bytes memory oneInchData,
        uint256 amountIn
    ) internal returns (uint256) {
        require(oneInchData.length > 0, "1inch data required");
        require(tokenIn != address(0) && tokenOut != address(0), "Invalid token");
        require(amountIn > 0, "Invalid amountIn");

        IERC20(tokenIn).approve(ONE_INCH_ROUTER, 0);
        IERC20(tokenIn).approve(ONE_INCH_ROUTER, amountIn);

        uint256 balanceBefore = IERC20(tokenOut).balanceOf(address(this));
        (bool success, bytes memory returnData) = ONE_INCH_ROUTER.call(oneInchData);

        if (!success) {
            string memory reason = "1inch call reverted";
            if (returnData.length > 68) {
                assembly { returnData := add(returnData, 0x04) }
                reason = abi.decode(returnData, (string));
            }
            emit OneInchSwapFailed(tokenIn, tokenOut, amountIn, reason);
            revert(reason);
        }

        uint256 balanceAfter = IERC20(tokenOut).balanceOf(address(this));
        uint256 actualOut = balanceAfter - balanceBefore;
        require(actualOut > 0, "1inch swap returned zero");
        emit OneInchSwapExecuted(tokenIn, tokenOut, amountIn, actualOut);
        return actualOut;
    }

    // ── Admin ────────────────────────────────────────────────────────────

    function addCurvePool(address pool) external onlyOwner {
        require(pool != address(0), "Invalid pool");
        approvedCurvePools[pool] = true;
        emit CurvePoolAdded(pool);
    }

    function removeCurvePool(address pool) external onlyOwner {
        require(pool != address(0), "Invalid pool");
        approvedCurvePools[pool] = false;
        emit CurvePoolRemoved(pool);
    }

    function addMultipleCurvePools(address[] calldata pools) external onlyOwner {
        for (uint256 i = 0; i < pools.length; i++) {
            require(pools[i] != address(0), "Invalid pool");
            approvedCurvePools[pools[i]] = true;
            emit CurvePoolAdded(pools[i]);
        }
    }

    function emergencyWithdraw(address token) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        if (bal > 0) IERC20(token).transfer(owner(), bal);
    }

    function emergencyWithdrawETH() external onlyOwner {
        uint256 bal = address(this).balance;
        if (bal > 0) payable(owner()).transfer(bal);
    }

    function isCurvePoolApproved(address pool) external view returns (bool) {
        return approvedCurvePools[pool];
    }

    function getContractBalance(address token) external view returns (uint256) {
        return IERC20(token).balanceOf(address(this));
    }

    receive() external payable {}
}
