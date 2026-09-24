// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

interface IBalancerVault {
    function flashLoan(
        address recipient,
        address[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external;
}

interface IAavePool {
    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;

    function getUserAccountData(address user)
        external
        view
        returns (
            uint256 totalCollateralBase,
            uint256 totalDebtBase,
            uint256 availableBorrowsBase,
            uint256 currentLiquidationThreshold,
            uint256 ltv,
            uint256 healthFactor
        );
}

interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);

    function getAmountsOut(uint256 amountIn, address[] calldata path)
        external
        view
        returns (uint256[] memory amounts);
}

interface IUniswapV3Router {
    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }

    function exactInput(ExactInputParams calldata params) external returns (uint256 amountOut);
}

contract AaveLiquidationContract is Ownable, ReentrancyGuard {
    enum SwapRouterType { QUICKSWAP_V2, SUSHISWAP, UNISWAP_V3, ONE_INCH }

    struct LiquidationParams {
        address collateralAsset;
        address debtAsset;
        address user;
        uint256 debtToCover;
        uint256 minCollateralReceived;
        address[] swapPath;
        address swapRouter;
        SwapRouterType routerType;
        uint24[] uniswapV3Fees;
        bytes oneInchData;
        uint256 deadline;
        uint256 minProfitRequired;
    }

    IBalancerVault public constant BALANCER_VAULT = IBalancerVault(0xBA12222222228d8Ba445958a75a0704d566BF2C8);
    IAavePool public constant AAVE_POOL = IAavePool(0x794a61358D6845594F94dc1DB02A252b5b4814aD);

    address public constant QUICKSWAP_ROUTER = 0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff;
    address public constant SUSHISWAP_ROUTER = 0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506;
    address public constant UNISWAP_V3_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address public constant ONE_INCH_ROUTER = 0x111111125421cA6dc452d289314280a0f8842A65;

    address public constant WETH = 0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619;
    address public constant WMATIC = 0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270;
    address public constant USDC = 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174;

    mapping(address => bool) public approvedTokens;
    mapping(address => bool) public approvedRouters;

    uint256 public globalMinProfit = 10 * 1e6;
    uint256 public maxGasPrice = 500 gwei;
    bool public paused = false;

    event LiquidationExecuted(
        address indexed user,
        address indexed collateralAsset,
        address indexed debtAsset,
        uint256 debtCovered,
        uint256 collateralReceived,
        uint256 profit
    );

    event LiquidationFailed(
        address indexed user,
        string reason
    );

    event TokenApproved(address indexed token, bool approved);
    event RouterApproved(address indexed router, bool approved);
    event EmergencyWithdraw(address indexed token, uint256 amount);
    event ParametersUpdated(uint256 minProfit, uint256 maxGasPrice);

    error Paused();
    error InvalidDeadline();
    error RouterNotApproved();
    error PositionHealthy();
    error InsufficientCollateral();
    error InsufficientProfit(uint256 actual, uint256 required);
    error FlashloanRepaymentFailed();
    error UnauthorizedCaller();
    error InvalidSwapPath();
    error GasPriceTooHigh(uint256 current, uint256 max);

    modifier whenNotPaused() {
        if (paused) revert Paused();
        _;
    }

    constructor() Ownable(msg.sender) {
        approvedRouters[QUICKSWAP_ROUTER] = true;
        approvedRouters[SUSHISWAP_ROUTER] = true;
        approvedRouters[UNISWAP_V3_ROUTER] = true;
        approvedRouters[ONE_INCH_ROUTER] = true;

        approvedTokens[WETH] = true;
        approvedTokens[WMATIC] = true;
        approvedTokens[USDC] = true;
    }

    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
    }

    function setGlobalMinProfit(uint256 _minProfit) external onlyOwner {
        globalMinProfit = _minProfit;
        emit ParametersUpdated(_minProfit, maxGasPrice);
    }

    function setMaxGasPrice(uint256 _maxGasPrice) external onlyOwner {
        maxGasPrice = _maxGasPrice;
        emit ParametersUpdated(globalMinProfit, _maxGasPrice);
    }

    function setTokenApproval(address token, bool approved) external onlyOwner {
        approvedTokens[token] = approved;
        emit TokenApproved(token, approved);
    }

    function setRouterApproval(address router, bool approved) external onlyOwner {
        approvedRouters[router] = approved;
        emit RouterApproved(router, approved);
    }

    function batchApproveTokens(address[] calldata tokens) external onlyOwner {
        for (uint256 i = 0; i < tokens.length; i++) {
            approvedTokens[tokens[i]] = true;
            emit TokenApproved(tokens[i], true);
        }
    }

    function batchApproveForAave(address[] calldata tokens) external onlyOwner {
        for (uint256 i = 0; i < tokens.length; i++) {
            IERC20(tokens[i]).approve(address(AAVE_POOL), type(uint256).max);
            approvedTokens[tokens[i]] = true;
        }
    }

    function batchApproveForRouter(address[] calldata tokens, address router) external onlyOwner {
        if (!approvedRouters[router]) revert RouterNotApproved();
        for (uint256 i = 0; i < tokens.length; i++) {
            IERC20(tokens[i]).approve(router, type(uint256).max);
        }
    }

    function executeLiquidation(bytes calldata params) external nonReentrant whenNotPaused {
        if (tx.gasprice > maxGasPrice) revert GasPriceTooHigh(tx.gasprice, maxGasPrice);

        LiquidationParams memory liqParams = abi.decode(params, (LiquidationParams));

        if (block.timestamp > liqParams.deadline) revert InvalidDeadline();
        if (!approvedRouters[liqParams.swapRouter]) revert RouterNotApproved();
        if (liqParams.swapPath.length < 2) revert InvalidSwapPath();
        if (liqParams.swapPath[0] != liqParams.collateralAsset) revert InvalidSwapPath();
        if (liqParams.swapPath[liqParams.swapPath.length - 1] != liqParams.debtAsset) revert InvalidSwapPath();

        (, , , , , uint256 healthFactor) = AAVE_POOL.getUserAccountData(liqParams.user);
        if (healthFactor >= 1e18) revert PositionHealthy();

        address[] memory tokens = new address[](1);
        tokens[0] = liqParams.debtAsset;

        uint256[] memory amounts = new uint256[](1);
        amounts[0] = liqParams.debtToCover;

        BALANCER_VAULT.flashLoan(
            address(this),
            tokens,
            amounts,
            abi.encode(liqParams)
        );
    }

    function receiveFlashLoan(
        address[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes memory userData
    ) external {
        if (msg.sender != address(BALANCER_VAULT)) revert UnauthorizedCaller();

        LiquidationParams memory liqParams = abi.decode(userData, (LiquidationParams));

        address debtAsset = tokens[0];
        uint256 debtAmount = amounts[0];
        uint256 flashloanFee = feeAmounts[0];
        uint256 totalRepayment = debtAmount + flashloanFee;

        _ensureApproval(debtAsset, address(AAVE_POOL), debtAmount);

        uint256 collateralBefore = IERC20(liqParams.collateralAsset).balanceOf(address(this));

        try AAVE_POOL.liquidationCall(
            liqParams.collateralAsset,
            debtAsset,
            liqParams.user,
            debtAmount,
            false
        ) {
            uint256 collateralReceived = IERC20(liqParams.collateralAsset).balanceOf(address(this)) - collateralBefore;

            if (collateralReceived < liqParams.minCollateralReceived) {
                revert InsufficientCollateral();
            }

            uint256 debtBalanceBefore = IERC20(debtAsset).balanceOf(address(this));

            if (liqParams.collateralAsset != debtAsset) {
                _swapCollateralForDebt(liqParams, collateralReceived);
            }

            uint256 debtBalanceAfter = IERC20(debtAsset).balanceOf(address(this));

            if (debtBalanceAfter < totalRepayment) {
                revert FlashloanRepaymentFailed();
            }

            _ensureApproval(debtAsset, address(BALANCER_VAULT), totalRepayment);
            IERC20(debtAsset).transfer(address(BALANCER_VAULT), totalRepayment);

            uint256 debtProfit = IERC20(debtAsset).balanceOf(address(this));
            uint256 collateralRemaining = IERC20(liqParams.collateralAsset).balanceOf(address(this));

            uint256 effectiveProfit = debtProfit;

            if (effectiveProfit < liqParams.minProfitRequired) {
                revert InsufficientProfit(effectiveProfit, liqParams.minProfitRequired);
            }

            if (debtProfit > 0) {
                IERC20(debtAsset).transfer(owner(), debtProfit);
            }

            if (collateralRemaining > 0) {
                IERC20(liqParams.collateralAsset).transfer(owner(), collateralRemaining);
            }

            emit LiquidationExecuted(
                liqParams.user,
                liqParams.collateralAsset,
                debtAsset,
                debtAmount,
                collateralReceived,
                effectiveProfit
            );
        } catch Error(string memory reason) {
            emit LiquidationFailed(liqParams.user, reason);
            revert(reason);
        } catch {
            emit LiquidationFailed(liqParams.user, "Unknown error");
            revert("Liquidation failed");
        }
    }

    function _swapCollateralForDebt(
        LiquidationParams memory params,
        uint256 collateralAmount
    ) internal {
        _ensureApproval(params.collateralAsset, params.swapRouter, collateralAmount);

        if (params.routerType == SwapRouterType.QUICKSWAP_V2 || params.routerType == SwapRouterType.SUSHISWAP) {
            IUniswapV2Router router = IUniswapV2Router(params.swapRouter);

            uint256[] memory expectedAmounts = router.getAmountsOut(collateralAmount, params.swapPath);
            uint256 minOut = (expectedAmounts[expectedAmounts.length - 1] * 97) / 100;

            router.swapExactTokensForTokens(
                collateralAmount,
                minOut,
                params.swapPath,
                address(this),
                params.deadline
            );
        } else if (params.routerType == SwapRouterType.UNISWAP_V3) {
            bytes memory path = _encodeUniswapV3Path(params.swapPath, params.uniswapV3Fees);

            IUniswapV3Router.ExactInputParams memory swapParams = IUniswapV3Router.ExactInputParams({
                path: path,
                recipient: address(this),
                deadline: params.deadline,
                amountIn: collateralAmount,
                amountOutMinimum: 0
            });

            IUniswapV3Router(params.swapRouter).exactInput(swapParams);
        } else if (params.routerType == SwapRouterType.ONE_INCH) {
            require(params.oneInchData.length > 0, "1inch data required");

            (bool success, bytes memory returnData) = params.swapRouter.call(params.oneInchData);
            require(success, string(returnData));
        }
    }

    function _encodeUniswapV3Path(address[] memory path, uint24[] memory fees) internal pure returns (bytes memory) {
        require(path.length >= 2, "Invalid path length");
        require(fees.length == path.length - 1, "Invalid fees length");

        bytes memory encodedPath = abi.encodePacked(path[0]);

        for (uint256 i = 0; i < fees.length; i++) {
            encodedPath = abi.encodePacked(encodedPath, fees[i], path[i + 1]);
        }

        return encodedPath;
    }

    function _ensureApproval(address token, address spender, uint256 amount) internal {
        uint256 currentAllowance = IERC20(token).allowance(address(this), spender);
        if (currentAllowance < amount) {
            if (currentAllowance > 0) {
                IERC20(token).approve(spender, 0);
            }
            IERC20(token).approve(spender, type(uint256).max);
        }
    }

    function simulateLiquidation(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover
    ) external view returns (
        bool canLiquidate,
        uint256 healthFactor,
        uint256 totalCollateral,
        uint256 totalDebt
    ) {
        (totalCollateral, totalDebt, , , , healthFactor) = AAVE_POOL.getUserAccountData(user);
        canLiquidate = healthFactor < 1e18;

        return (canLiquidate, healthFactor, totalCollateral, totalDebt);
    }

    function checkUserHealth(address user) external view returns (
        uint256 totalCollateralBase,
        uint256 totalDebtBase,
        uint256 healthFactor,
        bool canBeLiquidated
    ) {
        (totalCollateralBase, totalDebtBase, , , , healthFactor) = AAVE_POOL.getUserAccountData(user);
        canBeLiquidated = healthFactor < 1e18;
    }

    function emergencyWithdraw(address token) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        if (balance > 0) {
            IERC20(token).transfer(owner(), balance);
            emit EmergencyWithdraw(token, balance);
        }
    }

    function emergencyWithdrawETH() external onlyOwner {
        uint256 balance = address(this).balance;
        if (balance > 0) {
            payable(owner()).transfer(balance);
        }
    }

    receive() external payable {}
}
