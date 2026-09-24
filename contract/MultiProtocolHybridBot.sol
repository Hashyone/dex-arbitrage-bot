// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// ═══════════════════════════════════════════════════════════════════════
// INLINED DEPENDENCIES  (no OZ imports required)
// ═══════════════════════════════════════════════════════════════════════

abstract contract ReentrancyGuard {
    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;
    uint256 private _status;
    constructor() { _status = _NOT_ENTERED; }
    modifier nonReentrant() {
        require(_status != _ENTERED, "ReentrancyGuard: reentrant call");
        _status = _ENTERED;
        _;
        _status = _NOT_ENTERED;
    }
}

abstract contract Ownable {
    address private _owner;
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    constructor(address initialOwner) {
        require(initialOwner != address(0), "Ownable: zero address");
        _owner = initialOwner;
        emit OwnershipTransferred(address(0), initialOwner);
    }
    function owner() public view returns (address) { return _owner; }
    modifier onlyOwner() {
        require(_owner == msg.sender, "Ownable: caller is not the owner");
        _;
    }
    function transferOwnership(address newOwner) public onlyOwner {
        require(newOwner != address(0), "Ownable: zero address");
        emit OwnershipTransferred(_owner, newOwner);
        _owner = newOwner;
    }
}

// ═══════════════════════════════════════════════════════════════════════
// SAFE ERC20 HELPERS
// Low-level calls that handle non-standard tokens (USDT, etc.)
// that don't return bool from approve/transfer.
// ═══════════════════════════════════════════════════════════════════════

library SafeERC20 {
    // Selectors (pre-computed keccak256 of function signatures)
    bytes4 private constant TRANSFER     = 0xa9059cbb; // transfer(address,uint256)
    bytes4 private constant TRANSFER_FROM= 0x23b872dd; // transferFrom(address,address,uint256)
    bytes4 private constant APPROVE      = 0x095ea7b3; // approve(address,uint256)
    bytes4 private constant ALLOWANCE    = 0xdd62ed3e; // allowance(address,address)
    bytes4 private constant BALANCE_OF   = 0x70a08231; // balanceOf(address)

    function safeTransfer(address token, address to, uint256 amount) internal {
        (bool ok, bytes memory ret) = token.call(abi.encodeWithSelector(TRANSFER, to, amount));
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "SafeERC20: transfer failed");
    }

    function safeTransferFrom(address token, address from, address to, uint256 amount) internal {
        (bool ok, bytes memory ret) = token.call(abi.encodeWithSelector(TRANSFER_FROM, from, to, amount));
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "SafeERC20: transferFrom failed");
    }

    function balanceOf(address token, address account) internal view returns (uint256) {
        (bool ok, bytes memory ret) = token.staticcall(abi.encodeWithSelector(BALANCE_OF, account));
        if (!ok || ret.length < 32) return 0;
        return abi.decode(ret, (uint256));
    }

    function allowance(address token, address owner_, address spender) internal view returns (uint256) {
        (bool ok, bytes memory ret) = token.staticcall(abi.encodeWithSelector(ALLOWANCE, owner_, spender));
        if (!ok || ret.length < 32) return 0;
        return abi.decode(ret, (uint256));
    }

    function safeApprove(address token, address spender, uint256 amount, bool requiresReset) internal {
        uint256 current = allowance(token, address(this), spender);
        if (current == amount) return;
        // Reset to zero first if token requires it (e.g. USDT, bridged USDC)
        if (current > 0 && (requiresReset || amount > 0)) {
            (bool ok,) = token.call(abi.encodeWithSelector(APPROVE, spender, uint256(0)));
            require(ok, "SafeERC20: reset-to-zero failed");
        }
        (bool ok2,) = token.call(abi.encodeWithSelector(APPROVE, spender, amount));
        require(ok2, "SafeERC20: approve failed");
    }
}

// ═══════════════════════════════════════════════════════════════════════
// PROTOCOL INTERFACES (minimal — only what we call)
// ═══════════════════════════════════════════════════════════════════════

