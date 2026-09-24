// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Minimal interface for MultiProtocolHybridBot – used in tests only.
interface IHybridBot {
    // ── Admin ────────────────────────────────────────────────────────
    function owner() external view returns (address);
    function paused() external view returns (bool);
    function setPaused(bool _paused) external;
    function maxGasPrice() external view returns (uint256);
    function setMaxGasPrice(uint256 _max) external;

    // ── Token approvals ──────────────────────────────────────────────
    function batchApproveTokensForSpender(
        address[] calldata tokens,
        address spender
    ) external;
    function approveToken(
        address token,
        address spender,
        uint256 amount
    ) external;
    function approveTokenForAllSpenders(address token) external;

    // ── Health checks (view) ─────────────────────────────────────────
    function checkAaveV3Health(address user)
        external
        view
        returns (
            uint256 totalCollateralBase,
            uint256 totalDebtBase,
            uint256 healthFactor,
            bool canBeLiquidated
        );

    function checkRadiantHealth(address user)
        external
        view
        returns (
            uint256 totalCollateralBase,
            uint256 totalDebtBase,
            uint256 healthFactor,
            bool canBeLiquidated
        );

    function checkCompoundV3Liquidatable(address user)
        external
        view
        returns (bool isLiquidatable, address absorber);

    // ── Execution ────────────────────────────────────────────────────
    function executeLiquidation(bytes calldata params) external;
    function receiveFlashLoan(
        address[] calldata tokens,
        uint256[] calldata amounts,
        uint256[] calldata feeAmounts,
        bytes calldata userData
    ) external;

    // ── Emergency ────────────────────────────────────────────────────
    function emergencyWithdraw(address token) external;
    function emergencyWithdrawETH() external;
    function emergencyWithdrawMultiple(address[] calldata tokens) external;

    // ── Token addresses ──────────────────────────────────────────────
    function USDC() external view returns (address);
    function USDT() external view returns (address);
    function WETH() external view returns (address);
    function WBTC() external view returns (address);
    function WPOL()  external view returns (address);
    function DAI()  external view returns (address);
    function BALANCER_VAULT() external view returns (address);
}

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function decimals() external view returns (uint8);
}

interface IAavePool {
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
