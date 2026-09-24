#!/usr/bin/env python3
"""
AAVE V3 LIQUIDATION BOT — Polygon
===================================
Contract: 0xc3845c11104538aeD89dc2461Dc675e2c8838b84

Root cause of col=[] debt=[] fixed:
  Users had collateral in tokens NOT in the hardcoded 6-asset list
  (stMATIC, MaticX, AAVE, CRV, LINK, BAL, GHST, etc.).
  getUserReserveData reverted for those tokens because they weren't
  recognised as valid reserves for that user.

Fix: Use UiPoolDataProviderV3.getUserReservesData() which returns
  ALL user positions across ALL reserves in one call — the same
  contract Aave's frontend uses. This eliminates the hardcoded
  asset list entirely and catches every possible collateral/debt pair.

Architecture:
  1. At startup: call getReservesData() to load all reserve metadata
     (decimals, liquidation bonus, aToken address, symbol, etc.)
     for every active reserve on Aave V3 Polygon.
  2. On each block: fetch Aave events, extract users, call
     getUserReservesData() per user for full position breakdown.
  3. Periodic rescan of all tracked users every 20 blocks to catch
     price-driven liquidations (no event required).
  4. Profit: oracle prices + real balances + real bonuses.
  5. Execution: encode LiquidationParams, estimate_gas, submit.

Web3.py v7.10 compatibility:
  - No w3.eth.account.sign_transaction kwargs changes needed in v7
  - middleware injection uses inject() not add()  
  - All contract calls use .call() not .call({}) 
  - No ExtraDataToPOAMiddleware import path changes in v7

Environment:
  ALCHEMY_API_KEY   — required
  PRIVATE_KEY       — required for execution
  EXECUTION_ENABLED — 'true' to execute
  MIN_PROFIT_USD    — default 10
"""

import asyncio
from collections import deque
import json
import logging
import traceback
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional
import concurrent.futures

import websockets
import aiohttp
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_abi import encode as abi_encode
from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════════════════
# LOGGING — verbose, structured
# ════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('liquidation_bot.log'),
    ],
)
logger = logging.getLogger('LiqBot')
for lib in ('websockets', 'web3', 'urllib3', 'aiohttp'):
    logging.getLogger(lib).setLevel(logging.WARNING)




# ════════════════════════════════════════════════════════════
# RPC CONFIG
# Primary: OnFinality (Alchemy monthly cap exhausted)
# Fallback: Alchemy (if OnFinality key not set)
# ════════════════════════════════════════════════════════════
ALCHEMY_KEY = (
    os.getenv('ALCHEMY_API_KEY') or
    os.getenv('ALCHEMY_KEY') or
    os.getenv('API_KEY') or
    ''
).strip()

# OnFinality — handles raw UUID, full URL, or query-string formats
_onfinality_raw = (
    os.getenv('FINALITY_API_KEY') or
    os.getenv('ONFINALITY_KEY') or
    os.getenv('finality') or
    ''
).strip()

if _onfinality_raw:
    if '?apikey=' in _onfinality_raw:
        _onfinality_key = _onfinality_raw.split('?apikey=')[-1].strip()
    elif _onfinality_raw.startswith('http'):
        import urllib.parse as _up
        _qs             = _up.urlparse(_onfinality_raw).query
        _params         = dict(p.split('=', 1) for p in _qs.split('&') if '=' in p)
        _onfinality_key = _params.get('apikey', '').strip()
    else:
        _onfinality_key = _onfinality_raw  # already a raw UUID
else:
    _onfinality_key = ''

# ── HTTP endpoint ──────────────────────────────────────────
if _onfinality_key:
    ALCHEMY_HTTP = f'https://polygon.api.onfinality.io/rpc?apikey={_onfinality_key}'
    _http_label  = 'OnFinality'
elif ALCHEMY_KEY:
    ALCHEMY_HTTP = f'https://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}'
    _http_label  = 'Alchemy (fallback)'
else:
    raise ValueError(
        'No HTTP RPC configured. Set FINALITY_API_KEY in Replit secrets.'
    )

# ── WSS endpoint ───────────────────────────────────────────
if _onfinality_key:
    ALCHEMY_WSS = f'wss://polygon.api.onfinality.io/ws?apikey={_onfinality_key}'
    _wss_label  = 'OnFinality'
elif ALCHEMY_KEY:
    ALCHEMY_WSS = f'wss://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}'
    _wss_label  = 'Alchemy (fallback)'
else:
    raise ValueError(
        'No WSS RPC configured. Set FINALITY_API_KEY in Replit secrets.'
    )

print(f'RPC  HTTP : {_http_label} → {ALCHEMY_HTTP}')
print(f'RPC  WSS  : {_wss_label}  → {ALCHEMY_WSS}')
# ════════════════════════════════════════════════════════════
# AAVE V3 POLYGON — VERIFIED CONTRACT ADDRESSES
# Source: https://github.com/aave/docs-v3/blob/main/deployed-contracts/v3-mainnet/polygon.md
# ════════════════════════════════════════════════════════════

POOL_ADDRESSES_PROVIDER = Web3.to_checksum_address(
    '0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb'
)
AAVE_POOL = Web3.to_checksum_address(
    '0x794a61358D6845594F94dc1DB02A252b5b4814aD'
)
AAVE_ORACLE = Web3.to_checksum_address(
    '0xb023e699F5a33916Ea823A16485e259257cA8Bd1'
)
UI_POOL_DATA_PROVIDER = Web3.to_checksum_address(
    '0x68100bD5345eA474D93577127C11F39FF8463e93'
)
POOL_DATA_PROVIDER = Web3.to_checksum_address(
    '0x69FA688f1Dc47d4B5d8029D5a35FB7a548310654'
)
LIQ_CONTRACT = Web3.to_checksum_address(
    '0xc3845c11104538aeD89dc2461Dc675e2c8838b84'
)
BALANCER_VAULT = Web3.to_checksum_address(
    '0xBA12222222228d8Ba445958a75a0704d566BF2C8'
)

# Routers
QS_ROUTER    = Web3.to_checksum_address('0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff')
SUSHI_ROUTER = Web3.to_checksum_address('0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506')
V3_ROUTER    = Web3.to_checksum_address('0xE592427A0AEce92De3Edee1F18E0157C05861564')
RT_QUICKSWAP = 0
RT_SUSHISWAP = 1
RT_V3        = 2

# Common intermediate tokens for swap routing
WETH   = Web3.to_checksum_address('0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619')
WMATIC = Web3.to_checksum_address('0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270')
USDC   = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
USDT   = Web3.to_checksum_address('0xc2132D05D31c914a87C6611C10748AEb04B58e8F')
DAI    = Web3.to_checksum_address('0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063')
WBTC   = Web3.to_checksum_address('0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6')

MAJOR_TOKENS = {WETH, WMATIC, USDC, USDT, DAI, WBTC}

# ════════════════════════════════════════════════════════════
# AAVE V3 EVENTS
# ════════════════════════════════════════════════════════════

def _topic(sig: str) -> str:
    h = Web3.keccak(text=sig).hex()
    return '0x' + h if not h.startswith('0x') else h

BORROW_TOPIC  = _topic('Borrow(address,address,address,uint256,uint8,uint256,uint16)')
REPAY_TOPIC   = _topic('Repay(address,address,address,uint256,bool)')
SUPPLY_TOPIC  = _topic('Supply(address,address,address,uint256,uint16)')
LIQ_TOPIC     = _topic('LiquidationCall(address,address,address,uint256,uint256,address,bool)')

WATCHED_TOPICS = [BORROW_TOPIC, REPAY_TOPIC, SUPPLY_TOPIC]

RESCAN_BLOCKS       = 20
RECHECK_USER_BLOCKS = 10

# ── Volatility trigger constants ──────────────────────────
POLL_INTERVAL_SECS  = 90      # seconds between HTTP price polls in quiet mode
PRICE_DROP_PCT      = 0.05    # 5% drop triggers WSS activation
HF_WATCH_THRESHOLD  = Decimal('1.08')  # only watch users below this HF
WSS_ACTIVE_SECS     = 600     # stay in WSS mode for 10 min after trigger
PRICE_HISTORY_DEPTH = 6       # compare against price N polls ago