interface IBalancerVault {
    function flashLoan(
        address recipient,
        address[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external;
}

interface IAaveV3Pool {
    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
    function getUserAccountData(address user) external view returns (
        uint256 totalCollateralBase,
        uint256 totalDebtBase,
        uint256 availableBorrowsBase,
        uint256 currentLiquidationThreshold,
        uint256 ltv,
        uint256 healthFactor
    );
}

interface IAaveV2Pool {
    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
    function getUserAccountData(address user) external view returns (
        uint256 totalCollateralETH,
        uint256 totalDebtETH,
        uint256 availableBorrowsETH,
        uint256 currentLiquidationThreshold,
        uint256 ltv,
        uint256 healthFactor
    );
}

struct MorphoMarketParams {
    address loanToken;
    address collateralToken;
    address oracle;
    address irm;
    uint256 lltv;
}

interface IMorphoBlue {
    function liquidate(
        MorphoMarketParams memory marketParams,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        bytes calldata data
    ) external returns (uint256 seizedAssets_, uint256 repaidAssets_);
}

interface IComet {
    function absorb(address absorber, address[] calldata accounts) external;
    function buyCollateral(
        address asset,
        uint256 minAmount,
        uint256 baseAmount,
        address recipient
    ) external;
    function isLiquidatable(address account) external view returns (bool);
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
        external view returns (uint256[] memory amounts);
}

interface IUniswapV3Router {
    struct ExactInputParams {
        bytes   path;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }
    function exactInput(ExactInputParams calldata params) external returns (uint256 amountOut);
}

// ═══════════════════════════════════════════════════════════════════════
// MAIN CONTRACT
// ═══════════════════════════════════════════════════════════════════════

contract MultiProtocolHybridBot is Ownable, ReentrancyGuard {
    using SafeERC20 for address;  // not strictly needed but documents intent

    // ── Enums ────────────────────────────────────────────────────────
    enum ProtocolType   { AAVE_V3, MORPHO_BLUE, COMPOUND_V3, RADIANT }
    enum SwapRouterType { QUICKSWAP_V2, SUSHISWAP, UNISWAP_V3, ONE_INCH }

    // ── Liquidation Params ────────────────────────────────────────────
    struct LiquidationParams {
        ProtocolType    protocol;
        address         collateralAsset;
        address         debtAsset;
        address         user;
        uint256         debtToCover;
        uint256         minCollateralReceived;
        address[]       swapPath;
        address         swapRouter;
        SwapRouterType  routerType;
        uint24[]        uniswapV3Fees;
        bytes           oneInchData;
        uint256         deadline;
        uint256         minProfitRequired;
        bytes           extraData;   // protocol-specific (e.g. MorphoMarketParams)
    }

    // ── Immutables ────────────────────────────────────────────────────
    IBalancerVault public constant BALANCER_VAULT =
        IBalancerVault(0xBA12222222228d8Ba445958a75a0704d566BF2C8);

    // ── Configurable Protocol Addresses ──────────────────────────────
    address public aaveV3Pool    = 0x794a61358D6845594F94dc1DB02A252b5b4814aD;
    address public radiantPool   = 0x2032b9A8e9F7e76768CA9271003d3e43E1616B1F;
    address public morphoBlue    = 0x9dc1cf03C47513f64C3cA6b226F4b2B9da36e281;
    address public compoundComet = 0xF25212E676D1F7F89Cd72fFEe66158f541246445;

    // ── DEX Routers ───────────────────────────────────────────────────
    address public constant QUICKSWAP_V2 = 0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff;
    address public constant SUSHISWAP    = 0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506;
    address public constant UNISWAP_V3   = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address public constant ONE_INCH     = 0x111111125421cA6dc452d289314280a0f8842A65;

    // ── Key Tokens (Polygon verified) ─────────────────────────────────
    address public constant WPOL   = 0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270;
    address public constant WETH   = 0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619;
    address public constant WBTC   = 0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6;
    address public constant USDC   = 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174;
    address public constant USDC_N = 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359;
    address public constant USDT   = 0xc2132D05D31c914a87C6611C10748AEb04B58e8F;
    address public constant DAI    = 0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063;
    address public constant LINK   = 0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39;
    address public constant AAVE   = 0xD6DF932A45C0f255f85145f286eA0b292B21C90B;
    address public constant CRV    = 0x172370d5Cd63279eFa6d502DAB29171933a610AF;
    address public constant BAL    = 0x9A71012b13ca4d3D0cDc72a177DF3eF03b0E76a7;

    // Tokens that require reset-to-zero approval (USDT + bridged USDC on Polygon)
    mapping(address => bool) public requiresResetApproval;

    // Access control
    mapping(address => bool) public approvedRouters;

    // Safety params
    uint256 public maxGasPrice = 500 gwei;
    bool    public paused      = false;

    // ── Events ────────────────────────────────────────────────────────
    event LiquidationExecuted(
        address indexed user,
        ProtocolType indexed protocol,
        address collateralAsset,
        address debtAsset,
        uint256 debtCovered,
        uint256 collateralReceived,
        uint256 profit
    );
    event ProtocolAddressUpdated(string protocol, address newAddress);
    event EmergencyWithdraw(address indexed token, uint256 amount);

    // ── Errors ────────────────────────────────────────────────────────
    error ContractPaused();
    error GasPriceTooHigh(uint256 current, uint256 max);
    error InvalidDeadline();
    error RouterNotApproved(address router);
    error InvalidSwapPath();
    error PositionHealthy();
    error InsufficientCollateral(uint256 received, uint256 minRequired);
    error InsufficientProfit(uint256 actual, uint256 required);
    error FlashloanRepaymentFailed();
    error UnauthorizedCaller(address caller);

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    // ── Constructor ───────────────────────────────────────────────────
    constructor() Ownable(msg.sender) {
        approvedRouters[QUICKSWAP_V2] = true;
        approvedRouters[SUSHISWAP]    = true;
        approvedRouters[UNISWAP_V3]   = true;
        approvedRouters[ONE_INCH]     = true;

        // USDT and bridged USDC require reset-to-zero before re-approving
        requiresResetApproval[USDT] = true;
        requiresResetApproval[USDC] = true;
    }

    // ═══════════════════════════════════════════════════════════════════
    // OWNER CONFIG
    // ═══════════════════════════════════════════════════════════════════

    function setPaused(bool _paused) external onlyOwner { paused = _paused; }
    function setMaxGasPrice(uint256 v) external onlyOwner { maxGasPrice = v; }

    function setAaveV3Pool(address v) external onlyOwner { aaveV3Pool = v; emit ProtocolAddressUpdated("AAVE_V3", v); }
    function setRadiantPool(address v) external onlyOwner { radiantPool = v; emit ProtocolAddressUpdated("RADIANT", v); }
    function setMorphoBlue(address v) external onlyOwner { morphoBlue = v; emit ProtocolAddressUpdated("MORPHO_BLUE", v); }
    function setCompoundComet(address v) external onlyOwner { compoundComet = v; emit ProtocolAddressUpdated("COMPOUND_V3", v); }

    function setRouterApproval(address router, bool approved) external onlyOwner { approvedRouters[router] = approved; }
    function setRequiresResetApproval(address token, bool v) external onlyOwner { requiresResetApproval[token] = v; }

    // ═══════════════════════════════════════════════════════════════════
    // APPROVAL MANAGEMENT
    // ═══════════════════════════════════════════════════════════════════

    /// @notice Approve a list of tokens for a single spender. Gas-efficient per-spender call.
    function batchApproveTokensForSpender(address[] calldata tokens, address spender)
        external onlyOwner
    {
        for (uint256 i = 0; i < tokens.length; i++) {
            _safeApprove(tokens[i], spender, type(uint256).max);
        }
    }

    /// @notice Approve all built-in tokens for all built-in spenders.
    ///         Call once per spender after deployment to avoid gas cap issues.
    function approveTokenForAllSpenders(address token) external onlyOwner {
        address[9] memory spenders = [
            QUICKSWAP_V2,
            SUSHISWAP,
            UNISWAP_V3,
            ONE_INCH,
            aaveV3Pool,
            radiantPool,
            morphoBlue,
            compoundComet,
            address(BALANCER_VAULT)
        ];
        for (uint256 i = 0; i < spenders.length; i++) {
            if (spenders[i] != address(0)) {
                _safeApprove(token, spenders[i], type(uint256).max);
            }
        }
    }

    /// @notice Approve a single token for a single spender (fine-grained).
    function approveToken(address token, address spender, uint256 amount) external onlyOwner {
        _safeApprove(token, spender, amount);
    }

    // ═══════════════════════════════════════════════════════════════════
    // MAIN ENTRY POINT
    // ═══════════════════════════════════════════════════════════════════

    function executeLiquidation(bytes calldata params)
        external nonReentrant whenNotPaused
    {
        if (tx.gasprice > maxGasPrice)
            revert GasPriceTooHigh(tx.gasprice, maxGasPrice);

        LiquidationParams memory p = abi.decode(params, (LiquidationParams));

        if (block.timestamp > p.deadline) revert InvalidDeadline();
        if (!approvedRouters[p.swapRouter]) revert RouterNotApproved(p.swapRouter);
        if (p.swapPath.length < 2) revert InvalidSwapPath();
        if (p.swapPath[0] != p.collateralAsset) revert InvalidSwapPath();
        if (p.swapPath[p.swapPath.length - 1] != p.debtAsset) revert InvalidSwapPath();

        _verifyPositionUnhealthy(p);

        address[] memory tokens  = new address[](1);
        uint256[] memory amounts = new uint256[](1);
        tokens[0]  = p.debtAsset;
        amounts[0] = p.debtToCover;

        BALANCER_VAULT.flashLoan(address(this), tokens, amounts, abi.encode(p));
    }

    // ═══════════════════════════════════════════════════════════════════
    // BALANCER FLASH LOAN CALLBACK
    // ═══════════════════════════════════════════════════════════════════

    function receiveFlashLoan(
        address[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes memory userData
    ) external {
        if (msg.sender != address(BALANCER_VAULT))
            revert UnauthorizedCaller(msg.sender);

        LiquidationParams memory p = abi.decode(userData, (LiquidationParams));

        address debtAsset  = tokens[0];
        uint256 debtAmount = amounts[0];
        uint256 totalRepay = debtAmount + feeAmounts[0];

        uint256 colBefore = SafeERC20.balanceOf(p.collateralAsset, address(this));

        if (p.protocol == ProtocolType.AAVE_V3) {
            _safeApprove(debtAsset, aaveV3Pool, debtAmount);
            IAaveV3Pool(aaveV3Pool).liquidationCall(
                p.collateralAsset, debtAsset, p.user, debtAmount, false
            );
        } else if (p.protocol == ProtocolType.RADIANT) {
            _safeApprove(debtAsset, radiantPool, debtAmount);
            IAaveV2Pool(radiantPool).liquidationCall(
                p.collateralAsset, debtAsset, p.user, debtAmount, false
            );
        } else if (p.protocol == ProtocolType.MORPHO_BLUE) {
            MorphoMarketParams memory mp = abi.decode(p.extraData, (MorphoMarketParams));
            _safeApprove(debtAsset, morphoBlue, debtAmount);
            IMorphoBlue(morphoBlue).liquidate(mp, p.user, p.debtToCover, 0, "");
        } else if (p.protocol == ProtocolType.COMPOUND_V3) {
            address[] memory accs = new address[](1);
            accs[0] = p.user;
            IComet(compoundComet).absorb(address(this), accs);
            _safeApprove(debtAsset, compoundComet, debtAmount);
            IComet(compoundComet).buyCollateral(
                p.collateralAsset, p.minCollateralReceived, debtAmount, address(this)
            );
        }

        uint256 colReceived = SafeERC20.balanceOf(p.collateralAsset, address(this)) - colBefore;
        if (colReceived < p.minCollateralReceived)
            revert InsufficientCollateral(colReceived, p.minCollateralReceived);

        if (p.collateralAsset != debtAsset && colReceived > 0) {
            _swapCollateralForDebt(p, colReceived);
        }

        uint256 debtBal = SafeERC20.balanceOf(debtAsset, address(this));
        if (debtBal < totalRepay) revert FlashloanRepaymentFailed();

        // Repay Balancer
        _safeApprove(debtAsset, address(BALANCER_VAULT), totalRepay);
        SafeERC20.safeTransfer(debtAsset, address(BALANCER_VAULT), totalRepay);

        // Verify profit and sweep to owner
        uint256 profit       = SafeERC20.balanceOf(debtAsset, address(this));
        uint256 colRemainder = SafeERC20.balanceOf(p.collateralAsset, address(this));

        if (profit < p.minProfitRequired)
            revert InsufficientProfit(profit, p.minProfitRequired);

        if (profit > 0)       SafeERC20.safeTransfer(debtAsset, owner(), profit);
        if (colRemainder > 0) SafeERC20.safeTransfer(p.collateralAsset, owner(), colRemainder);

        emit LiquidationExecuted(
            p.user, p.protocol, p.collateralAsset, debtAsset,
            debtAmount, colReceived, profit
        );
    }

    // ═══════════════════════════════════════════════════════════════════
    // INTERNAL HELPERS
    // ═══════════════════════════════════════════════════════════════════

    function _verifyPositionUnhealthy(LiquidationParams memory p) internal view {
        if (p.protocol == ProtocolType.AAVE_V3) {
            (, , , , , uint256 hf) = IAaveV3Pool(aaveV3Pool).getUserAccountData(p.user);
            if (hf >= 1e18) revert PositionHealthy();
        } else if (p.protocol == ProtocolType.RADIANT) {
            (, , , , , uint256 hf) = IAaveV2Pool(radiantPool).getUserAccountData(p.user);
            if (hf >= 1e18) revert PositionHealthy();
        } else if (p.protocol == ProtocolType.COMPOUND_V3) {
            if (!IComet(compoundComet).isLiquidatable(p.user)) revert PositionHealthy();
        }
        // Morpho: bot verifies off-chain (no cheap view for liquidatability on-chain)
    }

    function _safeApprove(address token, address spender, uint256 amount) internal {
        SafeERC20.safeApprove(token, spender, amount, requiresResetApproval[token]);
    }

    function _swapCollateralForDebt(LiquidationParams memory p, uint256 colAmount) internal {
        _safeApprove(p.collateralAsset, p.swapRouter, colAmount);

        if (p.routerType == SwapRouterType.QUICKSWAP_V2 ||
            p.routerType == SwapRouterType.SUSHISWAP)
        {
            IUniswapV2Router router = IUniswapV2Router(p.swapRouter);
            uint256[] memory expected = router.getAmountsOut(colAmount, p.swapPath);
            uint256 minOut = (expected[expected.length - 1] * 97) / 100;
            router.swapExactTokensForTokens(colAmount, minOut, p.swapPath, address(this), p.deadline);
        }
        else if (p.routerType == SwapRouterType.UNISWAP_V3) {
            bytes memory path = _encodeV3Path(p.swapPath, p.uniswapV3Fees);
            IUniswapV3Router(p.swapRouter).exactInput(
                IUniswapV3Router.ExactInputParams({
                    path:             path,
                    recipient:        address(this),
                    deadline:         p.deadline,
                    amountIn:         colAmount,
                    amountOutMinimum: 0
                })
            );
        }
        else if (p.routerType == SwapRouterType.ONE_INCH) {
            require(p.oneInchData.length > 0, "1inch: no data");
            (bool ok, bytes memory ret) = p.swapRouter.call(p.oneInchData);
            require(ok, string(ret));
        }
    }

    function _encodeV3Path(address[] memory path, uint24[] memory fees)
        internal pure returns (bytes memory encoded)
    {
        require(path.length >= 2 && fees.length == path.length - 1, "Bad V3 path");
        encoded = abi.encodePacked(path[0]);
        for (uint256 i = 0; i < fees.length; i++) {
            encoded = abi.encodePacked(encoded, fees[i], path[i + 1]);
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    // VIEW HELPERS
    // ═══════════════════════════════════════════════════════════════════

    function checkAaveV3Health(address user) external view returns (
        uint256 totalCollateralBase,
        uint256 totalDebtBase,
        uint256 healthFactor,
        bool    canBeLiquidated
    ) {
        (totalCollateralBase, totalDebtBase, , , , healthFactor) =
            IAaveV3Pool(aaveV3Pool).getUserAccountData(user);
        canBeLiquidated = healthFactor < 1e18;
    }

    function checkRadiantHealth(address user) external view returns (
        uint256 totalCollateralETH,
        uint256 totalDebtETH,
        uint256 healthFactor,
        bool    canBeLiquidated
    ) {
        (totalCollateralETH, totalDebtETH, , , , healthFactor) =
            IAaveV2Pool(radiantPool).getUserAccountData(user);
        canBeLiquidated = healthFactor < 1e18;
    }

    function checkCompoundV3Liquidatable(address user) external view returns (bool) {
        return IComet(compoundComet).isLiquidatable(user);
    }

    // ═══════════════════════════════════════════════════════════════════
    // EMERGENCY
    // ═══════════════════════════════════════════════════════════════════

    function emergencyWithdraw(address token) external onlyOwner {
        uint256 bal = SafeERC20.balanceOf(token, address(this));
        if (bal > 0) {
            SafeERC20.safeTransfer(token, owner(), bal);
            emit EmergencyWithdraw(token, bal);
        }
    }

    function emergencyWithdrawETH() external onlyOwner {
        uint256 bal = address(this).balance;
        if (bal > 0) payable(owner()).transfer(bal);
    }

    function emergencyWithdrawMultiple(address[] calldata tokens) external onlyOwner {
        for (uint256 i = 0; i < tokens.length; i++) {
            uint256 bal = SafeERC20.balanceOf(tokens[i], address(this));
            if (bal > 0) {
                SafeERC20.safeTransfer(tokens[i], owner(), bal);
                emit EmergencyWithdraw(tokens[i], bal);
            }
        }
    }

    receive() external payable {}
}