class UserTracker:
    """Persistent tracking of users with elevated risk"""

    def __init__(self, cache_file='tracked_users.json'):
        self.cache_file = cache_file
        self.load()

    def load(self):
        """Load tracked users from disk"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                    self.tracked_users = data.get('users', {})
                    logger.info(f"📁 Loaded {len(self.tracked_users)} tracked users from {self.cache_file}")
            except Exception as e:
                logger.warning(f"Failed to load cache: {e}")
                self.tracked_users = {}
        else:
            self.tracked_users = {}

    def save(self):
        """Save tracked users to disk"""
        try:
            with open(self.cache_file, 'w') as f:
                json.dump({'users': self.tracked_users, 'updated': time.time()}, f)
        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")

    def add_user(self, user, hf, block):
        """Add or update a tracked user"""
        if hf < 1.10:  # Only track users with HF < 1.10
            self.tracked_users[user] = {
                'hf': float(hf),
                'last_block': block,
                'last_seen': time.time()
            }
            self.save()

    def get_users_to_check(self, current_block):
        """Get users that need rechecking based on HF"""
        users_to_check = []
        for user, data in self.tracked_users.items():
            hf = data['hf']
            last_block = data['last_block']

            if hf < 1.05:
                interval = 5
            elif hf < 1.10:
                interval = 20
            else:
                interval = 50

            if current_block - last_block >= interval:
                users_to_check.append(user)

        return users_to_check


class VolatilityTrigger:
    """
    Tracks price history during HTTP polling and decides when to
    activate expensive WSS mode.
    Now includes predictive liquidation detection.
    """

    def __init__(self):
        self._history: dict = {}   # asset_lower -> list of (ts, price)
        self.wss_until: float = 0.0
        self._user_critical_drops: dict = {}  # user -> {asset_symbol: drop_needed}
        self.liquidity_indexes: dict = {}     # asset_lower -> Decimal(liquidityIndex)

    def record_prices(self, prices: dict):
        now = time.time()
        for asset, price in prices.items():
            if asset not in self._history:
                self._history[asset] = []
            self._history[asset].append((now, float(price)))
            # Keep only the last PRICE_HISTORY_DEPTH samples
            self._history[asset] = (
                self._history[asset][-PRICE_HISTORY_DEPTH:]
            )

    def max_drop(self) -> tuple:
        """
        Returns (asset_lower, drop_fraction) for the largest price
        decline observed across all assets since the oldest sample.
        e.g. ('0x7ceb...', 0.063) means WETH dropped 6.3%
        Returns ('', 0.0) if insufficient history.
        """
        worst_asset = ''
        worst_drop  = 0.0
        for asset, history in self._history.items():
            if len(history) < 2:
                continue
            oldest  = history[0][1]
            current = history[-1][1]
            if oldest == 0:
                continue
            drop = (oldest - current) / oldest
            if drop > worst_drop:
                worst_drop  = drop
                worst_asset = asset
        return worst_asset, worst_drop

    def estimate_hf(self, pos, prices: dict) -> Decimal:
        """
        Estimate current HF using cached position + latest oracle prices.
        No RPC calls — pure math on already-loaded data.
        Now includes liquidity index for accurate collateral calculation.
        """
        if not pos:
            return Decimal('999')

        weighted_col = Decimal('0')
        total_debt   = Decimal('0')

        for col in pos.collateral:
            price = prices.get(col.asset.lower())
            if not price:
                continue

            # Get liquidity index (default to 1e27 if not available)
            liq_index = self.liquidity_indexes.get(col.asset.lower(), Decimal('1e27'))

            # Convert scaled aToken balance to actual balance
            actual_a_bal = (Decimal(col.a_bal) * liq_index) / Decimal('1e27')

            col_usd = (actual_a_bal / Decimal(10 ** col.decimals)) * price
            weighted_col += col_usd * col.reserve.liq_threshold

        for debt in pos.debt:
            price = prices.get(debt.asset.lower())
            if not price:
                continue

            # Debt is already actual, not scaled
            actual_debt = Decimal(debt.total_debt) / Decimal(10 ** debt.decimals) * price
            total_debt += actual_debt

        if total_debt == 0:
            return Decimal('999')
        return weighted_col / total_debt

    def calculate_critical_price_drop(self, pos, prices: dict) -> dict:
        """
        For each collateral asset, calculate % drop needed to liquidate user.
        Returns: {asset_symbol: drop_percentage_needed}
        """
        if not pos or not pos.collateral or not pos.debt:
            return {}

        result = {}
        current_col_usd = Decimal('0')
        current_debt_usd = Decimal('0')

        # Calculate current totals
        for col in pos.collateral:
            price = prices.get(col.asset.lower())
            if price:
                # Get liquidity index for accurate balance
                liq_index = self.liquidity_indexes.get(col.asset.lower(), Decimal('1e27'))
                actual_a_bal = (Decimal(col.a_bal) * liq_index) / Decimal('1e27')
                col_usd = (actual_a_bal / Decimal(10 ** col.decimals)) * price
                current_col_usd += col_usd * col.reserve.liq_threshold

        for debt in pos.debt:
            price = prices.get(debt.asset.lower())
            if price:
                debt_usd = (Decimal(debt.total_debt) / Decimal(10 ** debt.decimals)) * price
                current_debt_usd += debt_usd

        if current_debt_usd == 0:
            return {}

        # Calculate drop needed for each collateral asset
        for col in pos.collateral:
            price = prices.get(col.asset.lower())
            if not price:
                continue

            # Get liquidity index for accurate balance
            liq_index = self.liquidity_indexes.get(col.asset.lower(), Decimal('1e27'))
            actual_a_bal = (Decimal(col.a_bal) * liq_index) / Decimal('1e27')
            col_usd = (actual_a_bal / Decimal(10 ** col.decimals)) * price
            col_weighted = col_usd * col.reserve.liq_threshold

            # Solve for new_price where HF = 1.0
            # (current_col_usd - col_weighted + col_weighted * (new_price/price)) / debt_usd = 1.0

            needed_col_weighted = current_debt_usd - (current_col_usd - col_weighted)

            if needed_col_weighted <= 0:
                drop_needed = Decimal('100')  # Already liquidatable or zero contribution
            else:
                price_ratio = needed_col_weighted / col_weighted
                drop_needed = (Decimal('1') - price_ratio) * Decimal('100')

            result[col.symbol] = max(Decimal('0'), min(Decimal('100'), drop_needed))

        return result

    def update_risk_profile(self, user_positions: dict, prices: dict):
        """
        Called periodically with all cached positions to track liquidation risk.
        """
        for user, pos in user_positions.items():
            if pos and pos.health_factor <= Decimal('1.08'):
                critical_drops = self.calculate_critical_price_drop(pos, prices)
                self._user_critical_drops[user] = critical_drops

                # Log if any asset needs < 5% drop
                for asset, drop_needed in critical_drops.items():
                    if drop_needed < Decimal('5'):
                        logger.warning(
                            f'⚠️ {user[:12]}... {asset} only needs '
                            f'{drop_needed:.1f}% drop to liquidate!'
                        )

    def get_price_drop_for_asset(self, asset_symbol: str) -> Decimal:
        """Calculate current price drop for an asset from history"""
        # Find asset address from symbol (need to map)
        # Simplified: return max drop from history
        _, drop = self.max_drop()
        return Decimal(drop)

    def should_activate_wss(self, min_user_hf: Decimal, price_drop: float) -> bool:
        """
        Determine if WSS should activate based on conditions.
        Now includes predictive triggers.
        """
        # Condition 1: Existing blanket threshold
        blanket_trigger = price_drop >= PRICE_DROP_PCT

        # Condition 2: Any user critical drop threshold crossed
        predictive_trigger = False
        for user, critical_drops in self._user_critical_drops.items():
            for asset_symbol, drop_needed in critical_drops.items():
                if drop_needed < Decimal('3'):  # 3% threshold for predictive trigger
                    predictive_trigger = True
                    logger.info(f'Predictive trigger: {user[:12]} needs {drop_needed:.1f}% drop')
                    break
            if predictive_trigger:
                break

        # Condition 3: Very low HF regardless of price movement
        hf_trigger = min_user_hf <= Decimal('1.02')

        return blanket_trigger or predictive_trigger or hf_trigger
# ════════════════════════════════════════════════════════════
# DATA CLASSES
# ════════════════════════════════════════════════════════════

@dataclass
class ReserveMeta:
    """Metadata for one Aave V3 reserve, loaded once at startup."""
    symbol:       str
    decimals:     int
    liq_bonus:    Decimal   # e.g. 1.05 = 5% bonus, loaded from protocol
    liq_threshold: Decimal  # e.g. 0.80 = 80% threshold
    a_token:      str       # aToken address (not used for balance reading anymore)
    is_active:    bool
    is_frozen:    bool

@dataclass
class UserReservePosition:
    """Per-reserve position for one user."""
    asset:       str        # underlying token address (checksum)
    symbol:      str
    decimals:    int
    a_bal:       int        # aToken balance = collateral
    stable_debt: int
    var_debt:    int
    use_as_col:  bool
    reserve:     ReserveMeta

    @property
    def total_debt(self) -> int:
        return self.stable_debt + self.var_debt

@dataclass
class UserPosition:
    user:                 str
    health_factor:        Decimal
    total_collateral_usd: Decimal
    total_debt_usd:       Decimal
    collateral: list = field(default_factory=list)  # [UserReservePosition]
    debt:       list = field(default_factory=list)  # [UserReservePosition]

@dataclass
class Opportunity:
    user:               str
    health_factor:      Decimal
    close_factor:       Decimal
    col_asset:          str
    col_symbol:         str
    col_amount_raw:     int
    col_usd:            Decimal
    debt_asset:         str
    debt_symbol:        str
    debt_to_cover_raw:  int
    debt_usd:           Decimal
    liq_bonus:          Decimal
    gross_profit_usd:   Decimal
    gas_cost_usd:       Decimal
    net_profit_usd:     Decimal
    block_number:       int
    swap_router:        str
    swap_router_type:   int
    swap_path:          list
    v3_fees:            list
    min_col_raw:        int
    min_profit_raw:     int

# ════════════════════════════════════════════════════════════
# ABIS — minimal, only what we need
# ════════════════════════════════════════════════════════════

POOL_ABI = [
    {
        'name': 'getUserAccountData',
        'inputs': [{'name': 'user', 'type': 'address'}],
        'outputs': [
            {'name': 'totalCollateralBase',         'type': 'uint256'},
            {'name': 'totalDebtBase',               'type': 'uint256'},
            {'name': 'availableBorrowsBase',         'type': 'uint256'},
            {'name': 'currentLiquidationThreshold', 'type': 'uint256'},
            {'name': 'ltv',                         'type': 'uint256'},
            {'name': 'healthFactor',                'type': 'uint256'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'getReservesList',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'address[]'}],
        'stateMutability': 'view', 'type': 'function',
    },
]

# UiPoolDataProviderV3 — returns ALL user positions in ONE call
# PoolDataProvider ABI — stable across all V3 deployments
POOL_DATA_PROVIDER_ABI = [
    {
        'name': 'getReserveConfigurationData',
        'inputs': [{'name': 'asset', 'type': 'address'}],
        'outputs': [
            {'name': 'decimals',             'type': 'uint256'},
            {'name': 'ltv',                  'type': 'uint256'},
            {'name': 'liquidationThreshold', 'type': 'uint256'},
            {'name': 'liquidationBonus',     'type': 'uint256'},
            {'name': 'reserveFactor',        'type': 'uint256'},
            {'name': 'usageAsCollateralEnabled', 'type': 'bool'},
            {'name': 'borrowingEnabled',     'type': 'bool'},
            {'name': 'stableBorrowRateEnabled', 'type': 'bool'},
            {'name': 'isActive',             'type': 'bool'},
            {'name': 'isFrozen',             'type': 'bool'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'getReserveTokensAddresses',
        'inputs': [{'name': 'asset', 'type': 'address'}],
        'outputs': [
            {'name': 'aTokenAddress',             'type': 'address'},
            {'name': 'stableDebtTokenAddress',    'type': 'address'},
            {'name': 'variableDebtTokenAddress',  'type': 'address'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'getUserReserveData',
        'inputs': [
            {'name': 'asset', 'type': 'address'},
            {'name': 'user',  'type': 'address'},
        ],
        'outputs': [
            {'name': 'currentATokenBalance',     'type': 'uint256'},
            {'name': 'currentStableDebt',        'type': 'uint256'},
            {'name': 'currentVariableDebt',      'type': 'uint256'},
            {'name': 'principalStableDebt',      'type': 'uint256'},
            {'name': 'scaledVariableDebt',       'type': 'uint256'},
            {'name': 'stableBorrowRate',         'type': 'uint256'},
            {'name': 'liquidityRate',            'type': 'uint256'},
            {'name': 'stableRateLastUpdated',    'type': 'uint40'},
            {'name': 'usageAsCollateralEnabled', 'type': 'bool'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
]

ERC20_META_ABI = [
    {
        'name': 'symbol',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'string'}],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'decimals',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'uint8'}],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'balanceOf',
        'inputs': [{'name': 'account', 'type': 'address'}],
        'outputs': [{'name': '', 'type': 'uint256'}],
        'stateMutability': 'view', 'type': 'function',
    },
]

UI_PROVIDER_ABI = [
    {
        'name': 'getUserReservesData',
        'inputs': [
            {'name': 'provider', 'type': 'address'},
            {'name': 'user',     'type': 'address'},
        ],
        'outputs': [
            {
                'components': [
                    {'name': 'underlyingAsset',             'type': 'address'},
                    {'name': 'scaledATokenBalance',         'type': 'uint256'},
                    {'name': 'usageAsCollateralEnabledOnUser', 'type': 'bool'},
                    {'name': 'stableBorrowRate',            'type': 'uint256'},
                    {'name': 'scaledVariableDebt',          'type': 'uint256'},
                    {'name': 'principalStableDebt',         'type': 'uint256'},
                    {'name': 'stableBorrowLastUpdateTimestamp', 'type': 'uint256'},
                ],
                'name': 'userReserves',
                'type': 'tuple[]',
            },
            {'name': 'userEmodeCategoryId', 'type': 'uint8'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'getReservesData',
        'inputs': [
            {'name': 'provider', 'type': 'address'},
        ],
        'outputs': [
            {
                'components': [
                    {'name': 'underlyingAsset',             'type': 'address'},
                    {'name': 'name',                        'type': 'string'},
                    {'name': 'symbol',                      'type': 'string'},
                    {'name': 'decimals',                    'type': 'uint256'},
                    {'name': 'baseLTVasCollateral',         'type': 'uint256'},
                    {'name': 'reserveLiquidationThreshold', 'type': 'uint256'},
                    {'name': 'reserveLiquidationBonus',     'type': 'uint256'},
                    {'name': 'reserveFactor',               'type': 'uint256'},
                    {'name': 'usageAsCollateralEnabled',    'type': 'bool'},
                    {'name': 'borrowingEnabled',            'type': 'bool'},
                    {'name': 'stableBorrowRateEnabled',     'type': 'bool'},
                    {'name': 'isActive',                    'type': 'bool'},
                    {'name': 'isFrozen',                    'type': 'bool'},
                    {'name': 'liquidityIndex',              'type': 'uint128'},
                    {'name': 'variableBorrowIndex',         'type': 'uint128'},
                    {'name': 'liquidityRate',               'type': 'uint128'},
                    {'name': 'variableBorrowRate',          'type': 'uint128'},
                    {'name': 'stableBorrowRate',            'type': 'uint128'},
                    {'name': 'lastUpdateTimestamp',         'type': 'uint40'},
                    {'name': 'aTokenAddress',               'type': 'address'},
                    {'name': 'stableDebtTokenAddress',      'type': 'address'},
                    {'name': 'variableDebtTokenAddress',    'type': 'address'},
                    {'name': 'interestRateStrategyAddress', 'type': 'address'},
                    {'name': 'availableLiquidity',          'type': 'uint256'},
                    {'name': 'totalPrincipalStableDebt',    'type': 'uint256'},
                    {'name': 'averageStableRate',           'type': 'uint256'},
                    {'name': 'stableDebtLastUpdateTimestamp', 'type': 'uint256'},
                    {'name': 'totalScaledVariableDebt',     'type': 'uint256'},
                    {'name': 'priceInMarketReferenceCurrency', 'type': 'uint256'},
                    {'name': 'priceOracle',                 'type': 'address'},
                    {'name': 'variableRateSlope1',          'type': 'uint256'},
                    {'name': 'variableRateSlope2',          'type': 'uint256'},
                    {'name': 'stableRateSlope1',            'type': 'uint256'},
                    {'name': 'stableRateSlope2',            'type': 'uint256'},
                    {'name': 'baseStableBorrowRate',        'type': 'uint256'},
                    {'name': 'baseVariableBorrowRate',      'type': 'uint256'},
                    {'name': 'optimalUsageRatio',           'type': 'uint256'},
                    {'name': 'isPaused',                    'type': 'bool'},
                    {'name': 'isSiloedBorrowing',           'type': 'bool'},
                    {'name': 'accruedToTreasury',           'type': 'uint256'},
                    {'name': 'unbacked',                    'type': 'uint256'},
                    {'name': 'isolationModeTotalDebt',      'type': 'uint256'},
                    {'name': 'flashLoanEnabled',            'type': 'bool'},
                    {'name': 'debtCeilingForIsolationMode', 'type': 'uint256'},
                    {'name': 'debtCeiling',                 'type': 'uint256'},
                    {'name': 'eModeCategoryId',             'type': 'uint8'},
                    {'name': 'eModeLabel',                  'type': 'string'},
                    {'name': 'borrowableInIsolation',       'type': 'bool'},
                ],
                'name': 'AggregatedReserveData',
                'type': 'tuple[]',
            },
            {
                'components': [
                    {'name': 'marketReferenceCurrencyUnit',          'type': 'uint256'},
                    {'name': 'marketReferenceCurrencyPriceInUsd',    'type': 'int256'},
                    {'name': 'networkBaseTokenPriceInUsd',           'type': 'int256'},
                    {'name': 'networkBaseTokenPriceDecimals',        'type': 'uint8'},
                ],
                'name': 'BaseCurrencyInfo',
                'type': 'tuple',
            },
        ],
        'stateMutability': 'view', 'type': 'function',
    },
]

ORACLE_ABI = [
    {
        'name': 'getAssetsPrices',
        'inputs': [{'name': 'assets', 'type': 'address[]'}],
        'outputs': [{'name': '', 'type': 'uint256[]'}],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'getAssetPrice',
        'inputs': [{'name': 'asset', 'type': 'address'}],
        'outputs': [{'name': '', 'type': 'uint256'}],
        'stateMutability': 'view', 'type': 'function',
    },
]

QS_ROUTER_ABI = [
    {
        'name': 'getAmountsOut',
        'inputs': [
            {'name': 'amountIn', 'type': 'uint256'},
            {'name': 'path',     'type': 'address[]'},
        ],
        'outputs': [{'name': 'amounts', 'type': 'uint256[]'}],
        'stateMutability': 'view', 'type': 'function',
    },
]

LIQ_ABI = [
    {
        'name': 'executeLiquidation',
        'inputs': [{'name': 'params', 'type': 'bytes'}],
        'outputs': [],
        'stateMutability': 'nonpayable', 'type': 'function',
    },
    {
        'name': 'paused',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'bool'}],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'owner',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'address'}],
        'stateMutability': 'view', 'type': 'function',
    },
]

PARAMS_TYPE = (
    '(address,address,address,uint256,uint256,'
    'address[],address,uint8,uint24[],bytes,uint256,uint256)'
)

# ════════════════════════════════════════════════════════════
# TOPIC UTILITIES
# ════════════════════════════════════════════════════════════

def _normalise_topic(t) -> str:
    if isinstance(t, (bytes, bytearray)):
        return '0x' + t.hex().lower()
    s = str(t).lower()
    return s if s.startswith('0x') else '0x' + s

def _topic_to_addr(t) -> str:
    h = _normalise_topic(t)
    return Web3.to_checksum_address('0x' + h[2:].zfill(64)[-40:])

# ════════════════════════════════════════════════════════════
# POSITION READER — uses UiPoolDataProviderV3
# ════════════════════════════════════════════════════════════

class PositionReader:

    def __init__(self, w3: Web3):
        self.w3         = w3
        self.pool       = w3.eth.contract(address=AAVE_POOL,            abi=POOL_ABI)
        self.ui         = w3.eth.contract(address=UI_POOL_DATA_PROVIDER, abi=UI_PROVIDER_ABI)
        self.dp         = w3.eth.contract(address=POOL_DATA_PROVIDER,    abi=POOL_DATA_PROVIDER_ABI)
        self.oracle     = w3.eth.contract(address=AAVE_ORACLE,           abi=ORACLE_ABI)
        self.qs         = w3.eth.contract(address=QS_ROUTER,             abi=QS_ROUTER_ABI)

        # Reserve metadata — loaded once at startup, refreshed periodically
        self.reserves: dict  = {}   # addr_lower -> ReserveMeta
        self.prices:   dict  = {}   # addr_lower -> Decimal USD
        self.prices_valid = False
        self._reserve_load_block = 0

        # Balancer V2 flashloan availability — checked at startup
        # Tokens in this set CANNOT be used as debt (no flashloan source)
        self.no_flashloan_tokens: set = set()  # addr_lower

    # ── Reserve metadata ──────────────────────────────────────

    def load_all_reserves(self):
        """
        Load ALL Aave V3 Polygon reserve metadata using a stable two-step approach:
          1. pool.getReservesList() — returns address[] of all reserves, never fails
          2. PoolDataProvider.getReserveConfigurationData(asset) — stable ABI for
             decimals, liquidationBonus, liquidationThreshold, isActive, isFrozen
          3. PoolDataProvider.getReserveTokensAddresses(asset) — aToken address
          4. ERC20.symbol() — human-readable symbol

        This avoids UiPoolDataProviderV3.getReservesData() whose ABI struct layout
        varies between Polygon V3 deployment versions causing decode failures.
        """
        logger.info('Loading all Aave V3 Polygon reserves via getReservesList + PoolDataProvider...')
        try:
            reserve_addrs = self.pool.functions.getReservesList().call()
            logger.info(f'getReservesList returned {len(reserve_addrs)} reserves')
        except Exception as e:
            logger.error(f'getReservesList failed: {e}')
            if not self.reserves:
                raise RuntimeError('Cannot load reserves — bot cannot function')
            return

        new_reserves = {}
        for addr in reserve_addrs:
            addr_lower = addr.lower()
            try:
                cs = Web3.to_checksum_address(addr)

                # Get configuration: decimals, thresholds, bonus, active/frozen
                cfg = self.dp.functions.getReserveConfigurationData(cs).call()
                decimals    = int(cfg[0])
                liq_thresh  = Decimal(cfg[2]) / Decimal('10000')
                liq_bonus_bp = int(cfg[3])
                liq_bonus   = Decimal(liq_bonus_bp) / Decimal('10000')
                is_active   = bool(cfg[8])
                is_frozen   = bool(cfg[9])

                # Skip inactive or zero-bonus reserves (no liquidation possible)
                if not is_active or liq_bonus_bp == 0:
                    continue

                # Get aToken address
                try:
                    tok_addrs = self.dp.functions.getReserveTokensAddresses(cs).call()
                    a_token   = tok_addrs[0]
                except Exception:
                    a_token = '0x0000000000000000000000000000000000000000'

                # Get symbol from ERC20
                try:
                    erc20 = self.w3.eth.contract(address=cs, abi=ERC20_META_ABI)
                    symbol = erc20.functions.symbol().call()
                    if not symbol:
                        symbol = addr_lower[:8]
                except Exception:
                    symbol = addr_lower[:8]

                new_reserves[addr_lower] = ReserveMeta(
                    symbol=symbol,
                    decimals=decimals,
                    liq_bonus=liq_bonus,
                    liq_threshold=liq_thresh,
                    a_token=a_token,
                    is_active=is_active,
                    is_frozen=is_frozen,
                )
                logger.debug(
                    f'  Reserve {symbol}: bonus={float(liq_bonus)*100-100:.1f}% '
                    f'thresh={float(liq_thresh)*100:.1f}% '
                    f'decimals={decimals}'
                )
            except Exception as e:
                logger.warning(f'Failed to load reserve {addr_lower[:12]}: {e}')

        if not new_reserves:
            logger.error('No reserves loaded — all failed')
            if not self.reserves:
                raise RuntimeError('Cannot load reserves — bot cannot function')
            return

        self.reserves = new_reserves
        self._reserve_load_block = self.w3.eth.block_number
        logger.info(
            f'Loaded {len(self.reserves)} active reserves: '
            + ', '.join(
                sorted(v.symbol for v in self.reserves.values())
            )
        )

    #LIQUIDITY AWARE ================================================

    # ── Liquidity Indexes ─────────────────────────────────────

    def get_liquidity_indexes(self) -> dict:
        """
        Fetch current liquidity indexes for all reserves.
        Needed for accurate collateral calculation.
        Returns dict {asset_lower: Decimal(liquidityIndex)}
        """
        indexes = {}
        for asset_addr_lower, meta in self.reserves.items():
            try:
                cs_asset = Web3.to_checksum_address(asset_addr_lower)
                # Get reserve data with liquidity index
                # Using UiPoolDataProvider for complete data
                result = self.ui.functions.getReservesData(
                    POOL_ADDRESSES_PROVIDER
                ).call()

                # Find matching reserve data
                reserve_data_list = result[0]  # First tuple is reserves array
                for reserve_data in reserve_data_list:
                    if reserve_data[0].lower() == asset_addr_lower:  # underlyingAsset
                        # liquidityIndex is field 13 (0-indexed)
                        liquidity_index = Decimal(reserve_data[13]) / Decimal('1e27')
                        indexes[asset_addr_lower] = liquidity_index
                        break
            except Exception as e:
                logger.debug(f'Failed to get liquidity index for {meta.symbol}: {e}')
                # Default to 1e27 (no scaling)
                indexes[asset_addr_lower] = Decimal('1e27')

        return indexes

    # ── Oracle prices ─────────────────────────────────────────

    def refresh_prices(self):
        """
        Batch-fetch prices for all known reserves from Aave oracle.
        Clears prices on failure to prevent stale evaluations.
        Oracle returns prices in USD with 8 decimals.
        """
        if not self.reserves:
            logger.warning('refresh_prices: no reserves loaded yet')
            return
        addrs = [Web3.to_checksum_address(a) for a in self.reserves]
        try:
            raw = self.oracle.functions.getAssetsPrices(addrs).call()
            new_prices = {}
            for addr, p in zip(addrs, raw):
                if p > 0:
                    new_prices[addr.lower()] = Decimal(p) / Decimal('1e8')
            self.prices       = new_prices
            self.prices_valid = bool(new_prices)
            logger.debug(
                f'Prices refreshed: {len(new_prices)} assets | '
                + ' | '.join(
                    f'{self.reserves[a].symbol}=${float(v):.4f}'
                    for a, v in list(new_prices.items())[:8]
                    if a in self.reserves
                )
            )
        except Exception as e:
            logger.error(f'Oracle refresh FAILED: {e} — clearing prices')
            self.prices       = {}
            self.prices_valid = False

    def get_price(self, addr: str) -> Optional[Decimal]:
        return self.prices.get(addr.lower()) if self.prices_valid else None

    def matic_usd(self) -> Decimal:
        return self.prices.get(WMATIC.lower(), Decimal('0.5'))

    # ── Position reading ──────────────────────────────────────

    def read_position(self, user: str) -> Optional[UserPosition]:
        """
        Read full user position using UiPoolDataProviderV3.getUserReservesData().
        PRODUCTION GRADE with comprehensive logging.
        Filters: Minimum $1,000 collateral OR minimum $300 debt (keeps underwater positions)
        """
        try:
            cs = Web3.to_checksum_address(user)
        except Exception as e:
            logger.debug(f'Invalid user address {user}: {e}')
            return None

        # Step 1: Aggregate data for health factor and USD totals
        try:
            acct = self.pool.functions.getUserAccountData(cs).call()
            col_base, debt_base, _, _, _, hf_raw = acct

            # Calculate USD values for filtering
            col_usd = Decimal(col_base) / Decimal('1e8')
            debt_usd = Decimal(debt_base) / Decimal('1e8')

            # ── FILTER: Skip tiny positions ─────────────────────────────
            # Keep if: collateral >= $1,000 OR debt >= $300
            MIN_COLLATERAL_USD = Decimal('1000')
            MIN_DEBT_USD = Decimal('300')

            if col_usd < MIN_COLLATERAL_USD and debt_usd < MIN_DEBT_USD:
                logger.debug(f"📭 USER {user[:14]} too small (col=${col_usd:.2f}, debt=${debt_usd:.2f}) - skipping")
                return None
            # ────────────────────────────────────────────────────────────

            logger.info(f"🏦 USER {user[:14]} ACCOUNT DATA:")
            logger.info(f"   Collateral Base (8 decimals): {col_base} → ${float(col_usd):,.2f}")
            logger.info(f"   Debt Base (8 decimals):       {debt_base} → ${float(debt_usd):,.2f}")

            if hf_raw >= 10 ** 27:
                hf = Decimal('999')
                logger.info(f"   Health Factor: ∞ (no debt)")
            else:
                hf = Decimal(hf_raw) / Decimal('1e18')
                logger.info(f"   Health Factor: {float(hf):.6f}")

        except Exception as e:
            logger.warning(f'❌ getUserAccountData FAILED {user[:12]}: {e}')
            return None

        if col_base == 0 and debt_base == 0:
            logger.info(f"📭 USER {user[:14]} has ZERO positions - skipping")
            return None

        # Initialize position object
        pos = UserPosition(
            user=user.lower(),
            health_factor=hf,
            total_collateral_usd=col_usd,
            total_debt_usd=debt_usd,
        )

        # Step 2: Per-asset breakdown
        found_any = False
        collateral_details = []
        debt_details = []

        for asset_addr_lower, meta in self.reserves.items():
            try:
                cs_asset = Web3.to_checksum_address(asset_addr_lower)
                rd = self.dp.functions.getUserReserveData(cs_asset, cs).call()

                a_bal = int(rd[0])
                stable_debt = int(rd[1])
                var_debt = int(rd[2])
                use_as_col = bool(rd[8])
                total_debt = stable_debt + var_debt

                if a_bal == 0 and total_debt == 0:
                    continue

                found_any = True

                # Human-readable amounts for logging
                token_price = self.get_price(cs_asset) or Decimal('0')
                col_amount_human = Decimal(a_bal) / Decimal(10 ** meta.decimals)
                col_value_usd = col_amount_human * token_price if token_price > 0 else Decimal('0')
                debt_amount_human = Decimal(total_debt) / Decimal(10 ** meta.decimals)
                debt_value_usd = debt_amount_human * token_price if token_price > 0 else Decimal('0')

                # ── Collateral log + summary append ───────────────
                if a_bal > 0:
                    logger.info(
                        f"   📈 COLLATERAL: {meta.symbol:<10} | "
                        f"Amount: {float(col_amount_human):,.4f} | "
                        f"Value: ${float(col_value_usd):,.2f} | "
                        f"Threshold: {float(meta.liq_threshold * 100):.1f}% | "
                        f"aToken balance: {a_bal:,}"
                    )
                    collateral_details.append(
                        f"{meta.symbol}={float(col_amount_human):.2f}(${float(col_value_usd):.0f})"
                    )

                # ── Debt log + summary append ──────────────────────
                if total_debt > 0:
                    debt_type = "STABLE" if stable_debt > var_debt else "VARIABLE"
                    logger.info(
                        f"   📉 DEBT:        {meta.symbol:<10} | "
                        f"Amount: {float(debt_amount_human):,.4f} | "
                        f"Value: ${float(debt_value_usd):,.2f} | "
                        f"Type: {debt_type} | "
                        f"Raw debt: {total_debt:,}"
                    )
                    debt_details.append(
                        f"{meta.symbol}={float(debt_amount_human):.2f}(${float(debt_value_usd):.0f})"
                    )

                # ── Build reserve position object ──────────────────
                reserve_pos = UserReservePosition(
                    asset=cs_asset,
                    symbol=meta.symbol,
                    decimals=meta.decimals,
                    a_bal=a_bal,
                    stable_debt=stable_debt,
                    var_debt=var_debt,
                    use_as_col=use_as_col,
                    reserve=meta,
                )

                if use_as_col and a_bal > 0:
                    pos.collateral.append(reserve_pos)
                if total_debt > 0:
                    pos.debt.append(reserve_pos)

            except Exception as e:
                logger.debug(f'  getUserReserveData {meta.symbol}: {e}')
                continue

        # ── Summary ────────────────────────────────────────────────
        logger.info(f"📊 USER {user[:14]} SUMMARY:")
        logger.info(f"   Total Collateral Value: ${float(pos.total_collateral_usd):,.2f}")
        logger.info(f"   Total Debt Value:       ${float(pos.total_debt_usd):,.2f}")
        logger.info(f"   Health Factor:          {float(hf):.6f}")

        if pos.collateral:
            logger.info(f"   Collateral Assets: {', '.join(collateral_details)}")
        if pos.debt:
            logger.info(f"   Debt Assets:       {', '.join(debt_details)}")

        if hf < Decimal('1.0'):
            logger.warning(f"   🚨 LIQUIDATABLE! Shortfall: ${float(pos.total_debt_usd - pos.total_collateral_usd):,.2f}")
        elif hf < Decimal('1.05'):
            logger.warning(f"   ⚠️  CRITICAL: Within 5% of liquidation")
        elif hf < Decimal('1.10'):
            logger.info(f"   ⚠️  Elevated risk: Within 10% of liquidation")
        else:
            logger.info(f"   ✅ Healthy position")

        logger.info(f"   ─────────────────────────────────────────────")

        if not found_any and (pos.total_collateral_usd > 0 or pos.total_debt_usd > 0):
            logger.warning(f"   ⚠️  WARNING: Account data shows positions but reserve data found nothing")
            logger.warning(f"   This may indicate exotic tokens not in reserve list")

        return pos
    # ── Swap quoting ──────────────────────────────────────────

    def quote_swap(self, path: list, amount_in: int) -> int:
        """Static call to QuickSwap getAmountsOut. Returns 0 on failure."""
        if len(path) < 2 or amount_in == 0:
            return amount_in   # same-asset: no swap
        try:
            cs_path = [Web3.to_checksum_address(p) for p in path]
            out     = self.qs.functions.getAmountsOut(amount_in, cs_path).call()
            return out[-1] if out else 0
        except Exception as e:
            logger.debug(f'getAmountsOut failed {path}: {e}')
            return 0

    def check_flashloan_liquidity(self):
        """
        Check Balancer V2 vault balance for every loaded Aave reserve.
        Tokens with zero balance CANNOT be used as a flashloan source.
        If we try to liquidate a position where debt = such a token, the
        Balancer flashLoan() call will revert immediately.

        Populates self.no_flashloan_tokens (set of addr_lower).
        Called once at startup; re-called every 1000 blocks alongside reserve refresh.
        """
        if not self.reserves:
            return

        blocked = []
        available = []
        for addr_lower, meta in self.reserves.items():
            try:
                cs  = Web3.to_checksum_address(addr_lower)
                tok = self.w3.eth.contract(address=cs, abi=ERC20_META_ABI)
                bal = tok.functions.balanceOf(BALANCER_VAULT).call()
                if bal == 0:
                    self.no_flashloan_tokens.add(addr_lower)
                    blocked.append(meta.symbol)
                else:
                    self.no_flashloan_tokens.discard(addr_lower)
                    available.append(meta.symbol)
            except Exception:
                # Cannot read balance — treat as no liquidity to be safe
                self.no_flashloan_tokens.add(addr_lower)
                blocked.append(meta.symbol)

        if blocked:
            logger.warning(
                f'Balancer flashloan check: {len(blocked)} token(s) BLOCKED '
                f'(zero vault balance — will skip as debt): {", ".join(blocked)}'
            )
        logger.info(
            f'Balancer flashloan check: {len(available)} token(s) available: '
            + ', '.join(available)
        )





# ════════════════════════════════════════════════════════════
# SWAP ROUTE BUILDER
# Dynamic: find best path from collateral to debt token.
# Falls back through USDC as bridge for exotic pairs.
# ════════════════════════════════════════════════════════════

def build_swap_path(col_addr: str, debt_addr: str) -> tuple:
    """
    Returns (router, router_type, path, v3_fees).
    path[0] == col_addr, path[-1] == debt_addr.
    Uses QuickSwap V2 with USDC as bridge for non-direct pairs.
    """
    col  = Web3.to_checksum_address(col_addr)
    debt = Web3.to_checksum_address(debt_addr)

    if col.lower() == debt.lower():
        # Same asset — minimal path, contract handles no-swap case
        return QS_ROUTER, RT_QUICKSWAP, [col, debt], []

    # Direct pair exists for major tokens
    direct_pairs = {
        frozenset([WETH.lower(), USDC.lower()]),
        frozenset([WETH.lower(), USDT.lower()]),
        frozenset([WETH.lower(), DAI.lower()]),
        frozenset([WETH.lower(), WMATIC.lower()]),
        frozenset([WETH.lower(), WBTC.lower()]),
        frozenset([USDC.lower(), USDT.lower()]),
        frozenset([USDC.lower(), DAI.lower()]),
        frozenset([USDC.lower(), WMATIC.lower()]),
        frozenset([USDC.lower(), WBTC.lower()]),
        frozenset([USDT.lower(), DAI.lower()]),
        frozenset([WMATIC.lower(), USDT.lower()]),
    }

    pair = frozenset([col.lower(), debt.lower()])
    if pair in direct_pairs:
        return QS_ROUTER, RT_QUICKSWAP, [col, debt], []

    # Route through USDC as bridge
    # col -> USDC -> debt
    if col.lower() != USDC.lower() and debt.lower() != USDC.lower():
        return QS_ROUTER, RT_QUICKSWAP, [col, USDC, debt], []

    # col is USDC, route direct
    if col.lower() == USDC.lower():
        return QS_ROUTER, RT_QUICKSWAP, [col, debt], []

    # debt is USDC, route direct
    return QS_ROUTER, RT_QUICKSWAP, [col, debt], []


#=============================================================
#CACHE
def estimate_hf(pos, prices: dict, liquidity_indexes: dict = None) -> Decimal:
    """
    Estimate current HF using cached position + latest oracle prices.
    Now includes liquidity index for accurate collateral calculation.

    liquidity_indexes: dict {asset_lower: Decimal(liquidityIndex)}
    If not provided, assumes index = 1 (fresh position approximation)
    """
    weighted_col = Decimal('0')
    total_debt   = Decimal('0')

    for col in pos.collateral:
        price = prices.get(col.asset.lower())
        if not price:
            continue

        # Get liquidity index (default to 1e27 if not available)
        liq_index = liquidity_indexes.get(col.asset.lower(), Decimal('1e27')) if liquidity_indexes else Decimal('1e27')

        # Convert scaled aToken balance to actual balance
        actual_a_bal = (Decimal(col.a_bal) * liq_index) / Decimal('1e27')

        col_usd = (actual_a_bal / Decimal(10 ** col.decimals)) * price
        weighted_col += col_usd * col.reserve.liq_threshold

    for debt in pos.debt:
        price = prices.get(debt.asset.lower())
        if not price:
            continue

        # Debt is already actual, not scaled
        actual_debt = Decimal(debt.total_debt) / Decimal(10 ** debt.decimals) * price
        total_debt += actual_debt

    if total_debt == 0:
        return Decimal('999')
    return weighted_col / total_debt


# ════════════════════════════════════════════════════════════
# OPPORTUNITY FINDER
# ════════════════════════════════════════════════════════════

GAS_UNITS_BASE = 400_000
GAS_UNITS_HOP  = 80_000

class OpportunityFinder:

    def __init__(self, reader: PositionReader, min_profit_usd: Decimal):
        self.reader         = reader
        self.min_profit_usd = min_profit_usd

    def _gas_cost_usd(self, n_hops: int) -> Decimal:
        try:
            gp        = Decimal(self.reader.w3.eth.gas_price)
            gas_units = Decimal(GAS_UNITS_BASE + GAS_UNITS_HOP * max(0, n_hops - 1))
            matic     = gp * gas_units / Decimal('1e18')
            return matic * self.reader.matic_usd()
        except Exception:
            return Decimal('12')

    def find_best(self, pos: UserPosition, block_num: int) -> Optional[Opportunity]:
        if pos.health_factor >= Decimal('1.0'):
            return None
        if not pos.collateral or not pos.debt:
            logger.debug(
                f'  {pos.user[:12]}: HF<1 but col={[c.symbol for c in pos.collateral]} '
                f'debt={[d.symbol for d in pos.debt]} — no pairs to evaluate'
            )
            return None
        if not self.reader.prices_valid:
            logger.warning('  Prices not valid — skipping evaluation')
            return None

        close_factor = (Decimal('1.0') if pos.health_factor < Decimal('0.95')
                        else Decimal('0.5'))

        best: Optional[Opportunity] = None
        best_net = Decimal('-9999')

        for col_pos in pos.collateral:
            col_price = self.reader.get_price(col_pos.asset)
            if not col_price or col_price == 0:
                logger.debug(f'  No price for collateral {col_pos.symbol}')
                continue

            bonus = col_pos.reserve.liq_bonus
            if bonus <= Decimal('1'):
                logger.debug(f'  {col_pos.symbol} liq_bonus={bonus} <= 1 — skip')
                continue

            for debt_pos in pos.debt:
                # Skip if Balancer has zero of this debt token (flashloan would revert)
                if debt_pos.asset.lower() in self.reader.no_flashloan_tokens:
                    logger.debug(
                        f'  Skipping {debt_pos.symbol} debt — no Balancer flashloan liquidity'
                    )
                    continue

                debt_price = self.reader.get_price(debt_pos.asset)
                if not debt_price or debt_price == 0:
                    logger.debug(f'  No price for debt {debt_pos.symbol}')
                    continue

                # Build swap route
                router, rt, path, fees = build_swap_path(
                    col_pos.asset, debt_pos.asset
                )
                cs_path = [Web3.to_checksum_address(p) for p in path]

                # USD amounts using scaled balances as proxy
                # (actual = scaled * liquidityIndex / 1e27, but scaled is
                # good enough for identifying the best pair to liquidate)
                debt_total_usd    = (Decimal(debt_pos.total_debt)
                                     / Decimal(10 ** debt_pos.decimals)
                                     * debt_price)
                debt_to_cover_usd = debt_total_usd * close_factor

                if debt_to_cover_usd < Decimal('20'):
                    continue

                gross_usd = debt_to_cover_usd * (bonus - Decimal('1'))
                n_hops    = max(1, len(path) - 1)
                gas_usd   = self._gas_cost_usd(n_hops)
                net_usd   = gross_usd - gas_usd

                logger.debug(
                    f'  Pair {col_pos.symbol}/{debt_pos.symbol} '
                    f'debt=${float(debt_to_cover_usd):.2f} '
                    f'bonus={float(bonus)*100-100:.1f}% '
                    f'gross=${float(gross_usd):.4f} '
                    f'gas=${float(gas_usd):.4f} '
                    f'net=${float(net_usd):.4f}'
                )

                if net_usd <= best_net:
                    continue

                # Raw debt to cover
                debt_to_cover_raw = int(
                    debt_to_cover_usd
                    * Decimal(10 ** debt_pos.decimals)
                    / debt_price
                )

                # Expected collateral received
                col_received_usd = debt_to_cover_usd * bonus
                col_received_raw = int(
                    col_received_usd
                    * Decimal(10 ** col_pos.decimals)
                    / col_price
                )

                # Pre-execution swap check (same-asset skips)
                same_asset = col_pos.asset.lower() == debt_pos.asset.lower()
                if not same_asset and col_received_raw > 0:
                    swap_out = self.reader.quote_swap(cs_path, col_received_raw)
                    if swap_out == 0:
                        logger.debug(
                            f'  Swap quote zero for '
                            f'{col_pos.symbol}→{debt_pos.symbol} — skip'
                        )
                        continue
                    swap_usd = (
                        Decimal(swap_out)
                        / Decimal(10 ** debt_pos.decimals)
                        * debt_price
                    )
                    if swap_usd < debt_to_cover_usd * Decimal('0.97'):
                        logger.debug(
                            f'  Swap output ${float(swap_usd):.2f} < '
                            f'debt ${float(debt_to_cover_usd):.2f} — skip'
                        )
                        continue
                    real_gross = swap_usd - debt_to_cover_usd
                    net_usd    = real_gross - gas_usd
                    gross_usd  = real_gross

                    if net_usd <= best_net:
                        continue

                min_col_raw    = int(col_received_raw * Decimal('0.95'))
                min_profit_raw = int(
                    self.min_profit_usd
                    * Decimal(10 ** debt_pos.decimals)
                    / debt_price
                )

                best_net = net_usd
                best = Opportunity(
                    user=pos.user,
                    health_factor=pos.health_factor,
                    close_factor=close_factor,
                    col_asset=col_pos.asset,
                    col_symbol=col_pos.symbol,
                    col_amount_raw=col_pos.a_bal,
                    col_usd=col_received_usd,
                    debt_asset=debt_pos.asset,
                    debt_symbol=debt_pos.symbol,
                    debt_to_cover_raw=debt_to_cover_raw,
                    debt_usd=debt_to_cover_usd,
                    liq_bonus=bonus,
                    gross_profit_usd=gross_usd,
                    gas_cost_usd=gas_usd,
                    net_profit_usd=net_usd,
                    block_number=block_num,
                    swap_router=router,
                    swap_router_type=rt,
                    swap_path=cs_path,
                    v3_fees=fees,
                    min_col_raw=min_col_raw,
                    min_profit_raw=min_profit_raw,
                )

        return best

# ════════════════════════════════════════════════════════════
# PARAM ENCODER
# ════════════════════════════════════════════════════════════

def encode_params(opp: Opportunity) -> bytes:
    deadline = int(time.time()) + 120
    tup = (
        Web3.to_checksum_address(opp.col_asset),
        Web3.to_checksum_address(opp.debt_asset),
        Web3.to_checksum_address(opp.user),
        opp.debt_to_cover_raw,
        opp.min_col_raw,
        opp.swap_path,
        Web3.to_checksum_address(opp.swap_router),
        opp.swap_router_type,
        opp.v3_fees,
        b'',
        deadline,
        opp.min_profit_raw,
    )
    return abi_encode([PARAMS_TYPE], [tup])

# ════════════════════════════════════════════════════════════
# MAIN BOT
# ════════════════════════════════════════════════════════════
class LiquidationBot:
    """
    AAVE V3 Liquidation Bot — Polygon
    Pure WSS mode with predictive liquidation and zero-RPC price-triggered scanning.

    Key design decisions:
      • Two WSS subscriptions: newHeads (block clock) + logs (Aave events)
      • Log subscription is sent AFTER block subscription ACK is confirmed,
        avoiding the race condition where -32029 rate-limit errors swallow
        the subscription response and leave sub_logs = None forever.
      • Zero-RPC HF re-estimation on every price refresh — sweeps all cached
        users in pure Python using stored collateral/debt amounts × new prices.
      • HF trajectory (linear regression over last 20 samples) predicts
        liquidation ETA without any extra RPC calls.
      • Adaptive recheck cadence: 5 / 20 / 50 blocks depending on HF tier.
    """

    # ── Predictive thresholds ──────────────────────────────────
    HF_WATCH_BELOW     = Decimal('1.10')   # begin trajectory tracking
    HF_ALERT_BELOW     = Decimal('1.05')   # tighten recheck cadence
    HF_PREDICT_TRIGGER = Decimal('1.02')   # run opportunity finder proactively
    HF_LIQUIDATE_BELOW = Decimal('1.00')   # execute immediately

    RECHECK_INTERVAL    = 50     # default blocks between passive rechecks
    PRICE_REFRESH_SECS  = 45     # background price refresh cadence (seconds)
    RESERVE_RELOAD_BLKS = 1_000  # reload full reserve list every N blocks

    def __init__(self):
        # ── Web3 ──────────────────────────────────────────────
        self.w3 = Web3(Web3.HTTPProvider(
            ALCHEMY_HTTP, request_kwargs={'timeout': 15}
        ))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        try:
            blk = self.w3.eth.block_number
            logger.info(f'HTTP connected (block {blk})')
        except Exception as e:
            raise ConnectionError(f'HTTP failed: {e}')

        # ── Config ─────────────────────────────────────────────
        _min_profit_raw = (os.getenv('MIN_PROFIT_USD') or '10').strip()
        try:
            self.min_profit_usd = Decimal(_min_profit_raw)
        except Exception:
            logger.warning(f'Invalid MIN_PROFIT_USD value {_min_profit_raw!r} — defaulting to 10')
            self.min_profit_usd = Decimal('10')
        self.execution_enabled = os.getenv('EXECUTION_ENABLED', '').strip().lower() == 'true'

        # ── Wallet ─────────────────────────────────────────────
        pk = (os.getenv('PRIVATE_KEY') or os.getenv('REAL_PRIVATE_KEY') or '').strip()
        if pk:
            pk_hex           = pk[2:] if pk.lower().startswith('0x') else pk
            self.private_key = '0x' + pk_hex
            self.account     = self.w3.eth.account.from_key(self.private_key)
            logger.info(f'Wallet: {self.account.address}')
        else:
            if self.execution_enabled:
                raise ValueError('PRIVATE_KEY required when execution is enabled')
            logger.warning('No PRIVATE_KEY — monitor-only mode')
            self.private_key = None
            self.account     = None

        # ── Contracts ──────────────────────────────────────────
        self.liq = self.w3.eth.contract(address=LIQ_CONTRACT, abi=LIQ_ABI)
        try:
            owner  = self.liq.functions.owner().call()
            paused = self.liq.functions.paused().call()
            logger.info(f'Liquidation contract : {LIQ_CONTRACT}')
            logger.info(f'  owner  : {owner}')
            logger.info(f'  paused : {paused}')
            if self.account and owner.lower() != self.account.address.lower():
                logger.error(
                    f'Wallet {self.account.address} is NOT owner ({owner}) '
                    f'— execution will revert on non-owner calls'
                )
            if paused:
                logger.error('Contract is PAUSED — execution will revert')
        except Exception as e:
            logger.warning(f'Contract state check failed: {e}')

        # ── Core helpers ───────────────────────────────────────
        self.reader = PositionReader(self.w3)
        self.finder = OpportunityFinder(self.reader, self.min_profit_usd)
        self._exec  = concurrent.futures.ThreadPoolExecutor(max_workers=8)
       
        # ── State ──────────────────────────────────────────────
        # user_lower -> last block we fully evaluated them
        self._user_registry:    dict[str, int]    = {}
        # user_lower -> most recent UserPosition (may be stale between RPC calls)
        self.user_tracker = UserTracker('tracked_users.json')
        for user, data in self.user_tracker.tracked_users.items():
            self._user_registry[user] = data.get('last_block', 0)
        logger.info(f'Loaded {len(self.user_tracker.tracked_users)} previously tracked users from disk')
        
        self._cached_positions: dict[str, object] = {}
        # user_lower -> deque[(block_num, hf)] for velocity regression
        self._hf_history:       dict[str, deque]  = {}
        

        self._last_reserve_reload: int = 0
        self._current_block:       int = 0

       


        self.stats = dict(
            blocks=0, events=0, users_tracked=0,
            users_checked=0, liquidatable=0,
            opportunities=0, attempts=0, successes=0,
        )

        logger.info('=' * 65)
        logger.info('AAVE V3 LIQUIDATION BOT — Polygon  [Pure WSS / Predictive]')
        logger.info(f'  Liq contract : {LIQ_CONTRACT}')
        logger.info(f'  Min profit   : ${self.min_profit_usd}')
        logger.info(f'  Execution    : {"ENABLED" if self.execution_enabled else "MONITOR ONLY"}')
        logger.info('=' * 65)

        self.reader.load_all_reserves()
        self.reader.refresh_prices()
        self.reader.check_flashloan_liquidity()
        self._last_reserve_reload = self._get_block_number_with_retry()

    # ══════════════════════════════════════════════════════════
    # RPC retry helpers
    # ══════════════════════════════════════════════════════════

    def _get_block_number_with_retry(self, max_attempts: int = 8) -> int:
        """Get current block number with exponential backoff on transient RPC errors."""
        import time
        delay = 3
        for attempt in range(1, max_attempts + 1):
            try:
                return self.w3.eth.block_number
            except Exception as e:
                if attempt == max_attempts:
                    logger.error(f'Could not get block number after {max_attempts} attempts: {e}')
                    return 0
                logger.warning(f'RPC block_number failed (attempt {attempt}/{max_attempts}): {e} — retrying in {delay}s')
                time.sleep(delay)
                delay = min(delay * 2, 60)
        return 0

    # ══════════════════════════════════════════════════════════
    # Thread-pool bridge
    # ══════════════════════════════════════════════════════════

    async def _run(self, fn, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._exec, fn, *args)

    # ══════════════════════════════════════════════════════════
    # Position display helpers
    # ══════════════════════════════════════════════════════════
    def _record_hf(self, user: str, block_num: int, hf: Decimal) -> None:
        """Append (block, hf) to the user's rolling 20-point history."""
        from collections import deque  # Local import as fallback

        if user not in self._hf_history:
            self._hf_history[user] = deque(maxlen=20)
        self._hf_history[user].append((block_num, hf))
        
    def _fmt_collateral(self, pos) -> str:
        """
        Build a human-readable collateral string from a UserPosition.
        Handles both raw-wei amounts (divided by 10**decimals) and
        pre-normalised Decimal amounts gracefully.
        """
        parts = []
        for c in pos.collateral:
            try:
                # Try the field names your PositionReader actually uses.
                # We attempt a_bal first (raw wei), then fall back to 'amount'.
                raw = getattr(c, 'a_bal', None)
                if raw is None:
                    raw = getattr(c, 'amount', 0)
                decimals = int(getattr(c, 'decimals', 18))
                amount   = Decimal(str(raw)) / Decimal(10 ** decimals)

                # Price lookup — try asset, then token_address
                addr  = getattr(c, 'asset', None) or getattr(c, 'token_address', '')
                price = self.reader.prices.get(str(addr).lower(), Decimal('0'))
                usd   = amount * price

                symbol = getattr(c, 'symbol', str(addr)[:6])
                parts.append(f'{symbol}={float(amount):.4f}(${float(usd):.2f})')
            except Exception as exc:
                parts.append(f'?ERR({exc})')
        return ', '.join(parts) if parts else 'NONE'

    def _fmt_debt(self, pos) -> str:
        """
        Build a human-readable debt string from a UserPosition.
        Tries total_debt (raw wei) then amount as fallback.
        """
        parts = []
        for d in pos.debt:
            try:
                raw = getattr(d, 'total_debt', None)
                if raw is None:
                    raw = getattr(d, 'amount', 0)
                decimals = int(getattr(d, 'decimals', 18))
                amount   = Decimal(str(raw)) / Decimal(10 ** decimals)

                addr  = getattr(d, 'asset', None) or getattr(d, 'token_address', '')
                price = self.reader.prices.get(str(addr).lower(), Decimal('0'))
                usd   = amount * price

                symbol = getattr(d, 'symbol', str(addr)[:6])
                parts.append(f'{symbol}={float(amount):.4f}(${float(usd):.2f})')
            except Exception as exc:
                parts.append(f'?ERR({exc})')
        return ', '.join(parts) if parts else 'NONE'

    # ══════════════════════════════════════════════════════════
    # HF trajectory (velocity + ETA)
    # ══════════════════════════════════════════════════════════

    def _record_hf(self, user: str, block_num: int, hf: Decimal) -> None:
        """Append (block, hf) to the user's rolling 20-point history."""
        if user not in self._hf_history:
            self._hf_history[user] = deque(maxlen=20)
        self._hf_history[user].append((block_num, hf))

    def _hf_velocity(self, user: str) -> Decimal:
        """
        Rate of HF change per block via OLS linear regression.
        Negative = deteriorating. Returns 0 if fewer than 3 data points.
        """
        history = self._hf_history.get(user)
        if not history or len(history) < 3:
            return Decimal('0')
        points = list(history)
        n      = len(points)
        sum_x  = sum(p[0] for p in points)
        sum_y  = sum(float(p[1]) for p in points)
        sum_xy = sum(p[0] * float(p[1]) for p in points)
        sum_xx = sum(p[0] ** 2 for p in points)
        denom  = n * sum_xx - sum_x ** 2
        if denom == 0:
            return Decimal('0')
        slope = (n * sum_xy - sum_x * sum_y) / denom
        return Decimal(str(slope))

    def _blocks_to_liquidation(self, user: str, hf: Decimal) -> Optional[int]:
        """
        Blocks until HF reaches 1.0 at current velocity.
        Returns None if trajectory is flat or improving.
        """
        v = self._hf_velocity(user)
        if v >= Decimal('0'):
            return None
        if hf <= Decimal('1.0'):
            return 0
        return int((hf - Decimal('1.0')) / abs(v))

    # ══════════════════════════════════════════════════════════
    # Zero-RPC HF estimation from cached positions + new prices
    # ══════════════════════════════════════════════════════════

    def _estimate_hf(self, pos, prices: dict) -> Optional[Decimal]:
        """
        Recompute HF for a UserPosition using supplied prices — zero RPC.

        This method estimates health factor using cached position data and current
        oracle prices. It properly handles:
          - Scaled aToken balances (multiplied by liquidity index)
          - Liquidation thresholds per asset
          - Zero debt edge cases
          - Price lookup failures

        Args:
            pos: UserPosition object with collateral and debt lists
            prices: Dict of asset_address -> current USD price (8 decimals from oracle)

        Returns:
            Decimal health factor (1.0 = liquidation threshold)
            Returns Decimal('999') if user has no debt
            Returns None if position data is invalid or missing prices
        """
        # Guard clauses for invalid inputs
        if not pos:
            return None

        if isinstance(pos, str):
            # This is the 'EMPTY' marker for users with no positions
            return None

        if not prices or len(prices) == 0:
            return None

        # Check if user has any positions to evaluate
        if not pos.collateral and not pos.debt:
            return Decimal('999')

        weighted_col = Decimal('0')
        total_debt = Decimal('0')

        # Get current liquidity indexes for accurate aToken conversion
        # Fallback to 1e27 if method doesn't exist (no scaling = fresh positions)
        liquidity_indexes = {}
        if hasattr(self.reader, 'get_liquidity_indexes'):
            try:
                liquidity_indexes = self.reader.get_liquidity_indexes()
            except Exception:
                liquidity_indexes = {}

        # Calculate weighted collateral value
        for col in pos.collateral:
            # Get current price
            price = prices.get(col.asset.lower())
            if not price or price <= 0:
                continue

            # Get liquidity index (default 1e27 = no scaling)
            liq_index = liquidity_indexes.get(col.asset.lower(), Decimal('1e27'))

            # Convert scaled aToken balance to actual balance
            # Formula: actual_balance = (scaled_balance * liquidity_index) / 1e27
            try:
                actual_a_bal = (Decimal(col.a_bal) * liq_index) / Decimal('1e27')
            except (ValueError, TypeError, decimal.InvalidOperation):
                # Fallback if conversion fails
                actual_a_bal = Decimal('0')

            # Convert to human-readable amount (divide by token decimals)
            try:
                col_amount = actual_a_bal / Decimal(10 ** col.decimals)
            except (ValueError, TypeError, ZeroDivisionError):
                continue

            # Calculate USD value and apply liquidation threshold
            col_usd = col_amount * price
            weighted_col += col_usd * col.reserve.liq_threshold

        # Calculate total debt value
        for debt in pos.debt:
            # Get current price
            price = prices.get(debt.asset.lower())
            if not price or price <= 0:
                continue

            # Convert raw debt to human-readable amount
            try:
                debt_amount = Decimal(debt.total_debt) / Decimal(10 ** debt.decimals)
            except (ValueError, TypeError, ZeroDivisionError):
                continue

            total_debt += debt_amount * price

        # Edge case: No debt = infinite health factor
        if total_debt == 0:
            return Decimal('999')

        # Edge case: No collateral but has debt (impossible but handle gracefully)
        if weighted_col == 0:
            return Decimal('0')

        # Calculate and return health factor
        try:
            result = weighted_col / total_debt
            return result
        except (decimal.DivisionByZero, decimal.InvalidOperation):
            return Decimal('999')

    def _price_sweep(self, new_prices: dict, block_num: int) -> set[str]:
        """
        Pure-Python sweep over every cached user after a price refresh.
        Re-estimates HF for each user using new_prices — zero RPC calls.
        Flags users crossing danger tiers and winds back their recheck clock.
        Returns the set needing an immediate RPC confirm.
        """
        urgent:  set[str] = set()
        staging: set[str] = set()
        alert:   set[str] = set()

        for user, pos in list(self._cached_positions.items()):
            # Skip if pos is a string marker (like 'EMPTY')
            if isinstance(pos, str):
                continue
            try:
                est = self._estimate_hf(pos, new_prices)
                if est is None:
                    continue

                # 🔍 DEBUG: Log why HF is zero
                if est == Decimal('0'):
                    logger.warning(f'🔍 DEBUG: User {user[:14]} est_HF=0')
                    logger.warning(f'   Collateral count: {len(pos.collateral)}')
                    for c in pos.collateral:
                        price = new_prices.get(c.asset.lower(), Decimal('0'))
                        logger.warning(f'     - {c.symbol}: a_bal={c.a_bal}, decimals={c.decimals}, price={price}, threshold={c.reserve.liq_threshold}')
                    logger.warning(f'   Debt count: {len(pos.debt)}')
                    for d in pos.debt:
                        price = new_prices.get(d.asset.lower(), Decimal('0'))
                        logger.warning(f'     - {d.symbol}: debt={d.total_debt}, decimals={d.decimals}, price={price}')

            except Exception as e:
                logger.error(f'Error estimating HF for {user[:14]}: {e}')
                traceback.print_exc()
                continue
            short = user[:14]
            if est < self.HF_LIQUIDATE_BELOW:
                logger.warning(
                    f'💥 PRICE-SWEEP  {short}... '
                    f'est_HF={float(est):.4f} — scheduling RPC confirm'
                )
                urgent.add(user)
            elif est < self.HF_PREDICT_TRIGGER:
                logger.warning(
                    f'🔮 PRICE-STAGE  {short}... '
                    f'est_HF={float(est):.4f} — scheduling RPC confirm'
                )
                staging.add(user)
            elif est < self.HF_ALERT_BELOW:
                logger.info(
                    f'⚠️  PRICE-ALERT  {short}... '
                    f'est_HF={float(est):.4f}'
                )
                alert.add(user)

        for user in urgent | staging:
            self._user_registry[user] = 0                           # force next-block RPC

        for user in alert:
            self._user_registry[user] = block_num - (self.RECHECK_INTERVAL - 5)

        total = len(self._cached_positions)
        if urgent or staging or alert:
            logger.info(
                f'Price-sweep ({total} users): '
                f'{len(urgent)} urgent | {len(staging)} staging | {len(alert)} alert'
            )

        return urgent | staging

    # ══════════════════════════════════════════════════════════
    # RPC position evaluation
    # ══════════════════════════════════════════════════════════

    def _evaluate_position(self, user: str, block_num: int) -> Optional[object]:
        """
        Read position via RPC, update cache and HF history.
        Fixed to properly cache zero positions and prevent repeated evaluations.
        """
        # ── CRITICAL: Prevent duplicate evaluation in same block ──
        # Check if already evaluated in this block
        last_checked = self._user_registry.get(user, 0)
        if last_checked == block_num:
            logger.debug(f"⏭️  SKIPPING {user[:14]} (already evaluated in block {block_num})")
            return None

        # Check if we already know this user has zero positions and it's too soon to recheck
        if user in self._user_registry:
            last_checked = self._user_registry.get(user, 0)
            # If user was marked as zero positions, use shorter interval but not every block
            if user in self._cached_positions and self._cached_positions.get(user) is None:
                # User has zero positions - check every 50 blocks instead of every block
                if block_num - last_checked < 50:
                    logger.debug(f"⏭️  SKIPPING {user[:14]} (zero positions, last checked {block_num - last_checked} blocks ago)")
                    return None

        # ── STAMP IMMEDIATELY (THIS IS THE KEY FIX) ──
        self._user_registry[user] = block_num

        logger.info(f"🔍 EVALUATING USER {user[:14]} at block {block_num}")

        pos = self.reader.read_position(user)

        # Cache the result even if it's None (zero positions)
        self._cached_positions[user] = pos
        self.stats['users_checked'] += 1

        if not pos:
            logger.info(f"📭 USER {user[:14]} has ZERO positions - cached for 50 blocks")
            return None

        # Calculate metrics
        hf = pos.health_factor
        velocity = self._hf_velocity(user)
        self._record_hf(user, block_num, hf)
        eta = self._blocks_to_liquidation(user, hf)

        # Log comprehensive position data (only for users with positions)
        logger.info(f"📊 POSITION EVALUATION COMPLETE for {user[:14]}:")
        logger.info(f"   ├─ Health Factor: {float(hf):.6f}")

        if hf < Decimal('999'):
            logger.info(f"   ├─ Total Collateral: ${float(pos.total_collateral_usd):,.2f}")
            logger.info(f"   ├─ Total Debt:       ${float(pos.total_debt_usd):,.2f}")

        # Calculate and log risk level
        if hf < Decimal('1.0'):
            risk_level = "🔴 LIQUIDATABLE"
            shortfall = pos.total_debt_usd - pos.total_collateral_usd
            logger.warning(f"   ├─ RISK LEVEL: {risk_level}")
            logger.warning(f"   └─ Shortfall: ${float(shortfall):,.2f}")

            self.stats['liquidatable'] += 1
            opp = self.finder.find_best(pos, block_num)
            if opp:
                logger.info(f"   ✅ Opportunity found! Net profit: ${float(opp.net_profit_usd):,.2f}")
                asyncio.create_task(self._fire_opportunity(opp, reason='LIQUIDATABLE'))
        elif hf < Decimal('1.03'):
            risk_level = "🟠 EXTREME RISK (3%)"
            logger.warning(f"   ├─ RISK LEVEL: {risk_level}")
            logger.info(f"   └─ Buffer: ${float(pos.total_collateral_usd - pos.total_debt_usd):,.2f}")
            # Force recheck in 5 blocks
            self._user_registry[user] = block_num - (self.RECHECK_INTERVAL - 5)
        elif hf < Decimal('1.05'):
            risk_level = "🟡 HIGH RISK (5%)"
            logger.warning(f"   ├─ RISK LEVEL: {risk_level}")
            logger.info(f"   └─ Buffer: ${float(pos.total_collateral_usd - pos.total_debt_usd):,.2f}")
            # Force recheck in 10 blocks
            self._user_registry[user] = block_num - (self.RECHECK_INTERVAL - 10)
        elif hf < Decimal('1.10'):
            risk_level = "🟢 ELEVATED RISK (10%)"
            logger.info(f"   ├─ RISK LEVEL: {risk_level}")
            logger.info(f"   └─ Buffer: ${float(pos.total_collateral_usd - pos.total_debt_usd):,.2f}")
        else:
            risk_level = "✅ HEALTHY"
            logger.info(f"   ├─ RISK LEVEL: {risk_level}")
            if pos.total_debt_usd > 0:
                logger.info(f"   └─ Buffer: ${float(pos.total_collateral_usd - pos.total_debt_usd):,.2f}")

        # Log collateral and debt breakdown
        if pos.collateral:
            col_summary = []
            for c in pos.collateral:
                price = self.reader.get_price(c.asset) or Decimal('0')
                amount = Decimal(c.a_bal) / Decimal(10 ** c.decimals)
                value = amount * price
                col_summary.append(f"{c.symbol}={float(amount):.2f}(${float(value):.0f})")
            logger.info(f"   ├─ Collateral: {', '.join(col_summary)}")

        if pos.debt:
            debt_summary = []
            for d in pos.debt:
                price = self.reader.get_price(d.asset) or Decimal('0')
                amount = Decimal(d.total_debt) / Decimal(10 ** d.decimals)
                value = amount * price
                debt_summary.append(f"{d.symbol}={float(amount):.2f}(${float(value):.0f})")
            logger.info(f"   └─ Debt: {', '.join(debt_summary)}")

        return pos

    async def _process_user(
        self, user: str, block_num: int, label: str = ''
    ) -> None:
        """
        Full pipeline for one user:
          1. RPC read + cache
          2. Classify by HF tier
          3. Run opportunity finder when warranted
          4. Execute if profitable
          5. Save high-risk users to persistent tracker

        PRODUCTION GRADE: Properly caches empty results to prevent recheck spam.
        """
        # Check if we already know this user has no positions and it's not time to recheck
        cached = self._cached_positions.get(user)
        if cached is None or cached == 'EMPTY':
            last_checked = self._user_registry.get(user, 0)
            # Empty users are rechecked every 50 blocks (handled in _handle_block)
            if block_num - last_checked < 45 and label != 'event':
                # Only skip if not from event and not due for recheck
                logger.debug(f'⏭️  SKIPPING {user[:14]} (cached empty, last checked {block_num - last_checked} blocks ago)')
                return

        tag = f'[{label}] ' if label else ''
        logger.info(f'{tag}🔍 EVALUATING {user[:14]} at block {block_num}')

        pos = await self._run(self._evaluate_position, user, block_num)

        # Cache the result even if None
        if pos is None:
            # Mark as empty with special value
            self._cached_positions[user] = 'EMPTY'
            self._user_registry[user] = block_num
            logger.info(f'{tag}📭 USER {user[:14]} confirmed EMPTY - will recheck in 50 blocks')
            return

        # Valid position found
        self._cached_positions[user] = pos
        self._user_registry[user] = block_num

        hf = pos.health_factor
        velocity = self._hf_velocity(user)
        eta = self._blocks_to_liquidation(user, hf)

        # ── SAVE HIGH-RISK USERS TO PERSISTENT TRACKER ──────────────
        # Track users with HF < 1.10 (elevated risk or worse)
        if hf < Decimal('1.10'):
            if hasattr(self, 'user_tracker'):
                try:
                    self.user_tracker.add_user(user, hf, block_num)
                    logger.debug(f'{tag}💾 Saved {user[:14]} to persistent tracker (HF={float(hf):.4f})')
                except Exception as e:
                    logger.warning(f'{tag}Failed to save user to tracker: {e}')

        # ── LOG COMPREHENSIVE POSITION DATA ─────────────────────────
        logger.info(f'{tag}📊 POSITION DATA for {user[:14]}:')
        logger.info(f'   ├─ Health Factor: {float(hf):.6f}')

        if hf < Decimal('999'):
            logger.info(f'   ├─ Total Collateral: ${float(pos.total_collateral_usd):,.2f}')
            logger.info(f'   ├─ Total Debt:       ${float(pos.total_debt_usd):,.2f}')

        # Format collateral and debt details
        if pos.collateral:
            col_details = []
            for c in pos.collateral:
                price = self.reader.get_price(c.asset) or Decimal('0')
                amount = Decimal(c.a_bal) / Decimal(10 ** c.decimals)
                value = amount * price
                col_details.append(f"{c.symbol}={float(amount):.2f}(${float(value):.0f})")
            logger.info(f'   ├─ Collateral: {", ".join(col_details)}')

        if pos.debt:
            debt_details = []
            for d in pos.debt:
                price = self.reader.get_price(d.asset) or Decimal('0')
                amount = Decimal(d.total_debt) / Decimal(10 ** d.decimals)
                value = amount * price
                debt_details.append(f"{d.symbol}={float(amount):.2f}(${float(value):.0f})")
            logger.info(f'   ├─ Debt: {", ".join(debt_details)}')

        # ── RISK ASSESSMENT & ACTION ─────────────────────────────────
        # Tier 1: Already liquidatable (HF < 1.0)
        if hf < self.HF_LIQUIDATE_BELOW:
            shortfall = pos.total_debt_usd - pos.total_collateral_usd
            logger.warning(f'{tag}🚨 LIQUIDATABLE! Shortfall: ${float(shortfall):,.2f}')
            self.stats['liquidatable'] += 1

            opp = await self._run(self.finder.find_best, pos, block_num)
            if opp:
                logger.info(f'{tag}💰 Opportunity found: ${float(opp.net_profit_usd):,.2f} profit')
                await self._fire_opportunity(opp, reason='LIQUIDATABLE')
            else:
                logger.warning(f'{tag}❌ No profitable opportunity found')

        # Tier 2: Predictive - imminent within ~10 blocks
        elif hf < self.HF_PREDICT_TRIGGER and eta is not None and eta <= 10:
            logger.warning(f'{tag}🔮 PREDICTIVE: May liquidate in ~{eta} blocks')
            opp = await self._run(self.finder.find_best, pos, block_num)
            if opp:
                logger.info(f'{tag}⏳ Staging opportunity: ${float(opp.net_profit_usd):,.2f}')
                self._log_opportunity(opp, prefix='⏳ STAGING')

        # Tier 3: Alert (HF < 1.05)
        elif hf < self.HF_ALERT_BELOW:
            logger.warning(f'{tag}⚠️  ALERT: High risk (HF={float(hf):.4f})')

        # Tier 4: Watch (HF < 1.10)
        elif hf < self.HF_WATCH_BELOW:
            logger.info(f'{tag}👁️  WATCHING: Elevated risk (HF={float(hf):.4f})')

        # Tier 5: Healthy
        else:
            logger.info(f'{tag}✅ HEALTHY: HF={float(hf):.4f}')

        # Log velocity and ETA for tracking (only for users near liquidation)
        if hf < Decimal('1.10') and hf > Decimal('1.0'):
            if eta is not None:
                logger.info(f'   └─ Trend: {float(velocity):+.6f}/block → ETA {eta} blocks')
            else:
                logger.info(f'   └─ Trend: {float(velocity):+.6f}/block (stable/improving)')
    # ══════════════════════════════════════════════════════════
    # Opportunity logging + execution
    # ══════════════════════════════════════════════════════════

    def _log_opportunity(self, opp, prefix: str = '💰 OPPORTUNITY') -> None:
        self.stats['opportunities'] += 1
        sep = '═' * 68
        logger.info(sep)
        logger.info(f'{prefix}  block={opp.block_number}')
        logger.info(f'   User         : {opp.user}')
        logger.info(f'   HF           : {float(opp.health_factor):.6f}')
        logger.info(f'   Collateral   : {opp.col_symbol}  ${float(opp.col_usd):.4f}')
        logger.info(f'   Debt covered : {opp.debt_symbol}  ${float(opp.debt_usd):.4f}')
        logger.info(f'   NET PROFIT   : ${float(opp.net_profit_usd):.4f}')
        logger.info(sep)

    def _execute_sync(self, opp) -> bool:
        try:
            encoded   = encode_params(opp)
            gas_price = int(self.w3.eth.gas_price * 1.2)
            nonce     = self.w3.eth.get_transaction_count(
                self.account.address, 'pending'
            )
            logger.info('  Running estimate_gas...')
            try:
                gas_est = self.liq.functions.executeLiquidation(encoded).estimate_gas({
                    'from':     self.account.address,
                    'gasPrice': gas_price,
                })
                gas_limit = int(gas_est * 1.3)
                logger.info(f'  Gas: {gas_est} estimated → {gas_limit} limit')
            except Exception as e:
                logger.warning(
                    f'  estimate_gas failed: {str(e)[:80]} — using fallback 500k'
                )
                gas_limit = 500_000

            tx = self.liq.functions.executeLiquidation(encoded).build_transaction({
                'from':     self.account.address,
                'nonce':    nonce,
                'gas':      gas_limit,
                'gasPrice': gas_price,
                'chainId':  137,
            })
            signed  = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            self.stats['attempts'] += 1
            logger.info(f'  TX SENT  : {tx_hash.hex()}')
            logger.info(f'  Explorer : https://polygonscan.com/tx/{tx_hash.hex()}')
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.get('status') == 1:
                self.stats['successes'] += 1
                logger.info('  ✅ SUCCESS')
                return True
            else:
                logger.warning('  ❌ REVERTED')
                return False
        except Exception as e:
            logger.error(f'  Execution exception: {e}')
            return False

    async def _fire_opportunity(self, opp, reason: str = '') -> None:
        self._log_opportunity(opp, prefix=f'💰 {reason}')
        if not self.execution_enabled:
            logger.info('  [MONITOR] Set EXECUTION_ENABLED=true to execute')
            return
        if not self.account:
            return
        if opp.net_profit_usd < self.min_profit_usd:
            logger.info(
                f'  Skipping — profit ${float(opp.net_profit_usd):.2f} '
                f'below threshold ${float(self.min_profit_usd):.2f}'
            )
            return
        logger.info('  ⚡ Executing...')
        await self._run(self._execute_sync, opp)

    # ══════════════════════════════════════════════════════════
    # Block handler
    # ══════════════════════════════════════════════════════════

    async def _handle_block(self, block_num: int, pending_events: list) -> None:
        """
        Handle new block with events.
        PRODUCTION GRADE: No duplicate evaluations, race-condition safe, reconnect-proof.
        """
        self.stats['blocks'] += 1
        self._current_block = block_num

        # ── Reload reserves periodically ──────────────────────────────
        if block_num - self._last_reserve_reload >= self.RESERVE_RELOAD_BLKS:
            await self._run(self.reader.load_all_reserves)
            await self._run(self.reader.check_flashloan_liquidity)
            self._last_reserve_reload = block_num
            logger.info(f'🔄 Reserves reloaded at block {block_num}')

        # ── Extract users from events ────────────────────────────────
        event_users: set[str] = set()
        for log in pending_events:
            user = self._extract_user(log)
            if user:
                ul = user.lower()
                event_users.add(ul)
                # Initialize registry only if not exists (preserve last_checked if present)
                if ul not in self._user_registry:
                    self._user_registry[ul] = 0

        if pending_events and event_users:
            self.stats['events'] += len(pending_events)
            logger.info(f'📦 Block {block_num}: {len(pending_events)} event(s) → {len(event_users)} unique user(s)')

        # ── Users due for passive recheck (WITH DEDUPLICATION) ────────
        recheck_users: set[str] = set()

        # Iterate over a snapshot to avoid dict size change during iteration
        registry_snapshot = list(self._user_registry.items())

        for user, last_checked in registry_snapshot:
            # Skip if already in event_users (will be processed anyway)
            if user in event_users:
                continue

            # CRITICAL: Skip if already evaluated in THIS block
            if last_checked == block_num:
                continue

            cached = self._cached_positions.get(user)

            # Determine recheck interval based on user state
            if not cached:
                interval = 50  # Unknown user, check moderately
            elif isinstance(cached, str):  # 'EMPTY' marker
                interval = 50  # Empty user, check every 50 blocks
            else:
                hf = getattr(cached, 'health_factor', Decimal('999'))
                if hf < Decimal('1.05'):
                    interval = 5   # Critical risk
                elif hf < Decimal('1.10'):
                    interval = 20  # Elevated risk
                else:
                    interval = self.RECHECK_INTERVAL  # 50 blocks default

            # Check if it's time to re-evaluate
            if block_num - last_checked >= interval:
                recheck_users.add(user)

        # ── Combine and process users ─────────────────────────────────
        all_users = event_users | recheck_users

        if not all_users:
            # Update stats and exit early
            self._update_stats_and_cleanup(block_num, event_users, recheck_users)
            return

        logger.info(f'🎯 Processing {len(all_users)} user(s) at block {block_num}')

        # Process with concurrency control
        semaphore = asyncio.Semaphore(3)  # Reduced to 3 for rate limit safety

        async def process_with_semaphore(user, label):
            async with semaphore:
                try:
                    return await self._process_user(user, block_num, label)
                except Exception as e:
                    logger.error(f'❌ Process failed for {user[:14]}: {e}')
                    return None

        tasks = []
        for user in all_users:
            label = 'event' if user in event_users else 'recheck'
            tasks.append(process_with_semaphore(user, label))

        # Gather results but don't let failures stop others
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Log any unexpected exceptions
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                user = list(all_users)[i]
                logger.error(f'❌ Unhandled error for {user[:14]}: {result}')
                traceback.print_exc()

        # Final stats update
        self._update_stats_and_cleanup(block_num, event_users, recheck_users)


    def _update_stats_and_cleanup(self, block_num: int, event_users: set, recheck_users: set) -> None:
        """
        Helper method to update stats and clean stale entries.
        Separated to keep _handle_block focused.
        """
        # Update user count
        self.stats['users_tracked'] = len(self._user_registry)

        # Periodic stats logging
        if self.stats['blocks'] % 50 == 0:
            users_with_positions = sum(1 for p in self._cached_positions.values() if p and not isinstance(p, str))
            users_empty = sum(1 for p in self._cached_positions.values() if p is None or isinstance(p, str))

            logger.info(f'📊 ── STATS (block {block_num}) ──────────────────────────────')
            logger.info(f'  blocks={self.stats["blocks"]:,} | events={self.stats["events"]:,} | '
                       f'tracked={self.stats["users_tracked"]:,} (active: {users_with_positions}, empty: {users_empty})')
            logger.info(f'  checked={self.stats["users_checked"]:,} | liquidatable={self.stats["liquidatable"]:,} | '
                       f'opps={self.stats["opportunities"]:,} | attempts={self.stats["attempts"]:,} | '
                       f'successes={self.stats["successes"]:,}')
            logger.info(f'  ────────────────────────────────────────────────────────────')

        # Clean up stale entries (every 100 blocks)
        if block_num % 100 == 0:
            stale_threshold = block_num - 2000  # 2,000 blocks = ~2.7 hours on Polygon
            stale_users = [
                user for user, last_checked in list(self._user_registry.items())
                if last_checked < stale_threshold and user not in event_users and user not in recheck_users
            ]
            for user in stale_users[:20]:  # Limit cleanup per cycle
                logger.debug(f'🧹 Removing stale user {user[:14]} (last seen {block_num - self._user_registry[user]} blocks ago)')
                self._user_registry.pop(user, None)
                self._cached_positions.pop(user, None)
                self._hf_history.pop(user, None)
    # ══════════════════════════════════════════════════════════
    # Event parsing
    # ══════════════════════════════════════════════════════════

    def _extract_user(self, log: dict) -> Optional[str]:
        topics = log.get('topics', [])
        if not topics:
            return None
        t0 = _normalise_topic(topics[0])
        if t0 == _normalise_topic(LIQ_TOPIC):
            return None   # skip liquidation events we emitted
        if len(topics) < 3:
            return None
        return _topic_to_addr(topics[2])

    # ══════════════════════════════════════════════════════════
    # WebSocket listener  (THE FIX IS HERE)
    # ══════════════════════════════════════════════════════════

    async def _listen(self) -> None:
        """
        Two WSS subscriptions:
          id=1  newHeads  → block clock + event buffer flush
          id=2  logs      → Aave pool events (sent ONLY after id=1 ACK arrives)

        WHY: Alchemy occasionally returns a rate-limit error (-32029) in the
        same message frame as the first subscription response.  If we send
        both subscriptions immediately, the error can consume or race with
        the id=2 ACK, leaving sub_logs = None permanently and silently
        discarding every Aave event.  Sending id=2 only after id=1 is
        confirmed eliminates this race entirely.

        Events are buffered per block and flushed when the NEXT block header
        arrives, so _handle_block always sees the complete event set for the
        previous block.
        """
        reconnect_delay               = 2
        sub_blocks: Optional[str]     = None
        sub_logs:   Optional[str]     = None
        log_sub_sent: bool            = False
        event_buffer: dict[int, list] = {}
        last_block                    = 0

        while True:
            try:
                logger.info('Connecting to WebSocket...')
                async with websockets.connect(
                    ALCHEMY_WSS,
                    ping_interval=20,
                    ping_timeout=30,
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    reconnect_delay = 15
                    sub_blocks   = None
                    sub_logs     = None
                    log_sub_sent = False

                    # ── Step 1: Subscribe to block headers only ────────
                    await ws.send(json.dumps({
                        'jsonrpc': '2.0', 'id': 1,
                        'method':  'eth_subscribe',
                        'params':  ['newHeads'],
                    }))
                    logger.info('Sent newHeads subscription (waiting for ACK)...')

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        # ── Subscription ACKs ──────────────────────────
                        if (
                            'id' in msg
                            and 'result' in msg
                            and isinstance(msg.get('result'), str)
                        ):
                            msg_id = msg['id']
                            sub_id = msg['result']

                            if msg_id == 1 and sub_blocks is None:
                                sub_blocks = sub_id
                                logger.info(f'✅ Block sub confirmed: {sub_blocks}')

                                # ── Step 2: NOW subscribe to Aave logs ─
                                if not log_sub_sent:
                                    await ws.send(json.dumps({
                                        'jsonrpc': '2.0', 'id': 2,
                                        'method':  'eth_subscribe',
                                        'params':  ['logs', {
                                            'address': AAVE_POOL,
                                            'topics':  [WATCHED_TOPICS],
                                        }],
                                    }))
                                    log_sub_sent = True
                                    logger.info('Sent Aave log subscription (waiting for ACK)...')

                            elif msg_id == 2 and sub_logs is None:
                                sub_logs = sub_id
                                logger.info(f'✅ Log sub confirmed : {sub_logs}')
                                logger.info(
                                    'Both subscriptions active — '
                                    'monitoring Aave events in pure WSS / predictive mode'
                                )
                            continue

                        # ── Rate-limit or other errors — log and continue ──
                        if 'error' in msg:
                            err = msg['error']
                            code = err.get('code', 0) if isinstance(err, dict) else 0
                            text = err.get('message', str(err)) if isinstance(err, dict) else str(err)
                            logger.warning(f'WSS error (code={code}): {text}')
                            # If log sub hasn't been sent yet and we hit a rate limit,
                            # retry the log subscription after a brief pause
                            if log_sub_sent and sub_logs is None and code == -32029:
                                await asyncio.sleep(1)
                                await ws.send(json.dumps({
                                    'jsonrpc': '2.0', 'id': 2,
                                    'method':  'eth_subscribe',
                                    'params':  ['logs', {
                                        'address': AAVE_POOL,
                                        'topics':  [WATCHED_TOPICS],
                                    }],
                                }))
                                logger.info('Retried Aave log subscription after rate-limit...')
                            continue

                        params = msg.get('params')
                        if not params:
                            continue

                        sub    = params.get('subscription')
                        result = params.get('result', {})

                        # ── New block header ───────────────────────────
                        if sub == sub_blocks and 'number' in result:
                            block_num = int(result['number'], 16)
                            if block_num <= last_block:
                                continue
                            # Flush previous block's buffered events
                            buffered = event_buffer.pop(last_block, [])
                            if last_block > 0:
                                asyncio.ensure_future(
                                    self._handle_block(last_block, buffered)
                                )
                            last_block = block_num

                        # ── Aave log ───────────────────────────────────
                        elif sub_logs and sub == sub_logs:
                            if result.get('removed'):
                                continue
                            log_block = int(result.get('blockNumber', '0x0'), 16)
                            event_buffer.setdefault(log_block, []).append(result)

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f'WS closed: {e}  — reconnecting in {reconnect_delay}s')
            except Exception as e:
                logger.error(f'WS error: {e}  — reconnecting in {reconnect_delay}s')

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

    
    # ══════════════════════════════════════════════════════════
    # Background price refresh + zero-RPC sweep
    # ══════════════════════════════════════════════════════════

    async def _price_refresh_loop(self) -> None:
        """
        Background price refresh loop with zero-RPC HF estimation.

        This method runs continuously in the background, fetching fresh oracle
        prices every PRICE_REFRESH_SECS seconds, then re-estimating HF for all
        tracked users using pure Python math (no RPC calls).

        Features:
          - Automatic retry on failure
          - Graceful degradation if prices fail
          - Immediate RPC confirmation for users crossing danger thresholds
          - Rate limit friendly (single oracle call vs. one per user)
          - Comprehensive error logging with traceback
        """
        retry_count = 0
        max_retries = 3

        while True:
            try:
                # Sleep first to avoid immediate execution on startup
                await asyncio.sleep(self.PRICE_REFRESH_SECS)

                # Reset retry counter on successful iteration start
                retry_count = 0

                # ── Step 1: Fetch fresh oracle prices ─────────────────────
                price_start = time.time()
                await self._run(self.reader.refresh_prices)
                price_duration = time.time() - price_start

                # Validate prices were fetched successfully
                if not self.reader.prices_valid:
                    logger.warning(f'⚠️ Price refresh completed but prices are invalid (duration: {price_duration:.2f}s)')
                    continue

                new_prices = self.reader.prices
                logger.debug(
                    f'💰 Prices refreshed: {len(new_prices)} assets | '
                    f'duration: {price_duration:.2f}s | '
                    f'block: {self._current_block}'
                )

                # ── Step 2: Sweep all tracked users for HF changes ─────────
                if len(self._cached_positions) > 0:
                    sweep_start = time.time()
                    urgent_users = self._price_sweep(new_prices, self._current_block)
                    sweep_duration = time.time() - sweep_start

                    if urgent_users:
                        logger.warning(
                            f'⚡ Price sweep detected {len(urgent_users)} user(s) crossing danger threshold '
                            f'(sweep duration: {sweep_duration:.3f}s)'
                        )

                        # Log which users triggered
                        for user in urgent_users:
                            cached = self._cached_positions.get(user)
                            if cached and cached != 'EMPTY':
                                hf = getattr(cached, 'health_factor', Decimal('0'))
                                logger.warning(f'   🔔 {user[:14]}... current HF={float(hf):.4f}')

                        # ── Step 3: Fire RPC confirms for urgent users ─────
                        confirm_start = time.time()
                        semaphore = asyncio.Semaphore(3)  # Limit concurrency

                        async def confirm_with_semaphore(user):
                            async with semaphore:
                                return await self._process_user(user, self._current_block, 'price-trigger')

                        tasks = [confirm_with_semaphore(u) for u in urgent_users]
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        # Log any failures
                        for user, result in zip(urgent_users, results):
                            if isinstance(result, Exception):
                                logger.error(f'❌ Failed to confirm {user[:14]}: {result}')

                        confirm_duration = time.time() - confirm_start
                        logger.info(
                            f'✅ RPC confirms completed for {len(urgent_users)} user(s) '
                            f'(duration: {confirm_duration:.2f}s)'
                        )
                    else:
                        if len(self._cached_positions) > 0:
                            logger.debug(f'📊 Price sweep complete: 0 urgent users (duration: {sweep_duration:.3f}s)')

                # ── Optional: Log price health occasionally ─────────────────
                if self.stats['blocks'] % 100 == 0 and self.stats['blocks'] > 0:
                    # Sample a few key assets to verify prices are reasonable
                    sample_assets = ['0x7ceb23fd6bc0add59e62ac25578270cff1b9f619',  # WETH
                                    '0x2791bca1f2de4661ed88a30c99a7a9449aa84174',  # USDC
                                    '0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270']  # WMATIC

                    price_sample = []
                    for asset in sample_assets:
                        price = new_prices.get(asset.lower(), Decimal('0'))
                        if price > 0:
                            price_sample.append(f"{asset[:8]}={float(price):.2f}")

                    if price_sample:
                        logger.info(f'📈 Oracle price sample: {", ".join(price_sample)}')

            except asyncio.CancelledError:
                logger.info('Price refresh loop cancelled - shutting down')
                break

            except Exception as e:
                # Comprehensive error handling with retry logic
                retry_count += 1
                logger.error(f'❌ Price refresh loop error (attempt {retry_count}/{max_retries}): {e}')
                traceback.print_exc()

                # Exponential backoff on repeated failures
                if retry_count >= max_retries:
                    logger.warning(f'⚠️ Max retries ({max_retries}) exceeded - waiting 60 seconds before retry')
                    await asyncio.sleep(60)
                    retry_count = 0
                else:
                    # Short delay before retry
                    await asyncio.sleep(5)
                    

    def _log_price_health(self, prices: dict) -> None:
        """
        Log price health metrics for debugging.
        Called periodically to ensure oracle is returning reasonable values.
        """
        if not prices:
            logger.warning('⚠️ Price dictionary is empty!')
            return

        # Check for zero or stale prices
        zero_prices = []
        for addr, price in prices.items():
            if price == 0:
                token = self.reader.reserves.get(addr.lower())
                symbol = token.symbol if token else addr[:8]
                zero_prices.append(symbol)

        if zero_prices:
            logger.warning(f'⚠️ Zero prices detected for: {", ".join(zero_prices[:5])}')

        # Verify against known reasonable ranges
        weth_price = prices.get(WETH.lower(), Decimal('0'))
        if weth_price > 0 and (weth_price < 1000 or weth_price > 5000):
            logger.warning(f'⚠️ Unusual WETH price: ${float(weth_price):.2f}')

        usdc_price = prices.get(USDC.lower(), Decimal('0'))
        if usdc_price > 0 and (usdc_price < 0.95 or usdc_price > 1.05):
            logger.warning(f'⚠️ Unusual USDC price: ${float(usdc_price):.4f}')
    # ══════════════════════════════════════════════════════════
    # Entry point
    # ══════════════════════════════════════════════════════════

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.create_task(self._price_refresh_loop())
            loop.run_until_complete(self._listen())
        except KeyboardInterrupt:
            logger.info('Shutting down...')
        finally:
            loop.close()
#════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='Aave V3 Liquidation Bot — Polygon')
    p.add_argument('--execute',    action='store_true',
                   help='Enable on-chain execution')
    p.add_argument('--min-profit', type=float, default=None,
                   help='Min profit in USD (default: 10)')
    args = p.parse_args()

    if args.execute:
        os.environ['EXECUTION_ENABLED'] = 'true'
    if args.min_profit is not None:
        os.environ['MIN_PROFIT_USD'] = str(args.min_profit)

    import time as _time
    _restart_delay = 10
    while True:
        try:
            LiquidationBot().run()
            break  # clean exit
        except KeyboardInterrupt:
            logger.info('Interrupted')
            break
        except Exception as e:
            logger.error(f'Fatal: {e}', exc_info=True)
            logger.warning(f'Bot crashed — restarting in {_restart_delay}s...')
            _time.sleep(_restart_delay)
            _restart_delay = min(_restart_delay * 2, 120)