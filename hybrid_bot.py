#!/usr/bin/env python3
"""
Multi-Protocol Hybrid Liquidation Bot — Polygon
================================================
Protocols : Aave V3 | Morpho Blue | Compound V3
Flash loans: Balancer V2 (zero fee) for ALL protocols (Aave V3, Morpho Blue,
             Compound V3) — all execution goes through MultiProtocolHybridBot
             contract.executeLiquidation() with the appropriate protocol byte.
Contract  : loaded from bot/contract/deployed_address.json

Compound V3 flash loan flow (inside contract, PROTOCOL_COMPOUND_V3 = 2):
  1. Flash loan USDC from Balancer V2
  2. absorb(contract_address, [user])        — seizes collateral into Comet reserves
  3. buyCollateral(col_asset, minCol, usdc)  — buys at storeFront discount
  4. Swap collateral → USDC via DEX
  5. Repay Balancer, sweep profit to owner

Root-cause fixes vs previous version
--------------------------------------
1. WSS rate-limited by OnFinality  → always-on HTTP poll loop as primary
   event source; WSS runs concurrently as a bonus (not a dependency).
2. Cold start only scanned 1 day   → now scans 7 days (≈50 k blocks) in
   5 000-block chunks that fit within OnFinality's getLogs limit.
3. Compound V3 not executed        → _build_c3_opportunity() + _fire_opportunity()
   routes through contract flash loan exactly like Aave V3.
4. Radiant removed (deprecated on Polygon).
5. ArbScanner removed (APR-spread logic fundamentally broken).
"""

import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
import concurrent.futures

import websockets
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_abi import encode as abi_encode, decode as abi_decode
from dotenv import load_dotenv

# Protocol modules
from protocols.morpho      import MorphoMonitor, MORPHO_BORROW_TOPIC as _MORPHO_BORROW_BYTES
from protocols.compound_v3 import (
    CompoundV3Monitor,
    SUPPLY_TOPIC            as _C3_SUPPLY_HEX,
    SUPPLY_COLLATERAL_TOPIC as _C3_COL_HEX,
    WITHDRAW_TOPIC          as _C3_WITHDRAW_HEX,
)


def _to_hex(t) -> str:
    if isinstance(t, (bytes, bytearray)):
        return '0x' + t.hex()
    s = str(t).lower()
    return s if s.startswith('0x') else '0x' + s

MORPHO_BORROW_TOPIC    = _to_hex(_MORPHO_BORROW_BYTES)
C3_SUPPLY_TOPIC        = _to_hex(_C3_SUPPLY_HEX)
C3_COL_TOPIC           = _to_hex(_C3_COL_HEX)
C3_WITHDRAW_TOPIC      = _to_hex(_C3_WITHDRAW_HEX)

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════
# LOGGING — rotating files + stdout
# ═══════════════════════════════════════════════════════════════════════
_fmt    = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
_stream = logging.StreamHandler(sys.stdout)
_stream.setFormatter(_fmt)
_file   = RotatingFileHandler('hybrid_bot.log', maxBytes=10 * 1024 * 1024, backupCount=5)
_file.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_stream, _file])
logger = logging.getLogger('HybridBot')
for _lib in ('websockets', 'web3', 'urllib3', 'aiohttp'):
    logging.getLogger(_lib).setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════
# RPC CONFIG
# ═══════════════════════════════════════════════════════════════════════
def _build_rpcs() -> tuple[str, str]:
    """
    Dual-provider RPC strategy used by all production MEV bots:
    ─ Alchemy   → primary for eth_call / Multicall3 / simulation / block queries
                  (300 CUs/s, but only 10 blocks per eth_getLogs on free tier)
    ─ OnFinality → primary for eth_getLogs cold-start scanning
                  (allows 10 000 blocks per getLogs request)
    Both are configured below; _build_logs_rpc() returns the getLogs-optimized URL.
    """
    alchemy = (os.getenv('ALCHEMY_API_KEY') or '').strip()
    if alchemy:
        return (
            f'https://polygon-mainnet.g.alchemy.com/v2/{alchemy}',
            f'wss://polygon-mainnet.g.alchemy.com/v2/{alchemy}',
        )
    raw = (os.getenv('FINALITY_API_KEY') or '').strip()
    if raw:
        if raw.startswith('http'):
            import urllib.parse as _up
            _qs  = _up.urlparse(raw).query
            _key = dict(p.split('=', 1) for p in _qs.split('&') if '=' in p).get('apikey', '')
            return raw, f'wss://polygon.api.onfinality.io/ws?apikey={_key}'
        return (
            f'https://polygon.api.onfinality.io/rpc?apikey={raw}',
            f'wss://polygon.api.onfinality.io/ws?apikey={raw}',
        )
    sys.exit('ERROR: No RPC key. Set ALCHEMY_API_KEY or FINALITY_API_KEY.')


def _build_logs_rpc() -> str:
    """
    eth_getLogs-optimised RPC endpoint.
    OnFinality supports 10 000 blocks per getLogs → cold start in ~25 calls.
    Alchemy free tier allows only 10 blocks per getLogs → 5 000 calls for cold start.
    Falls back to Alchemy if OnFinality key is absent.
    """
    raw = (os.getenv('FINALITY_API_KEY') or '').strip()
    if raw:
        if raw.startswith('http'):
            return raw
        return f'https://polygon.api.onfinality.io/rpc?apikey={raw}'
    alchemy = (os.getenv('ALCHEMY_API_KEY') or '').strip()
    if alchemy:
        return f'https://polygon-mainnet.g.alchemy.com/v2/{alchemy}'
    sys.exit('ERROR: No RPC key.')

RPC_HTTP, RPC_WSS = _build_rpcs()
LOGS_HTTP         = _build_logs_rpc()   # OnFinality for getLogs (10k blocks/call)

# ═══════════════════════════════════════════════════════════════════════
# MULTICALL3 — batch health checks (300+ users per RPC call)
# ═══════════════════════════════════════════════════════════════════════
MULTICALL3 = '0xcA11bde05977b3631167028862bE2a173976CA11'
MULTICALL3_ABI = [{
    'inputs': [{'components': [
        {'name': 'target',       'type': 'address'},
        {'name': 'allowFailure', 'type': 'bool'},
        {'name': 'callData',     'type': 'bytes'},
    ], 'name': 'calls', 'type': 'tuple[]'}],
    'name': 'aggregate3',
    'outputs': [{'components': [
        {'name': 'success',    'type': 'bool'},
        {'name': 'returnData', 'type': 'bytes'},
    ], 'name': 'returnData', 'type': 'tuple[]'}],
    'stateMutability': 'payable',
    'type': 'function',
}]
# Pre-compute 4-byte selectors once
_AAVE_ACCOUNT_SEL  = Web3.keccak(text='getUserAccountData(address)')[:4]
_COMET_IS_LIQ_SEL  = Web3.keccak(text='isLiquidatable(address)')[:4]
_COMET_BORROW_SEL  = Web3.keccak(text='borrowBalanceOf(address)')[:4]

# ═══════════════════════════════════════════════════════════════════════
# CONTRACT CONFIG
# ═══════════════════════════════════════════════════════════════════════
HERE      = Path(__file__).parent
ADDR_FILE = HERE / 'contract' / 'deployed_address.json'
ABI_FILE  = HERE / 'contract' / 'MultiProtocolHybridBot.abi.json'

def _load_contract_config() -> tuple[str, list]:
    if not ADDR_FILE.exists():
        logger.warning('No deployed_address.json — run deploy_contract.py first')
        return '', []
    data    = json.loads(ADDR_FILE.read_text())
    address = data['address']
    if not ABI_FILE.exists():
        logger.warning('No ABI file found')
        return address, []
    return address, json.loads(ABI_FILE.read_text())

CONTRACT_ADDRESS, CONTRACT_ABI = _load_contract_config()

# ═══════════════════════════════════════════════════════════════════════
# POLYGON ADDRESSES
# ═══════════════════════════════════════════════════════════════════════
AAVE_POOL          = '0x794a61358D6845594F94dc1DB02A252b5b4814aD'
AAVE_ORACLE        = '0xb023e699F5a33916Ea823A16485e259257cA8Bd1'
POOL_DATA_PROVIDER = '0x69FA688f1Dc47d4B5d8029D5a35FB7a548310654'
COMET_USDC         = '0xF25212E676D1F7F89Cd72fFEe66158f541246445'
BALANCER_VAULT     = '0xBA12222222228d8Ba445958a75a0704d566BF2C8'
QUICKSWAP_V2       = '0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff'
SUSHISWAP          = '0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506'
UNISWAP_V3         = '0xE592427A0AEce92De3Edee1F18E0157C05861564'

WETH = Web3.to_checksum_address('0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619')
WPOL = Web3.to_checksum_address('0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270')
USDC = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
USDT = Web3.to_checksum_address('0xc2132D05D31c914a87C6611C10748AEb04B58e8F')
DAI  = Web3.to_checksum_address('0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063')
WBTC = Web3.to_checksum_address('0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6')

# Protocol enum values matching contract
PROTOCOL_AAVE_V3     = 0
PROTOCOL_MORPHO_BLUE = 1
PROTOCOL_COMPOUND_V3 = 2

# SwapRouterType enum matching contract
RT_QUICKSWAP = 0
RT_SUSHISWAP = 1
RT_V3        = 2
RT_ONE_INCH  = 3

# ABI type for the multi-protocol LiquidationParams struct
NEW_PARAMS_TYPE = (
    '(uint8,address,address,address,uint256,uint256,'
    'address[],address,uint8,uint24[],bytes,uint256,uint256,bytes)'
)

WAD              = Decimal('1e18')
MIN_DEBT_USD     = Decimal('300')    # skip tiny positions
MAX_GAS_PRICE_WEI = 500 * 10**9     # 500 gwei ceiling

# Polygon block time ≈ 2.2 s
# 400 000 blocks ≈ 880 000 s ≈ 10 days  (first-time: cast a wide net)
# 100 000 blocks ≈ 220 000 s ≈ 2.5 days (stale-cache recovery)
#  50 000 blocks ≈ 110 000 s ≈ 7 days   (legacy / debug)
COLD_START_BLOCKS_FULL  = 400_000   # first-time scan (no cache file)
COLD_START_BLOCKS_STALE = 100_000   # stale cache (> CACHE_MAX_AGE_SECS old)
COLD_START_CHUNK        = 5_000     # OnFinality: max 10 000; safe at 5 000
CACHE_MAX_AGE_SECS      = 7_200     # 2 h — cache fresher than this → warm restart
NEW_USER_SCAN_BLOCKS    = 2_000     # incremental scan interval (~72 min)

# At-risk user re-check cooldowns (blocks between full read_position calls)
# Trades off RPC cost vs responsiveness. Event-triggered checks always bypass cooldown.
AT_RISK_COOLDOWN = {          # HF threshold → blocks between re-checks
    1.02: 2,                  # <1.02 → every 2 blocks (~4 s) — imminent
    1.05: 5,                  # <1.05 → every 5 blocks (~11 s)
    1.10: 15,                 # <1.10 → every 15 blocks (~33 s)
}

# ═══════════════════════════════════════════════════════════════════════
# AAVE V3 EVENTS (watched for user discovery)
# ═══════════════════════════════════════════════════════════════════════
def _topic(sig: str) -> str:
    h = Web3.keccak(text=sig).hex()
    return '0x' + h if not h.startswith('0x') else h

AAVE_BORROW_TOPIC = _topic('Borrow(address,address,address,uint256,uint8,uint256,uint16)')
AAVE_SUPPLY_TOPIC = _topic('Supply(address,address,address,uint256,uint16)')
AAVE_REPAY_TOPIC  = _topic('Repay(address,address,address,uint256,bool)')
AAVE_LIQ_TOPIC    = _topic('LiquidationCall(address,address,address,uint256,uint256,address,bool)')

# All topics watched for user discovery (Aave + Morpho + Compound V3)
ALL_WATCHED_TOPICS = [
    AAVE_BORROW_TOPIC, AAVE_SUPPLY_TOPIC, AAVE_REPAY_TOPIC,
    MORPHO_BORROW_TOPIC,
    C3_SUPPLY_TOPIC, C3_COL_TOPIC, C3_WITHDRAW_TOPIC,
]

# ═══════════════════════════════════════════════════════════════════════
# MINIMAL ABIS
# ═══════════════════════════════════════════════════════════════════════
POOL_ABI = [
    {'name': 'getUserAccountData', 'inputs': [{'name': 'user', 'type': 'address'}],
     'outputs': [
         {'name': 'totalCollateralBase', 'type': 'uint256'},
         {'name': 'totalDebtBase',       'type': 'uint256'},
         {'name': 'availableBorrowsBase','type': 'uint256'},
         {'name': 'currentLiquidationThreshold', 'type': 'uint256'},
         {'name': 'ltv',                 'type': 'uint256'},
         {'name': 'healthFactor',        'type': 'uint256'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'getReservesList', 'inputs': [],
     'outputs': [{'name': '', 'type': 'address[]'}],
     'stateMutability': 'view', 'type': 'function'},
]

POOL_DATA_PROVIDER_ABI = [
    {'name': 'getReserveConfigurationData', 'inputs': [{'name': 'asset', 'type': 'address'}],
     'outputs': [
         {'name': 'decimals',                  'type': 'uint256'},
         {'name': 'ltv',                       'type': 'uint256'},
         {'name': 'liquidationThreshold',      'type': 'uint256'},
         {'name': 'liquidationBonus',          'type': 'uint256'},
         {'name': 'reserveFactor',             'type': 'uint256'},
         {'name': 'usageAsCollateralEnabled',  'type': 'bool'},
         {'name': 'borrowingEnabled',          'type': 'bool'},
         {'name': 'stableBorrowRateEnabled',   'type': 'bool'},
         {'name': 'isActive',                  'type': 'bool'},
         {'name': 'isFrozen',                  'type': 'bool'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'getReserveTokensAddresses', 'inputs': [{'name': 'asset', 'type': 'address'}],
     'outputs': [
         {'name': 'aTokenAddress',           'type': 'address'},
         {'name': 'stableDebtTokenAddress',  'type': 'address'},
         {'name': 'variableDebtTokenAddress','type': 'address'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'getUserReserveData', 'inputs': [
         {'name': 'asset', 'type': 'address'}, {'name': 'user', 'type': 'address'}],
     'outputs': [
         {'name': 'currentATokenBalance',    'type': 'uint256'},
         {'name': 'currentStableDebt',       'type': 'uint256'},
         {'name': 'currentVariableDebt',     'type': 'uint256'},
         {'name': 'principalStableDebt',     'type': 'uint256'},
         {'name': 'scaledVariableDebt',      'type': 'uint256'},
         {'name': 'stableBorrowRate',        'type': 'uint256'},
         {'name': 'liquidityRate',           'type': 'uint256'},
         {'name': 'stableRateLastUpdated',   'type': 'uint40'},
         {'name': 'usageAsCollateralEnabled','type': 'bool'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'getReserveData', 'inputs': [{'name': 'asset', 'type': 'address'}],
     'outputs': [
         {'name': 'unbacked',                'type': 'uint256'},
         {'name': 'accruedToTreasuryScaled', 'type': 'uint256'},
         {'name': 'totalAToken',             'type': 'uint256'},
         {'name': 'totalStableDebt',         'type': 'uint256'},
         {'name': 'totalVariableDebt',       'type': 'uint256'},
         {'name': 'liquidityRate',           'type': 'uint256'},
         {'name': 'variableBorrowRate',      'type': 'uint256'},
         {'name': 'stableBorrowRate',        'type': 'uint256'},
         {'name': 'averageStableBorrowRate', 'type': 'uint256'},
         {'name': 'liquidityIndex',          'type': 'uint256'},
         {'name': 'variableBorrowIndex',     'type': 'uint256'},
         {'name': 'lastUpdateTimestamp',     'type': 'uint40'}],
     'stateMutability': 'view', 'type': 'function'},
]

ORACLE_ABI = [
    {'name': 'getAssetsPrices', 'inputs': [{'name': 'assets', 'type': 'address[]'}],
     'outputs': [{'name': '', 'type': 'uint256[]'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'getAssetPrice', 'inputs': [{'name': 'asset', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}],
     'stateMutability': 'view', 'type': 'function'},
]

ERC20_META_ABI = [
    {'name': 'symbol',   'inputs': [], 'outputs': [{'name': '', 'type': 'string'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'decimals', 'inputs': [], 'outputs': [{'name': '', 'type': 'uint8'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'balanceOf', 'inputs': [{'name': 'account', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}],
     'stateMutability': 'view', 'type': 'function'},
]

QS_ROUTER_ABI = [
    {'name': 'getAmountsOut',
     'inputs': [{'name': 'amountIn', 'type': 'uint256'}, {'name': 'path', 'type': 'address[]'}],
     'outputs': [{'name': 'amounts', 'type': 'uint256[]'}],
     'stateMutability': 'view', 'type': 'function'},
]

# ═══════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════
@dataclass
class ReserveMeta:
    symbol:        str
    decimals:      int
    liq_bonus:     Decimal   # e.g. 1.05 = 5% bonus (raw / 10000)
    liq_threshold: Decimal
    a_token:       str
    is_active:     bool
    is_frozen:     bool

@dataclass
class UserReservePosition:
    asset:       str
    symbol:      str
    decimals:    int
    a_bal:       int
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
    collateral: list = field(default_factory=list)
    debt:       list = field(default_factory=list)
    protocol:   str  = 'AAVE_V3'

@dataclass
class Opportunity:
    user:              str
    health_factor:     Decimal
    close_factor:      Decimal
    col_asset:         str
    col_symbol:        str
    col_amount_raw:    int
    col_usd:           Decimal
    debt_asset:        str
    debt_symbol:       str
    debt_to_cover_raw: int
    debt_usd:          Decimal
    liq_bonus:         Decimal
    gross_profit_usd:  Decimal
    gas_cost_usd:      Decimal
    net_profit_usd:    Decimal
    block_number:      int
    swap_router:       str
    swap_router_type:  int
    swap_path:         list
    v3_fees:           list
    min_col_raw:       int
    min_profit_raw:    int
    protocol:          int = PROTOCOL_AAVE_V3
    extra_data:        bytes = b''

@dataclass
class TrackedUser:
    address:          str
    protocols:        set   = field(default_factory=set)
    last_hf:          dict  = field(default_factory=dict)
    last_block:       int   = 0
    last_seen:        float = field(default_factory=time.time)
    is_at_risk:       bool  = False
    last_check_block: int   = 0   # last block we ran a full read_position for throttling

# ═══════════════════════════════════════════════════════════════════════
# NONCE MANAGER
# ═══════════════════════════════════════════════════════════════════════
class NonceManager:
    def __init__(self, w3: Web3, address: str):
        self._nonce = w3.eth.get_transaction_count(address, 'pending')
        self._lock  = asyncio.Lock()
        self._w3    = w3
        self._addr  = address

    async def next(self) -> int:
        async with self._lock:
            n = self._nonce
            self._nonce += 1
            return n

    async def sync(self):
        async with self._lock:
            on_chain    = self._w3.eth.get_transaction_count(self._addr, 'pending')
            self._nonce = max(self._nonce, on_chain)

# ═══════════════════════════════════════════════════════════════════════
# USER REGISTRY
# ═══════════════════════════════════════════════════════════════════════
class UserRegistry:
    CACHE_FILE = 'hybrid_tracked_users.json'

    def __init__(self):
        self.users: dict[str, TrackedUser] = {}
        self._updated_at: float = 0.0
        self._load()

    def _load(self):
        if Path(self.CACHE_FILE).exists():
            try:
                raw = json.loads(Path(self.CACHE_FILE).read_text())
                self._updated_at = float(raw.get('updated', 0))
                for addr, d in raw.get('users', {}).items():
                    self.users[addr] = TrackedUser(
                        address=addr,
                        protocols=set(d.get('protocols', [])),
                        last_hf=d.get('last_hf', {}),
                        last_block=d.get('last_block', 0),
                        last_seen=d.get('last_seen', time.time()),
                        is_at_risk=d.get('is_at_risk', False),
                        last_check_block=d.get('last_check_block', 0),
                    )
                age_mins = (time.time() - self._updated_at) / 60
                logger.info(
                    f'Loaded {len(self.users)} tracked users from disk '
                    f'(cache age: {age_mins:.0f} min)'
                )
            except Exception as e:
                logger.warning(f'Registry load failed: {e}')

    def save(self):
        try:
            now = time.time()
            raw = {
                'users': {
                    a: {
                        'protocols':        list(u.protocols),
                        'last_hf':          u.last_hf,
                        'last_block':       u.last_block,
                        'last_seen':        u.last_seen,
                        'is_at_risk':       u.is_at_risk,
                        'last_check_block': u.last_check_block,
                    }
                    for a, u in self.users.items()
                },
                'updated': now,
            }
            Path(self.CACHE_FILE).write_text(json.dumps(raw, indent=2))
            self._updated_at = now
        except Exception as e:
            logger.warning(f'Registry save failed: {e}')

    def cache_age_secs(self) -> float:
        """Seconds since the cache file was last saved (0 = never saved)."""
        return time.time() - self._updated_at if self._updated_at > 0 else float('inf')

    def get_max_saved_block(self) -> int:
        """Highest last_block across all tracked users — used to resume incremental scans."""
        if not self.users:
            return 0
        return max((u.last_block for u in self.users.values()), default=0)

    def get_min_hf(self, address: str) -> float:
        """Return the lowest known HF across all protocols for a user (999 = unknown)."""
        u = self.users.get(address.lower())
        if not u or not u.last_hf:
            return 999.0
        return min(float(v) for v in u.last_hf.values())

    def add_user(self, address: str, protocol: str, block: int):
        addr = address.lower()
        if addr not in self.users:
            self.users[addr] = TrackedUser(address=addr)
        u = self.users[addr]
        u.protocols.add(protocol)
        u.last_block = max(u.last_block, block)
        u.last_seen  = time.time()

    def update_hf(self, address: str, protocol: str, hf: float, block: int):
        addr = address.lower()
        if addr not in self.users:
            self.add_user(address, protocol, block)
        u             = self.users[addr]
        u.last_hf[protocol] = hf
        u.last_block  = max(u.last_block, block)
        u.is_at_risk  = any(v < 1.10 for v in u.last_hf.values())

    def get_at_risk(self) -> list:
        return [u for u in self.users.values() if u.is_at_risk]

# ═══════════════════════════════════════════════════════════════════════
# POSITION READER  (Aave V3 only)
# ═══════════════════════════════════════════════════════════════════════
GAS_UNITS_BASE = 600_000   # flash loan txs: 600-900k gas on Polygon mainnet
GAS_UNITS_HOP  = 80_000

class PositionReader:
    def __init__(self, w3: Web3):
        self.w3     = w3
        self.pool   = w3.eth.contract(address=Web3.to_checksum_address(AAVE_POOL),          abi=POOL_ABI)
        self.dp     = w3.eth.contract(address=Web3.to_checksum_address(POOL_DATA_PROVIDER), abi=POOL_DATA_PROVIDER_ABI)
        self.oracle = w3.eth.contract(address=Web3.to_checksum_address(AAVE_ORACLE),        abi=ORACLE_ABI)
        self.qs     = w3.eth.contract(address=Web3.to_checksum_address(QUICKSWAP_V2),       abi=QS_ROUTER_ABI)

        self.reserves:           dict[str, ReserveMeta] = {}
        self.prices:             dict[str, Decimal]     = {}
        self.liq_indexes:        dict[str, Decimal]     = {}
        self.prices_valid        = False
        self._price_ts: float    = 0.0
        self.no_flashloan_tokens: set[str] = set()

    def load_all_reserves(self):
        logger.info('Loading Aave V3 reserves...')
        try:
            addrs = self.pool.functions.getReservesList().call()
        except Exception as e:
            logger.error(f'getReservesList: {e}')
            return
        new_res = {}
        for addr in addrs:
            al = addr.lower()
            try:
                cs  = Web3.to_checksum_address(addr)
                cfg = self.dp.functions.getReserveConfigurationData(cs).call()
                decimals, _, liq_thresh, liq_bonus_bp, _, _, _, _, is_active, is_frozen = cfg
                if not is_active or liq_bonus_bp == 0:
                    continue
                try:
                    tok     = self.dp.functions.getReserveTokensAddresses(cs).call()
                    a_token = tok[0]
                except Exception:
                    a_token = '0x' + '0' * 40
                try:
                    erc20  = self.w3.eth.contract(address=cs, abi=ERC20_META_ABI)
                    symbol = erc20.functions.symbol().call() or al[:8]
                except Exception:
                    symbol = al[:8]
                new_res[al] = ReserveMeta(
                    symbol=symbol, decimals=int(decimals),
                    liq_bonus=Decimal(liq_bonus_bp) / Decimal('10000'),
                    liq_threshold=Decimal(liq_thresh) / Decimal('10000'),
                    a_token=a_token, is_active=is_active, is_frozen=is_frozen,
                )
            except Exception as e:
                logger.debug(f'Reserve load {al[:12]}: {e}')
        if new_res:
            self.reserves = new_res
            logger.info(f'Loaded {len(new_res)} reserves: {", ".join(sorted(v.symbol for v in new_res.values()))}')

    def refresh_prices(self):
        if not self.reserves:
            return
        addrs = [Web3.to_checksum_address(a) for a in self.reserves]
        try:
            raw         = self.oracle.functions.getAssetsPrices(addrs).call()
            new_p       = {a.lower(): Decimal(p) / Decimal('1e8') for a, p in zip(addrs, raw) if p > 0}
            self.prices       = new_p
            self.prices_valid = bool(new_p)
            self._price_ts    = time.time()
            logger.debug(f'Prices refreshed ({len(new_p)} assets)')
        except Exception as e:
            logger.error(f'Oracle refresh: {e}')
            self.prices       = {}
            self.prices_valid = False

    def refresh_liquidity_indexes(self):
        for al in self.reserves:
            try:
                rd = self.dp.functions.getReserveData(
                    Web3.to_checksum_address(al)
                ).call()
                self.liq_indexes[al] = Decimal(rd[9])   # index 9 = liquidityIndex (RAY)
            except Exception:
                pass

    def prices_are_fresh(self, max_age_secs: float = 120.0) -> bool:
        return self.prices_valid and (time.time() - self._price_ts) < max_age_secs

    def get_price(self, addr: str) -> Optional[Decimal]:
        return self.prices.get(addr.lower()) if self.prices_valid else None

    def matic_usd(self) -> Decimal:
        return self.prices.get(WPOL.lower(), Decimal('0.5'))

    def check_flashloan_liquidity(self):
        blocked, available = [], []
        vault_cs = Web3.to_checksum_address(BALANCER_VAULT)
        for al, meta in self.reserves.items():
            try:
                tok = self.w3.eth.contract(address=Web3.to_checksum_address(al), abi=ERC20_META_ABI)
                bal = tok.functions.balanceOf(vault_cs).call()
                if bal == 0:
                    self.no_flashloan_tokens.add(al)
                    blocked.append(meta.symbol)
                else:
                    self.no_flashloan_tokens.discard(al)
                    available.append(meta.symbol)
            except Exception:
                self.no_flashloan_tokens.add(al)
        logger.info(f'Balancer flashloan: {len(available)} tokens available | blocked: {", ".join(blocked) or "none"}')

    def check_flashloan_liquidity_for(self, debt_asset: str) -> bool:
        al = debt_asset.lower()
        try:
            vault_cs = Web3.to_checksum_address(BALANCER_VAULT)
            tok = self.w3.eth.contract(address=Web3.to_checksum_address(al), abi=ERC20_META_ABI)
            return tok.functions.balanceOf(vault_cs).call() > 0
        except Exception:
            return False

    def quote_swap(self, path: list, amount_in: int) -> int:
        if len(path) < 2 or amount_in == 0:
            return amount_in
        try:
            cs_path = [Web3.to_checksum_address(p) for p in path]
            out     = self.qs.functions.getAmountsOut(amount_in, cs_path).call()
            return out[-1] if out else 0
        except Exception:
            return 0

    def batch_aave_health(self, users: list[str], batch_size: int = 250) -> dict[str, float]:
        """
        Fetch getUserAccountData for many users via Multicall3 in one RPC call per 250 users.
        Returns {user_lower: health_factor}. Users with no debt (hf=999) or errors are omitted.
        Without Multicall3: 1000 users = 1000 RPC calls.
        With Multicall3:    1000 users =    4 RPC calls.
        """
        results: dict[str, float] = {}
        pool_cs = Web3.to_checksum_address(AAVE_POOL)
        mc3     = self.w3.eth.contract(
            address=Web3.to_checksum_address(MULTICALL3), abi=MULTICALL3_ABI
        )
        for i in range(0, len(users), batch_size):
            batch = [Web3.to_checksum_address(u) for u in users[i:i + batch_size]]
            calls = [
                (pool_cs, True, _AAVE_ACCOUNT_SEL + abi_encode(['address'], [u]))
                for u in batch
            ]
            try:
                raw = mc3.functions.aggregate3(calls).call()
                for j, (success, ret) in enumerate(raw):
                    if not success or len(ret) < 192:
                        continue
                    col, debt, _, _, _, hf_raw = abi_decode(
                        ['uint256', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256'], ret
                    )
                    if debt == 0:
                        continue
                    hf = 999.0 if hf_raw >= 10 ** 27 else hf_raw / 1e18
                    results[batch[j].lower()] = hf
            except Exception as e:
                logger.warning(f'Multicall3 Aave batch {i // batch_size}: {e}')
        return results

    def batch_comet_borrowers(self, users: list[str], comet_addr: str,
                               batch_size: int = 250) -> dict[str, int]:
        """
        Batch-check borrowBalanceOf on Comet for many users via Multicall3.
        Returns {user_lower: borrow_balance_raw} for users with balance > 0.
        """
        results: dict[str, int] = {}
        comet_cs = Web3.to_checksum_address(comet_addr)
        mc3      = self.w3.eth.contract(
            address=Web3.to_checksum_address(MULTICALL3), abi=MULTICALL3_ABI
        )
        for i in range(0, len(users), batch_size):
            batch = [Web3.to_checksum_address(u) for u in users[i:i + batch_size]]
            calls = [
                (comet_cs, True, _COMET_BORROW_SEL + abi_encode(['address'], [u]))
                for u in batch
            ]
            try:
                raw = mc3.functions.aggregate3(calls).call()
                for j, (success, ret) in enumerate(raw):
                    if not success or len(ret) < 32:
                        continue
                    bal = int(abi_decode(['uint256'], ret)[0])
                    if bal > 0:
                        results[batch[j].lower()] = bal
            except Exception as e:
                logger.warning(f'Multicall3 Comet borrow batch {i // batch_size}: {e}')
        return results

    def batch_comet_liquidatable(self, users: list[str], comet_addr: str,
                                  batch_size: int = 250) -> set[str]:
        """
        Batch isLiquidatable() on Comet via Multicall3.
        Returns set of user_lower addresses that are liquidatable.
        """
        liquidatable: set[str] = set()
        comet_cs = Web3.to_checksum_address(comet_addr)
        mc3      = self.w3.eth.contract(
            address=Web3.to_checksum_address(MULTICALL3), abi=MULTICALL3_ABI
        )
        for i in range(0, len(users), batch_size):
            batch = [Web3.to_checksum_address(u) for u in users[i:i + batch_size]]
            calls = [
                (comet_cs, True, _COMET_IS_LIQ_SEL + abi_encode(['address'], [u]))
                for u in batch
            ]
            try:
                raw = mc3.functions.aggregate3(calls).call()
                for j, (success, ret) in enumerate(raw):
                    if success and len(ret) >= 32:
                        if abi_decode(['bool'], ret)[0]:
                            liquidatable.add(batch[j].lower())
            except Exception as e:
                logger.warning(f'Multicall3 Comet liq batch {i // batch_size}: {e}')
        return liquidatable

    def read_position(self, user: str) -> Optional[UserPosition]:
        try:
            cs = Web3.to_checksum_address(user)
        except Exception:
            return None
        try:
            acct     = self.pool.functions.getUserAccountData(cs).call()
            col_base, debt_base, _, _, _, hf_raw = acct
            col_usd  = Decimal(col_base)  / Decimal('1e8')
            debt_usd = Decimal(debt_base) / Decimal('1e8')

            if debt_usd < MIN_DEBT_USD:
                return None
            if hf_raw >= 10**27:
                hf = Decimal('999')
            else:
                hf = Decimal(hf_raw) / Decimal('1e18')
        except Exception as e:
            logger.debug(f'getUserAccountData {user[:12]}: {e}')
            return None

        pos = UserPosition(user=user.lower(), health_factor=hf,
                           total_collateral_usd=col_usd, total_debt_usd=debt_usd)
        for al, meta in self.reserves.items():
            try:
                cs_asset = Web3.to_checksum_address(al)
                rd       = self.dp.functions.getUserReserveData(cs_asset, cs).call()
                a_bal, stable_debt, var_debt = int(rd[0]), int(rd[1]), int(rd[2])
                use_as_col = bool(rd[8])
                if a_bal == 0 and (stable_debt + var_debt) == 0:
                    continue
                rp = UserReservePosition(
                    asset=cs_asset, symbol=meta.symbol, decimals=meta.decimals,
                    a_bal=a_bal, stable_debt=stable_debt, var_debt=var_debt,
                    use_as_col=use_as_col, reserve=meta,
                )
                if use_as_col and a_bal > 0:
                    pos.collateral.append(rp)
                if (stable_debt + var_debt) > 0:
                    pos.debt.append(rp)
            except Exception as e:
                logger.debug(f'getUserReserveData {meta.symbol}: {e}')
        return pos

    def zero_rpc_sweep(self, positions: dict) -> list[str]:
        """Re-estimate HF for cached positions using refreshed prices.  Zero RPC."""
        if not self.prices_valid:
            return []
        liquidatable = []
        RAY = Decimal('1e27')
        for user, pos in positions.items():
            if not isinstance(pos, UserPosition):
                continue
            try:
                weighted_col = Decimal('0')
                total_debt   = Decimal('0')
                for col in pos.collateral:
                    price = self.prices.get(col.asset.lower())
                    if not price:
                        continue
                    liq_idx = self.liq_indexes.get(col.asset.lower(), RAY)
                    actual  = (Decimal(col.a_bal) * liq_idx) / RAY
                    col_usd = (actual / Decimal(10 ** col.decimals)) * price
                    weighted_col += col_usd * col.reserve.liq_threshold
                for debt in pos.debt:
                    price = self.prices.get(debt.asset.lower())
                    if not price:
                        continue
                    total_debt += (Decimal(debt.total_debt) / Decimal(10 ** debt.decimals)) * price
                if total_debt == 0:
                    continue
                hf_est = weighted_col / total_debt
                if hf_est < Decimal('1.0'):
                    liquidatable.append(user)
                elif hf_est < Decimal('1.05'):
                    logger.debug(f'[Sweep] {user[:12]} HF_est={float(hf_est):.4f}')
            except Exception:
                pass
        return liquidatable


# ═══════════════════════════════════════════════════════════════════════
# SWAP PATH BUILDER
# ═══════════════════════════════════════════════════════════════════════
def build_swap_path(col_addr: str, debt_addr: str) -> tuple:
    col  = Web3.to_checksum_address(col_addr)
    debt = Web3.to_checksum_address(debt_addr)
    if col.lower() == debt.lower():
        return QUICKSWAP_V2, RT_QUICKSWAP, [col, debt], []
    direct_pairs = {
        frozenset([WETH.lower(), USDC.lower()]),
        frozenset([WETH.lower(), USDT.lower()]),
        frozenset([WETH.lower(), DAI.lower()]),
        frozenset([WETH.lower(), WPOL.lower()]),
        frozenset([WETH.lower(), WBTC.lower()]),
        frozenset([USDC.lower(), USDT.lower()]),
        frozenset([USDC.lower(), DAI.lower()]),
        frozenset([USDC.lower(), WPOL.lower()]),
        frozenset([USDC.lower(), WBTC.lower()]),
        frozenset([USDT.lower(), DAI.lower()]),
        frozenset([WPOL.lower(), USDT.lower()]),
    }
    pair = frozenset([col.lower(), debt.lower()])
    if pair in direct_pairs:
        return QUICKSWAP_V2, RT_QUICKSWAP, [col, debt], []
    # Route through USDC as bridge
    if col.lower() != USDC.lower() and debt.lower() != USDC.lower():
        return QUICKSWAP_V2, RT_QUICKSWAP, [col, USDC, debt], []
    return QUICKSWAP_V2, RT_QUICKSWAP, [col, debt], []


# ═══════════════════════════════════════════════════════════════════════
# OPPORTUNITY FINDER
# ═══════════════════════════════════════════════════════════════════════
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
            return None
        if not self.reader.prices_are_fresh():
            logger.warning(f'Prices stale — skip evaluation for {pos.user[:12]}')
            return None

        close_factor = (Decimal('1.0') if pos.health_factor < Decimal('0.95')
                        else Decimal('0.5'))
        best: Optional[Opportunity] = None
        best_net = Decimal('-9999')

        for col_pos in pos.collateral:
            col_price = self.reader.get_price(col_pos.asset)
            if not col_price:
                continue
            bonus = col_pos.reserve.liq_bonus
            if bonus <= Decimal('1'):
                continue

            for debt_pos in pos.debt:
                if debt_pos.asset.lower() in self.reader.no_flashloan_tokens:
                    continue
                debt_price = self.reader.get_price(debt_pos.asset)
                if not debt_price:
                    continue

                debt_total_usd    = (Decimal(debt_pos.total_debt)
                                     / Decimal(10 ** debt_pos.decimals) * debt_price)
                debt_to_cover_usd = debt_total_usd * close_factor

                if debt_to_cover_usd < MIN_DEBT_USD:
                    continue

                router, rt, path, fees = build_swap_path(col_pos.asset, debt_pos.asset)
                cs_path = [Web3.to_checksum_address(p) for p in path]
                n_hops  = max(1, len(path) - 1)
                gas_usd = self._gas_cost_usd(n_hops)

                gross_usd = debt_to_cover_usd * (bonus - Decimal('1'))
                net_usd   = gross_usd - gas_usd

                # Pre-check swap quote (only when col != debt)
                same_asset = col_pos.asset.lower() == debt_pos.asset.lower()
                if not same_asset:
                    col_received_usd = debt_to_cover_usd * bonus
                    col_received_raw = int(
                        col_received_usd * Decimal(10 ** col_pos.decimals) / col_price
                    )
                    if col_received_raw > 0:
                        swap_out = self.reader.quote_swap(cs_path, col_received_raw)
                        if swap_out == 0:
                            continue
                        swap_usd = (Decimal(swap_out) / Decimal(10 ** debt_pos.decimals)
                                    * debt_price)
                        # Require at least 95% of expected (slippage guard)
                        if swap_usd < debt_to_cover_usd * Decimal('0.95'):
                            continue
                        gross_usd = swap_usd - debt_to_cover_usd
                        net_usd   = gross_usd - gas_usd

                if net_usd <= best_net:
                    continue

                debt_to_cover_raw = int(
                    debt_to_cover_usd * Decimal(10 ** debt_pos.decimals) / debt_price
                )
                col_received_usd = debt_to_cover_usd * bonus
                col_received_raw = int(
                    col_received_usd * Decimal(10 ** col_pos.decimals) / col_price
                )
                min_col_raw    = int(col_received_raw * Decimal('0.95'))
                min_profit_raw = int(
                    self.min_profit_usd * Decimal(10 ** debt_pos.decimals) / debt_price
                )

                best_net = net_usd
                best = Opportunity(
                    user=pos.user,
                    health_factor=pos.health_factor,
                    close_factor=close_factor,
                    col_asset=col_pos.asset,       col_symbol=col_pos.symbol,
                    col_amount_raw=col_pos.a_bal,  col_usd=col_received_usd,
                    debt_asset=debt_pos.asset,     debt_symbol=debt_pos.symbol,
                    debt_to_cover_raw=debt_to_cover_raw,
                    debt_usd=debt_to_cover_usd,
                    liq_bonus=bonus,
                    gross_profit_usd=gross_usd, gas_cost_usd=gas_usd, net_profit_usd=net_usd,
                    block_number=block_num,
                    swap_router=router, swap_router_type=rt, swap_path=cs_path, v3_fees=fees,
                    min_col_raw=min_col_raw, min_profit_raw=min_profit_raw,
                    protocol=(pos.protocol if hasattr(pos, 'protocol')
                               and isinstance(pos.protocol, int) else PROTOCOL_AAVE_V3),
                    extra_data=getattr(pos, 'extra_data', b''),
                )

        return best


# ═══════════════════════════════════════════════════════════════════════
# PARAM ENCODER  (Aave V3 + Morpho Blue)
# ═══════════════════════════════════════════════════════════════════════
def encode_params(opp: Opportunity) -> bytes:
    deadline = int(time.time()) + 120
    tup = (
        opp.protocol,
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
        opp.extra_data,
    )
    return abi_encode([NEW_PARAMS_TYPE], [tup])

def encode_morpho_extra(loan_token: str, collateral_token: str,
                         oracle: str, irm: str, lltv: int) -> bytes:
    return abi_encode(
        ['(address,address,address,address,uint256)'],
        [(Web3.to_checksum_address(loan_token),
          Web3.to_checksum_address(collateral_token),
          Web3.to_checksum_address(oracle),
          Web3.to_checksum_address(irm),
          lltv)]
    )


# ═══════════════════════════════════════════════════════════════════════
# MAIN BOT
# ═══════════════════════════════════════════════════════════════════════
class HybridBot:

    HF_WATCH_BELOW       = Decimal('1.10')
    HF_ALERT_BELOW       = Decimal('1.05')
    HF_LIQUIDATE_BELOW   = Decimal('1.00')
    RESERVE_RELOAD_BLKS  = 1_000
    PRICE_REFRESH_SECS   = 45
    HTTP_POLL_INTERVAL   = 3     # seconds between HTTP block polls

    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(RPC_HTTP, request_kwargs={'timeout': 20}))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        # Separate provider for eth_getLogs cold-start scanning
        # (OnFinality: 10k blocks/call vs Alchemy free: 10 blocks/call)
        self._logs_w3 = Web3(Web3.HTTPProvider(LOGS_HTTP, request_kwargs={'timeout': 30}))
        self._logs_w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        blk = self._rpc(lambda: self.w3.eth.block_number)
        logger.info(f'Connected to Polygon (block {blk:,})')
        logs_label = 'OnFinality' if 'onfinality' in LOGS_HTTP else 'Alchemy'
        logger.info(f'Logs RPC   : {logs_label} ({LOGS_HTTP.split("/")[2]})')

        _min_raw = (os.getenv('MIN_PROFIT_USD') or '10').strip().lstrip('$')
        try:
            self.min_profit_usd = Decimal(_min_raw)
        except Exception:
            self.min_profit_usd = Decimal('10')
        self.execution_enabled = os.getenv('EXECUTION_ENABLED', '').strip().lower() == 'true'

        pk = (os.getenv('PRIVATE_KEY') or '').strip()
        if pk:
            pk = '0x' + pk.lstrip('0x')
            self.private_key = pk
            self.account     = self.w3.eth.account.from_key(pk)
            logger.info(f'Wallet: {self.account.address}')
        else:
            if self.execution_enabled:
                raise ValueError('PRIVATE_KEY required when EXECUTION_ENABLED=true')
            self.private_key = None
            self.account     = None

        if CONTRACT_ADDRESS and CONTRACT_ABI:
            self.contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(CONTRACT_ADDRESS),
                abi=CONTRACT_ABI,
            )
            logger.info(f'Hybrid contract: {CONTRACT_ADDRESS}')
            try:
                owner  = self.contract.functions.owner().call()
                paused = self.contract.functions.paused().call()
                logger.info(f'  owner={owner}  paused={paused}')
                if self.account and owner.lower() != self.account.address.lower():
                    logger.error('Wallet is NOT owner — execution will revert')
                if paused:
                    logger.error('Contract is PAUSED — execution will revert')
            except Exception as e:
                logger.warning(f'Contract state check: {e}')
        else:
            self.contract = None
            logger.warning('No hybrid contract loaded')

        self.reader   = PositionReader(self.w3)
        self.finder   = OpportunityFinder(self.reader, self.min_profit_usd)
        self.morpho   = MorphoMonitor(self.w3)
        self.compound = CompoundV3Monitor(self.w3)
        self.registry = UserRegistry()
        self._exec    = concurrent.futures.ThreadPoolExecutor(max_workers=12)

        self._nonce_mgr:     Optional[NonceManager] = None
        self._cached_pos:    dict[str, object]      = {}
        self._hf_history:    dict[str, deque]       = {}
        self._current_block: int                    = 0
        self._last_reload:   int                    = 0

        # HTTP poll state — tracks last block processed by the HTTP poll loop
        self._http_last_block: int = 0

        # HF change-log dedup: only log [AaveV3] At risk when HF shifts > this fraction
        # Suppresses identical-reading spam when price is flat.
        self._last_logged_hf: dict[str, float] = {}   # addr -> last logged HF
        self.HF_LOG_CHANGE_THRESHOLD = 0.005           # 0.5% change required to re-log

        self.stats = dict(
            blocks=0, events=0,
            liquidations_attempted=0, liquidations_succeeded=0,
            liquidations_simfail=0,
            compound_absorbs=0,
        )

    # ── RPC helpers ─────────────────────────────────────────────────

    def _rpc(self, fn, max_attempts: int = 8):
        delay = 3
        for attempt in range(1, max_attempts + 1):
            try:
                return fn()
            except Exception as e:
                err = str(e)
                is_rate_limit = '429' in err or 'Too Many Requests' in err or 'rate limit' in err.lower()
                if attempt == max_attempts:
                    logger.error(f'RPC failed after {max_attempts} attempts: {e}')
                    return None
                if is_rate_limit:
                    delay = max(delay, 15)  # minimum 15s back-off on 429
                    logger.warning(f'RPC 429 rate-limited — backing off {delay}s')
                else:
                    logger.warning(f'RPC attempt {attempt}: {e} — retry in {delay}s')
                time.sleep(delay)
                delay = min(delay * 2, 120)

    async def _run(self, fn, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._exec, fn, *args)

    # ── Init ────────────────────────────────────────────────────────

    def _verify_approvals(self):
        """
        Check that the deployed contract has non-zero allowances for critical
        spenders (Balancer Vault + Comet).  Flash loan repayment and buyCollateral
        BOTH revert silently if these are zero.  Run fix_approvals.py if warnings appear.
        """
        if not self.contract:
            return
        ERC20_ALLOW_ABI = [{
            'name': 'allowance',
            'inputs':  [{'name': 'o', 'type': 'address'}, {'name': 's', 'type': 'address'}],
            'outputs': [{'name': '', 'type': 'uint256'}],
            'stateMutability': 'view', 'type': 'function',
        }]
        contract_cs = self.contract.address
        checks = [
            (USDC,         BALANCER_VAULT, 'USDC  → Balancer (flash-loan repayment)'),
            (USDC,         COMET_USDC,     'USDC  → Comet    (buyCollateral)'),
            (WETH,         BALANCER_VAULT, 'WETH  → Balancer'),
            (WBTC,         BALANCER_VAULT, 'WBTC  → Balancer'),
        ]
        missing = []
        for tok, spender, label in checks:
            try:
                c   = self.w3.eth.contract(
                    address=Web3.to_checksum_address(tok), abi=ERC20_ALLOW_ABI
                )
                amt = c.functions.allowance(
                    contract_cs, Web3.to_checksum_address(spender)
                ).call()
                if amt == 0:
                    missing.append(label)
            except Exception:
                pass
        if missing:
            logger.error('━' * 65)
            logger.error('⚠️  CONTRACT MISSING CRITICAL ALLOWANCES — every trade WILL REVERT')
            logger.error('   Run:  python3 bot/fix_approvals.py')
            for m in missing:
                logger.error(f'   ❌  {m}')
            logger.error('━' * 65)
        else:
            logger.info('✅ Contract allowances OK (Balancer + Comet)')

    def _startup(self):
        self.reader.load_all_reserves()
        self.reader.refresh_prices()
        self.reader.refresh_liquidity_indexes()
        self.reader.check_flashloan_liquidity()
        self._verify_approvals()
        self._last_reload = self._rpc(lambda: self.w3.eth.block_number) or 0
        try:
            self.compound.initialize()
        except Exception as e:
            logger.warning(f'Compound V3 init: {e}')
        if self.account:
            self._nonce_mgr = NonceManager(self.w3, self.account.address)
            logger.info(f'NonceManager init: nonce={self._nonce_mgr._nonce}')

    # ── Event parsing ───────────────────────────────────────────────

    def _extract_user(self, log: dict) -> Optional[tuple[str, str]]:
        """Returns (user_address, protocol_name) or None."""
        if log.get('removed', False):
            return None
        topics = log.get('topics', [])
        if not topics:
            return None
        t0 = topics[0].hex() if isinstance(topics[0], bytes) else topics[0]

        def _addr(t) -> str:
            h = t.hex() if isinstance(t, bytes) else t
            return '0x' + h[-40:]

        if t0 in (AAVE_BORROW_TOPIC, AAVE_SUPPLY_TOPIC, AAVE_REPAY_TOPIC):
            if len(topics) >= 3:
                return _addr(topics[2]), 'AAVE_V3'
        elif t0 == MORPHO_BORROW_TOPIC:
            if len(topics) >= 3:
                return _addr(topics[2]), 'MORPHO'
        elif t0 in (C3_SUPPLY_TOPIC, C3_COL_TOPIC, C3_WITHDRAW_TOPIC):
            if len(topics) >= 3:
                return _addr(topics[2]), 'COMPOUND_V3'
        return None

    # ── Block handler ───────────────────────────────────────────────

    async def _handle_block(self, block_num: int, events: list):
        self._current_block = max(self._current_block, block_num)
        self.stats['blocks'] += 1

        # Periodic reserve / liquidity reload
        if block_num - self._last_reload >= self.RESERVE_RELOAD_BLKS:
            await self._run(self.reader.load_all_reserves)
            await self._run(self.reader.check_flashloan_liquidity)
            await self._run(self.reader.refresh_liquidity_indexes)
            self._last_reload = block_num

        # Register users from events
        event_users: set[str] = set()
        for log in events:
            result = self._extract_user(log)
            if result:
                user_raw, protocol = result
                try:
                    user = Web3.to_checksum_address(user_raw).lower()
                except Exception:
                    continue
                event_users.add(user)
                self.registry.add_user(user, protocol, block_num)
                self.stats['events'] += 1

        # Throttle at-risk user re-checks: only re-check if either:
        #   (a) there was a live event for them this block, OR
        #   (b) their cooldown has elapsed (based on HF proximity to 1.0)
        at_risk: set[str] = set()
        for u in self.registry.get_at_risk():
            addr = u.address.lower()
            if addr in event_users:
                at_risk.add(addr)   # always check event-triggered users
                continue
            min_hf = self.registry.get_min_hf(addr)
            # Determine cooldown: imminent positions checked more often
            cooldown = 15   # default: HF 1.05-1.10
            for hf_thresh, blks in sorted(AT_RISK_COOLDOWN.items()):
                if min_hf < hf_thresh:
                    cooldown = blks
                    break
            if block_num - u.last_check_block >= cooldown:
                at_risk.add(addr)

        all_users = event_users | at_risk

        if not all_users:
            return

        sem = asyncio.Semaphore(4)

        async def _process(user: str, from_event: bool):
            async with sem:
                try:
                    await self._evaluate_user(user, block_num, from_event)
                except Exception as e:
                    logger.error(f'Process {user[:12]}: {e}')

        await asyncio.gather(
            *[_process(u, u in event_users) for u in all_users],
            return_exceptions=True,
        )

        if block_num % 20 == 0:
            self.registry.save()

    # ── User evaluation ─────────────────────────────────────────────

    async def _evaluate_user(self, user: str, block_num: int, from_event: bool):
        tracked = self.registry.users.get(user)
        if not tracked:
            return

        # Record that we're doing a full check now (used for throttle cooldowns)
        tracked.last_check_block = block_num

        # ── Aave V3 ────────────────────────────────────────────────
        if 'AAVE_V3' in tracked.protocols:
            pos = await self._run(self.reader.read_position, user)
            if pos:
                self._cached_pos[user] = pos
                hf = pos.health_factor
                self.registry.update_hf(user, 'AAVE_V3', float(hf), block_num)
                if user not in self._hf_history:
                    self._hf_history[user] = deque(maxlen=20)
                self._hf_history[user].append((block_num, hf))

                col_parts  = self._format_col(pos)
                debt_parts = self._format_debt(pos)

                if hf < self.HF_LIQUIDATE_BELOW:
                    logger.warning(
                        f'[AaveV3] LIQUIDATABLE {user[:12]} HF={float(hf):.4f} | '
                        f'col:[{", ".join(col_parts)}] debt:[{", ".join(debt_parts)}] | '
                        f'col=${float(pos.total_collateral_usd):,.0f} '
                        f'debt=${float(pos.total_debt_usd):,.0f}'
                    )
                    self.stats['liquidations_attempted'] += 1
                    opp = await self._run(self.finder.find_best, pos, block_num)
                    if opp:
                        await self._fire_opportunity(opp, 'AAVE_V3')
                    else:
                        logger.warning(f'[AaveV3] {user[:12]} liquidatable but no profitable opportunity')
                elif hf < self.HF_ALERT_BELOW:
                    hf_f = float(hf)
                    prev = self._last_logged_hf.get(user, 999.0)
                    hf_changed = abs(hf_f - prev) / max(prev, 0.001) > self.HF_LOG_CHANGE_THRESHOLD
                    if hf_changed or prev == 999.0:
                        self._last_logged_hf[user] = hf_f
                        logger.warning(
                            f'[AaveV3] At risk {user[:12]} HF={hf_f:.4f} | '
                            f'col:[{", ".join(col_parts)}] debt:[{", ".join(debt_parts)}]'
                        )

        # ── Compound V3 ─────────────────────────────────────────────
        if 'COMPOUND_V3' in tracked.protocols:
            c3_pos = await self._run(self.compound.check_position, user)
            if c3_pos and c3_pos.is_liquidatable:
                opp = await self._run(self._build_c3_opportunity, c3_pos, block_num)
                if opp:
                    logger.warning(
                        f'[CompV3] LIQUIDATABLE {user[:12]} '
                        f'borrow={float(c3_pos.borrow_usd):.2f} USDC '
                        f'col={opp.col_symbol} net≈${float(opp.net_profit_usd):.4f}'
                    )
                    self.stats['liquidations_attempted'] += 1
                    await self._fire_opportunity(opp, 'COMPOUND_V3')
                else:
                    logger.info(
                        f'[CompV3] {user[:12]} liquidatable but not profitable '
                        f'(borrow={float(c3_pos.borrow_usd):.2f} USDC)'
                    )

        # ── Morpho Blue ─────────────────────────────────────────────
        if 'MORPHO' in tracked.protocols and self.morpho.markets:
            for market_id in list(self.morpho.markets):
                try:
                    morpho_pos = await self._run(self.morpho.check_position, market_id, user)
                    if morpho_pos and morpho_pos.is_liquidatable:
                        logger.warning(
                            f'[Morpho] LIQUIDATABLE {user[:12]} '
                            f'market={market_id[:12]} '
                            f'ltv={float(morpho_pos.ltv_estimate):.4f}'
                        )
                        # Build and fire opportunity via contract flash loan
                        mkt = self.morpho.markets.get(market_id)
                        if mkt:
                            extra = encode_morpho_extra(
                                mkt.loan_token, mkt.collateral_token,
                                mkt.oracle, mkt.irm, int(mkt.lltv * Decimal('1e18'))
                            )
                            col_price  = self.reader.get_price(mkt.collateral_token)
                            loan_price = self.reader.get_price(mkt.loan_token)
                            if col_price and loan_price and morpho_pos.collateral > 0:
                                # Repay up to full borrow
                                ci = self.morpho.markets[market_id]
                                from protocols.morpho import MorphoMarket
                                router, rt, path, fees = build_swap_path(
                                    mkt.collateral_token, mkt.loan_token
                                )
                                cs_path = [Web3.to_checksum_address(p) for p in path]
                                opp = Opportunity(
                                    user=user,
                                    health_factor=Decimal('0'),
                                    close_factor=Decimal('1'),
                                    col_asset=Web3.to_checksum_address(mkt.collateral_token),
                                    col_symbol=mkt.collateral_token[:8],
                                    col_amount_raw=morpho_pos.collateral,
                                    col_usd=Decimal(morpho_pos.collateral) * col_price,
                                    debt_asset=Web3.to_checksum_address(mkt.loan_token),
                                    debt_symbol=mkt.loan_token[:8],
                                    debt_to_cover_raw=morpho_pos.borrow_shares,
                                    debt_usd=Decimal(morpho_pos.borrow_shares) * loan_price,
                                    liq_bonus=Decimal('1'),
                                    gross_profit_usd=Decimal('0'),
                                    gas_cost_usd=self.finder._gas_cost_usd(len(path) - 1),
                                    net_profit_usd=Decimal('-1'),
                                    block_number=block_num,
                                    swap_router=router,
                                    swap_router_type=rt,
                                    swap_path=cs_path,
                                    v3_fees=fees,
                                    min_col_raw=0,
                                    min_profit_raw=0,
                                    protocol=PROTOCOL_MORPHO_BLUE,
                                    extra_data=extra,
                                )
                                self.stats['liquidations_attempted'] += 1
                                await self._fire_opportunity(opp, 'MORPHO')
                except Exception as e:
                    logger.debug(f'[Morpho] check {user[:12]} market {market_id[:12]}: {e}')

    def _format_col(self, pos: UserPosition) -> list[str]:
        parts = []
        for c in pos.collateral:
            amt   = c.a_bal / (10 ** c.decimals)
            price = self.reader.get_price(c.asset) or Decimal('0')
            usd   = Decimal(str(amt)) * price
            parts.append(f'{c.symbol}={amt:.4f}(${float(usd):,.0f})')
        return parts

    def _format_debt(self, pos: UserPosition) -> list[str]:
        parts = []
        for d in pos.debt:
            amt   = d.total_debt / (10 ** d.decimals)
            price = self.reader.get_price(d.asset) or Decimal('0')
            usd   = Decimal(str(amt)) * price
            parts.append(f'{d.symbol}={amt:.4f}(${float(usd):,.0f})')
        return parts

    # ── Compound V3 opportunity builder (via contract flash loan) ──────

    def _build_c3_opportunity(self, c3_pos, block_num: int) -> Optional[Opportunity]:
        """
        Build an Opportunity for Compound V3 via hybrid contract executeLiquidation().

        The contract does ALL of this inside a Balancer flash loan:
          1. absorb(contract_address, [user])        — seize collateral into Comet reserves
          2. buyCollateral(col, minCol, usdc, self)  — buy seized collateral at discount
          3. Swap col → USDC via DEX
          4. Repay Balancer flash loan
          5. Sweep profit to owner

        Nothing is called directly from the wallet.  Execution = contract.executeLiquidation()
        with protocol=COMPOUND_V3 (=2), identical path to Aave V3.
        """
        usdc_to_spend = c3_pos.borrow_balance   # USDC (6-decimal) we flash loan
        if usdc_to_spend == 0 or not c3_pos.collaterals:
            return None

        best_net: Decimal = Decimal('-9999')
        best_opp: Optional[Opportunity] = None

        for asset_cs, col_bal in c3_pos.collaterals.items():
            if col_bal == 0:
                continue
            ci = self.compound.asset_infos.get(asset_cs.lower())
            if not ci:
                continue

            # How much collateral do we receive for spending usdc_to_spend USDC?
            col_received = self.compound.quote_collateral(asset_cs, usdc_to_spend)
            if col_received == 0:
                continue

            # DEX swap path: collateral → USDC
            # Contract validates: swapPath[0]==collateralAsset, swapPath[-1]==debtAsset
            router, rt, path, fees = build_swap_path(asset_cs, USDC)
            cs_path  = [Web3.to_checksum_address(p) for p in path]
            swap_out = self.reader.quote_swap(cs_path, col_received)
            if swap_out == 0:
                continue

            # Gas: flash loan contract costs ~600-800k gas
            gas_usd   = self.finder._gas_cost_usd(len(path) - 1)

            # Profit (both in USDC 6-decimal units → convert to USD)
            gross_raw = Decimal(swap_out) - Decimal(usdc_to_spend)
            gross_usd = gross_raw / Decimal('1000000')
            net_usd   = gross_usd - gas_usd

            if net_usd <= best_net:
                continue

            best_net = net_usd
            min_col  = int(col_received * 95 // 100)
            min_prof = int(self.min_profit_usd * Decimal('1000000'))

            best_opp = Opportunity(
                user=c3_pos.user,
                health_factor=Decimal('0'),   # not applicable for C3
                close_factor=Decimal('1'),
                col_asset=Web3.to_checksum_address(asset_cs),
                col_symbol=ci.symbol or asset_cs[:8],
                col_amount_raw=col_bal,
                col_usd=Decimal(col_received) / Decimal(10 ** ci.decimals),
                debt_asset=USDC,
                debt_symbol='USDC',
                debt_to_cover_raw=usdc_to_spend,
                debt_usd=Decimal(usdc_to_spend) / Decimal('1000000'),
                liq_bonus=Decimal('1'),
                gross_profit_usd=gross_usd,
                gas_cost_usd=gas_usd,
                net_profit_usd=net_usd,
                block_number=block_num,
                swap_router=router,
                swap_router_type=rt,
                swap_path=cs_path,
                v3_fees=fees,
                min_col_raw=min_col,
                min_profit_raw=min_prof,
                protocol=PROTOCOL_COMPOUND_V3,
                extra_data=b'',
            )

        return best_opp if best_net >= self.min_profit_usd else None

    # ── Aave / Morpho execution pipeline ───────────────────────────

    async def _fire_opportunity(self, opp: Opportunity, reason: str = '') -> None:
        sep = '═' * 64
        logger.info(sep)
        logger.info(f'💰 {reason}  block={opp.block_number}')
        logger.info(f'   User       : {opp.user}')
        logger.info(f'   HF         : {float(opp.health_factor):.6f}')
        logger.info(f'   Col/Debt   : {opp.col_symbol} / {opp.debt_symbol}')
        logger.info(f'   Gross      : ${float(opp.gross_profit_usd):.4f}')
        logger.info(f'   Gas cost   : ${float(opp.gas_cost_usd):.4f}')
        logger.info(f'   Net profit : ${float(opp.net_profit_usd):.4f}')
        logger.info(sep)

        if not self.execution_enabled or not self.contract or not self.account:
            logger.info('[MONITOR] Set EXECUTION_ENABLED=true to execute')
            return

        if opp.net_profit_usd < self.min_profit_usd:
            logger.info(f'Profit ${float(opp.net_profit_usd):.2f} < threshold — skip')
            return

        # 1. Price staleness guard
        if not self.reader.prices_are_fresh(max_age_secs=120):
            logger.warning('Prices stale (>120s) — abort')
            return

        # 2. Position freshness re-read
        fresh = await self._run(self.reader.read_position, opp.user)
        if fresh is None or fresh.health_factor >= Decimal('1.0'):
            logger.info('Position already liquidated or recovered — skip')
            return

        # 3. Balancer liquidity at execution time
        if not await self._run(self.reader.check_flashloan_liquidity_for, opp.debt_asset):
            logger.warning(f'Balancer has no liquidity for {opp.debt_symbol} — abort')
            return

        encoded = encode_params(opp)

        # 4. eth_call simulation
        try:
            self.contract.functions.executeLiquidation(encoded).call({
                'from': self.account.address, 'gas': 3_000_000,
            })
            logger.info('Simulation: PASS')
        except Exception as e:
            logger.warning(f'Simulation FAILED: {str(e)[:120]} — abort')
            self.stats['liquidations_simfail'] += 1
            return

        # 5. Submit
        await self._submit_liquidation(opp, encoded)

    async def _submit_liquidation(self, opp: Opportunity, encoded: bytes) -> None:
        try:
            # EIP-1559 gas pricing — Polygon validators strongly prefer it.
            # Use baseFee*2 + 30 Gwei priority fee for reliable 1-block inclusion.
            try:
                latest    = self.w3.eth.get_block('latest')
                base_fee  = latest.get('baseFeePerGas') or self.w3.eth.gas_price
            except Exception:
                base_fee  = self.w3.eth.gas_price

            priority_fee = Web3.to_wei(30, 'gwei')   # 30 Gwei tip — competitive on Polygon
            max_fee      = min(int(base_fee * 2) + priority_fee, MAX_GAS_PRICE_WEI)

            try:
                gas_est   = self.contract.functions.executeLiquidation(encoded).estimate_gas({
                    'from': self.account.address,
                    'maxFeePerGas': max_fee,
                    'maxPriorityFeePerGas': priority_fee,
                })
                gas_limit = int(gas_est * 1.3)
            except Exception as e:
                logger.warning(f'estimate_gas failed: {e} — fallback 800k')
                gas_limit = 800_000

            nonce  = await self._nonce_mgr.next()
            tx     = self.contract.functions.executeLiquidation(encoded).build_transaction({
                'from': self.account.address, 'nonce': nonce,
                'gas': gas_limit,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'chainId': 137,
            })
            signed  = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info(
                f'TX SENT: {tx_hash.hex()} | '
                f'baseFee={base_fee/1e9:.1f}G  prio={priority_fee/1e9:.0f}G  '
                f'max={max_fee/1e9:.1f}G  gas={gas_limit:,}'
            )
            logger.info(f'Polygonscan: https://polygonscan.com/tx/{tx_hash.hex()}')

            receipt = await self._wait_for_receipt(tx_hash)
            if receipt and receipt.get('status') == 1:
                self.stats['liquidations_succeeded'] += 1
                logger.info(f'✅ SUCCESS  gas_used={receipt["gasUsed"]:,}')
            else:
                logger.warning('❌ REVERTED')
                await self._nonce_mgr.sync()
        except Exception as e:
            logger.error(f'Submission error: {e}', exc_info=True)
            await self._nonce_mgr.sync()

    async def _wait_for_receipt(self, tx_hash, timeout: int = 60):
        start = time.time()
        while time.time() - start < timeout:
            try:
                receipt = self.w3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    return receipt
            except Exception:
                pass
            await asyncio.sleep(2)
        logger.warning(f'Receipt timeout after {timeout}s')
        return None

    # ── Background loops ────────────────────────────────────────────

    async def _price_refresh_loop(self):
        """Refresh prices every 45 s + zero-RPC sweep for immediate detections."""
        while True:
            try:
                await asyncio.sleep(self.PRICE_REFRESH_SECS)
                await self._run(self.reader.refresh_prices)
                await self._run(self.reader.refresh_liquidity_indexes)

                liquidatable = self.reader.zero_rpc_sweep(self._cached_pos)
                for user in liquidatable:
                    logger.warning(f'[PriceSweep] {user[:12]} estimated LIQUIDATABLE')
                    tracked = self.registry.users.get(user)
                    if not tracked or 'AAVE_V3' not in tracked.protocols:
                        continue

                    pos = await self._run(self.reader.read_position, user)
                    if pos is None:
                        continue

                    self._cached_pos[user] = pos
                    hf = pos.health_factor
                    self.registry.update_hf(user, 'AAVE_V3', float(hf), self._current_block)

                    if hf < self.HF_LIQUIDATE_BELOW:
                        logger.warning(
                            f'[PriceSweep] CONFIRMED {user[:12]} HF={float(hf):.4f} '
                            f'debt=${float(pos.total_debt_usd):,.0f}'
                        )
                        self.stats['liquidations_attempted'] += 1
                        opp = await self._run(self.finder.find_best, pos, self._current_block)
                        if opp:
                            await self._fire_opportunity(opp, 'PRICE_SWEEP')
                        else:
                            logger.warning(f'[PriceSweep] {user[:12]} liquidatable but no profitable opp')
                    elif hf < self.HF_ALERT_BELOW:
                        logger.warning(f'[PriceSweep] {user[:12]} at risk HF={float(hf):.4f}')
            except Exception as e:
                logger.warning(f'Price refresh loop: {e}', exc_info=True)

    async def _http_poll_loop(self):
        """
        Always-on HTTP polling for block + event discovery.
        Primary event source — runs alongside WSS (WSS is a bonus, not a requirement).
        Processes blocks and events that WSS may have missed due to rate limiting.
        """
        await asyncio.sleep(5)   # let cold start finish first
        tip = self._rpc(lambda: self.w3.eth.block_number) or self._current_block
        self._http_last_block = tip
        logger.info(f'HTTP poll loop starting at block {tip:,}')

        while True:
            try:
                await asyncio.sleep(self.HTTP_POLL_INTERVAL)
                tip = await self._run(lambda: self.w3.eth.block_number)
                if not tip or tip <= self._http_last_block:
                    continue

                from_blk = self._http_last_block + 1
                to_blk   = min(tip, self._http_last_block + 30)   # max 30 blocks at once

                # Fetch all watched events in this range
                try:
                    logs = await self._run(
                        lambda: self.w3.eth.get_logs({
                            'topics':    [ALL_WATCHED_TOPICS],
                            'fromBlock': from_blk,
                            'toBlock':   to_blk,
                        })
                    )
                except Exception as e:
                    logger.debug(f'HTTP poll getLogs {from_blk}-{to_blk}: {e}')
                    logs = []

                # Group by block
                by_block: dict[int, list] = {}
                for log in logs:
                    bn = log.get('blockNumber')
                    if bn is None:
                        continue
                    bn_int = int(bn, 16) if isinstance(bn, str) else int(bn)
                    by_block.setdefault(bn_int, []).append(log)

                # Process each new block (only if WSS hasn't handled it yet)
                for bn in range(from_blk, to_blk + 1):
                    block_logs = by_block.get(bn, [])
                    await self._handle_block(bn, block_logs)

                self._http_last_block = to_blk

            except Exception as e:
                logger.debug(f'HTTP poll loop: {e}')

    async def _new_user_scan_loop(self):
        """
        Periodically fetch the last N blocks of Aave + Compound V3 events
        to discover users that may have become active since the cold start.
        Runs every NEW_USER_SCAN_BLOCKS Polygon blocks (~72 min).
        """
        await asyncio.sleep(120)  # give cold start + HTTP poll time to stabilise
        last_scan = self._current_block
        logger.info('New-user scan loop started')

        while True:
            try:
                await asyncio.sleep(60)
                tip = self._current_block
                if tip - last_scan < NEW_USER_SCAN_BLOCKS:
                    continue

                from_blk = last_scan + 1
                to_blk   = tip
                logger.info(f'[UserScan] Scanning {from_blk}-{to_blk} for new borrowers...')

                new_users: dict[str, str] = {}   # address -> protocol

                # Use _logs_w3 (OnFinality: 10k blocks/call) for getLogs scans.
                # Chunk into COLD_START_CHUNK blocks to stay within limits.
                async def _scan_topic_chunked(address: str, topic: str) -> list[dict]:
                    collected = []
                    cs = from_blk
                    while cs <= to_blk:
                        ce = min(cs + COLD_START_CHUNK - 1, to_blk)
                        try:
                            batch = await self._run(
                                lambda a=address, t=topic, s=cs, e=ce: self._logs_w3.eth.get_logs({
                                    'address': Web3.to_checksum_address(a),
                                    'topics': [t],
                                    'fromBlock': s,
                                    'toBlock': e,
                                })
                            )
                            collected.extend(batch)
                        except Exception as ex:
                            logger.debug(f'UserScan getLogs {cs}-{ce}: {ex}')
                        cs = ce + 1
                    return collected

                # Aave V3 Borrow + Supply events
                for topic, proto in [
                    (AAVE_BORROW_TOPIC, 'AAVE_V3'),
                    (AAVE_SUPPLY_TOPIC, 'AAVE_V3'),
                ]:
                    try:
                        logs = await _scan_topic_chunked(AAVE_POOL, topic)
                        for log in logs:
                            if log.get('removed'):
                                continue
                            tpcs = log.get('topics', [])
                            if len(tpcs) >= 3:
                                raw  = tpcs[2]
                                addr = '0x' + (raw.hex() if isinstance(raw, bytes) else raw)[-40:]
                                try:
                                    new_users[Web3.to_checksum_address(addr).lower()] = proto
                                except Exception:
                                    pass
                    except Exception as e:
                        logger.debug(f'UserScan Aave: {e}')

                # Compound V3 Supply/Collateral events
                for topic in (C3_SUPPLY_TOPIC, C3_COL_TOPIC, C3_WITHDRAW_TOPIC):
                    try:
                        logs = await _scan_topic_chunked(COMET_USDC, topic)
                        for log in logs:
                            if log.get('removed'):
                                continue
                            tpcs = log.get('topics', [])
                            if len(tpcs) >= 3:
                                raw  = tpcs[2]
                                addr = '0x' + (raw.hex() if isinstance(raw, bytes) else raw)[-40:]
                                try:
                                    new_users[Web3.to_checksum_address(addr).lower()] = 'COMPOUND_V3'
                                except Exception:
                                    pass
                    except Exception as e:
                        logger.debug(f'UserScan C3: {e}')

                pre_count = len(self.registry.users)
                for addr, proto in new_users.items():
                    if addr not in self.registry.users:
                        self.registry.add_user(addr, proto, tip)
                added = len(self.registry.users) - pre_count
                logger.info(
                    f'[UserScan] Scanned {from_blk}-{to_blk}: '
                    f'{len(new_users)} event users, {added} new → '
                    f'registry={len(self.registry.users)}'
                )
                last_scan = to_blk

            except Exception as e:
                logger.warning(f'New user scan loop: {e}')

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(300)
            try:
                Path('heartbeat.txt').write_text(str(time.time()))
            except Exception:
                pass

    async def _stats_loop(self):
        while True:
            await asyncio.sleep(300)
            logger.info(
                f'[Stats] blocks={self.stats["blocks"]} '
                f'events={self.stats["events"]} '
                f'users={len(self.registry.users)} '
                f'at_risk={len(self.registry.get_at_risk())} '
                f'cached_pos={len(self._cached_pos)} '
                f'liq_attempts={self.stats["liquidations_attempted"]} '
                f'liq_ok={self.stats["liquidations_succeeded"]} '
                f'sim_fail={self.stats["liquidations_simfail"]} '
                f'c3_absorbs={self.stats["compound_absorbs"]}'
            )

    # ── WSS listener — sequential ACK, block event buffer ──────────

    async def _listen(self):
        """
        WSS subscription for block headers + protocol events.
        If WSS is rate-limited, the HTTP poll loop (always running) covers it.
        """
        reconnect_delay = 5

        while True:
            try:
                async with websockets.connect(
                    RPC_WSS,
                    ping_interval=20, ping_timeout=30,
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    reconnect_delay = 5
                    sub_blocks:   Optional[str] = None
                    sub_logs:     Optional[str] = None
                    log_sub_sent: bool          = False
                    event_buffer: dict[int, list] = {}
                    last_block:   int            = 0

                    # Step 1: subscribe to newHeads
                    await ws.send(json.dumps({
                        'jsonrpc': '2.0', 'id': 1,
                        'method': 'eth_subscribe', 'params': ['newHeads'],
                    }))
                    logger.info('WSS: sent newHeads sub (awaiting ACK)...')

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        # ── error responses (e.g. rate limiting) ─────────
                        if 'error' in msg or (
                            'code' in msg and msg.get('code') in (-32029, -32005)
                        ):
                            code = msg.get('code') or msg.get('error', {}).get('code', '?')
                            logger.warning(f'WSS error (code={code}): {msg.get("message") or msg.get("error", {}).get("message", "")} — HTTP poll provides coverage')
                            # Don't break — keep connection alive, HTTP poll compensates
                            continue

                        # ── subscription ACKs ─────────────────────────────
                        if ('id' in msg and 'result' in msg
                                and isinstance(msg.get('result'), str)):
                            msg_id = msg['id']
                            sub_id = msg['result']
                            if msg_id == 1 and sub_blocks is None:
                                sub_blocks = sub_id
                                logger.info(f'WSS: newHeads ACK sub={sub_blocks}')
                                if not log_sub_sent:
                                    await ws.send(json.dumps({
                                        'jsonrpc': '2.0', 'id': 2,
                                        'method': 'eth_subscribe',
                                        'params': ['logs', {'topics': [ALL_WATCHED_TOPICS]}],
                                    }))
                                    log_sub_sent = True
                                    logger.info('WSS: sent logs sub...')
                            elif msg_id == 2 and sub_logs is None:
                                sub_logs = sub_id
                                logger.info(f'WSS: logs ACK sub={sub_logs}')
                            continue

                        if 'params' not in msg:
                            continue
                        params = msg['params']
                        result = params.get('result', {})
                        sub_id = params.get('subscription')

                        if sub_id == sub_blocks and 'parentHash' in result:
                            block_num = int(result.get('number', '0x0'), 16)
                            if last_block > 0 and last_block in event_buffer:
                                events = event_buffer.pop(last_block)
                                asyncio.ensure_future(self._handle_block(last_block, events))
                            for old in sorted(k for k in event_buffer if k < block_num - 1):
                                asyncio.ensure_future(
                                    self._handle_block(old, event_buffer.pop(old))
                                )
                            self._current_block = max(self._current_block, block_num)
                            last_block          = block_num

                        elif sub_id == sub_logs and 'topics' in result:
                            if result.get('removed', False):
                                continue
                            blk = result.get('blockNumber')
                            if blk is None:
                                continue
                            bn  = int(blk, 16) if isinstance(blk, str) else int(blk)
                            event_buffer.setdefault(bn, []).append(result)

            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                logger.warning(f'WSS disconnected: {e} — retry in {reconnect_delay}s')
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 120)
            except Exception as e:
                logger.error(f'WSS error: {e}')
                await asyncio.sleep(reconnect_delay)

    # ── Warm restart — use cached registry, skip full historical scan ─

    async def _warm_restart(self):
        """
        Fast startup when hybrid_tracked_users.json is fresh (< CACHE_MAX_AGE_SECS).
        - Batch-refresh HF for every cached user via Multicall3
        - Run a short incremental getLogs scan from last_saved_block to now
          (catches any borrowers who appeared while we were offline)
        No need to rescan 400k blocks.
        """
        n = len(self.registry.users)
        at_risk_loaded = len(self.registry.get_at_risk())
        logger.info('=' * 65)
        logger.info(f'WARM RESTART: {n} users from cache ({at_risk_loaded} at risk)')
        logger.info('=' * 65)

        current_block = self._rpc(lambda: self.w3.eth.block_number)
        if not current_block:
            logger.error('WARM RESTART: cannot get block number — falling back to cold start')
            await self._cold_start_scan(COLD_START_BLOCKS_STALE)
            return

        # ── 1. Batch Multicall3 HF refresh for all known users ────────
        # batch_aave_health returns {addr_lower: float} — already divided by 1e18
        aave_users = [a for a, u in self.registry.users.items() if 'AAVE_V3' in u.protocols]
        logger.info(f'  Batch HF refresh: {len(aave_users)} Aave users via Multicall3...')
        if aave_users:
            try:
                hf_map = await self._run(self.reader.batch_aave_health, aave_users)
                updated = at_risk_now = 0
                for addr, hf_float in (hf_map or {}).items():
                    self.registry.update_hf(addr, 'AAVE_V3', hf_float, current_block)
                    updated += 1
                    if hf_float < 1.10:
                        at_risk_now += 1
                logger.info(f'  HF refresh done: {updated} updated, {at_risk_now} at risk')
            except Exception as e:
                logger.warning(f'  Batch HF refresh failed: {e}')

        # ── 2. Short incremental scan — blocks added while offline ────
        last_seen_block = self.registry.get_max_saved_block()
        gap_blocks      = current_block - last_seen_block if last_seen_block > 0 else 0
        if gap_blocks > 0 and last_seen_block > 0:
            scan_from = max(last_seen_block, current_block - COLD_START_BLOCKS_STALE)
            logger.info(
                f'  Incremental scan: blocks {scan_from:,}–{current_block:,} '
                f'({current_block - scan_from:,} blocks, gap={gap_blocks:,})'
            )
            gap_found: dict[str, str] = {}
            chunk_start = scan_from
            while chunk_start <= current_block:
                chunk_end = min(chunk_start + COLD_START_CHUNK - 1, current_block)
                for topic, proto in [
                    (AAVE_BORROW_TOPIC, 'AAVE_V3'),
                    (C3_SUPPLY_TOPIC,   'COMPOUND_V3'),
                ]:
                    try:
                        contract = Web3.to_checksum_address(
                            AAVE_POOL if proto == 'AAVE_V3' else COMPOUND_COMET
                        )
                        logs = await self._run(
                            lambda cs=chunk_start, ce=chunk_end, t=topic, c=contract:
                                self._logs_w3.eth.get_logs({
                                    'address': c, 'topics': [t],
                                    'fromBlock': cs, 'toBlock': ce,
                                })
                        )
                        for log in logs:
                            if log.get('removed'):
                                continue
                            tpcs = log.get('topics', [])
                            if len(tpcs) >= 3:
                                raw  = tpcs[2]
                                addr = '0x' + (raw.hex() if isinstance(raw, bytes) else raw)[-40:]
                                try:
                                    gap_found[Web3.to_checksum_address(addr).lower()] = proto
                                except Exception:
                                    pass
                    except Exception as e:
                        logger.debug(f'  Gap scan chunk error: {e}')
                chunk_start = chunk_end + 1
                await asyncio.sleep(0.1)

            new_users = 0
            for addr, proto in gap_found.items():
                if addr not in self.registry.users:
                    self.registry.add_user(addr, proto, current_block)
                    new_users += 1
            logger.info(f'  Gap scan: {len(gap_found)} borrowers found, {new_users} new')
        else:
            logger.info('  No gap since last save — skipping incremental scan')

        logger.info('=' * 65)
        logger.info(
            f'WARM RESTART COMPLETE: {len(self.registry.users)} users '
            f'({len(self.registry.get_at_risk())} at risk)'
        )
        logger.info('=' * 65)
        self.registry.save()

    # ── Cold start — full historical scan ───────────────────────────

    async def _cold_start_scan(self, n_blocks: int = COLD_START_BLOCKS_FULL):
        """
        Scan the last n_blocks blocks for Aave + Compound V3 events.
        Builds the initial user registry.  Chunked to respect RPC getLogs limits.
        n_blocks=COLD_START_BLOCKS_FULL  (~400k, ~56 days) on first-ever run.
        n_blocks=COLD_START_BLOCKS_STALE (~100k, ~14 days) on stale-cache recovery.
        """
        days_approx = int(n_blocks * 2.2 / 86400)
        logger.info('=' * 65)
        logger.info(f'COLD START: scanning last {n_blocks:,} blocks (~{days_approx} days) for active borrowers')
        logger.info('=' * 65)

        current_block = self._rpc(lambda: self.w3.eth.block_number)
        if not current_block:
            logger.error('COLD START: cannot get block number')
            return

        from_block = max(0, current_block - n_blocks)
        users_found: dict[str, str] = {}   # address.lower() -> protocol

        # ── Aave V3 Borrow events ────────────────────────────────────
        logger.info(f'  Scanning Aave V3 Borrow events {from_block}-{current_block}...')
        chunk_start = from_block
        total_events = 0
        while chunk_start <= current_block:
            chunk_end = min(chunk_start + COLD_START_CHUNK - 1, current_block)
            try:
                logs = await self._run(
                    lambda cs=chunk_start, ce=chunk_end: self._logs_w3.eth.get_logs({
                        'address':   Web3.to_checksum_address(AAVE_POOL),
                        'topics':    [AAVE_BORROW_TOPIC],
                        'fromBlock': cs,
                        'toBlock':   ce,
                    })
                )
                for log in logs:
                    if log.get('removed'):
                        continue
                    tpcs = log.get('topics', [])
                    if len(tpcs) >= 3:
                        raw  = tpcs[2]
                        addr = '0x' + (raw.hex() if isinstance(raw, bytes) else raw)[-40:]
                        try:
                            users_found[Web3.to_checksum_address(addr).lower()] = 'AAVE_V3'
                        except Exception:
                            pass
                total_events += len(logs)
                logger.info(f'  Aave chunk {chunk_start}-{chunk_end}: {len(logs)} events ({len(users_found)} unique users so far)')
            except Exception as e:
                logger.warning(f'  Aave chunk {chunk_start}-{chunk_end}: {e}')
                await asyncio.sleep(2)

            chunk_start = chunk_end + 1
            await asyncio.sleep(0.2)  # rate-limit courtesy

        logger.info(f'Aave V3: {total_events} events → {sum(1 for v in users_found.values() if v == "AAVE_V3")} unique borrowers')

        # ── Compound V3 Supply + Withdraw events ──────────────────────
        logger.info(f'  Scanning Compound V3 events {from_block}-{current_block}...')
        c3_events = 0
        for topic, label in [
            (C3_SUPPLY_TOPIC, 'Supply'),
            (C3_COL_TOPIC,    'SupplyCollateral'),
            (C3_WITHDRAW_TOPIC, 'Withdraw'),
        ]:
            chunk_start = from_block
            while chunk_start <= current_block:
                chunk_end = min(chunk_start + COLD_START_CHUNK - 1, current_block)
                try:
                    logs = await self._run(
                        lambda cs=chunk_start, ce=chunk_end, t=topic: self._logs_w3.eth.get_logs({
                            'address':   Web3.to_checksum_address(COMET_USDC),
                            'topics':    [t],
                            'fromBlock': cs,
                            'toBlock':   ce,
                        })
                    )
                    for log in logs:
                        if log.get('removed'):
                            continue
                        tpcs = log.get('topics', [])
                        if len(tpcs) >= 3:
                            raw  = tpcs[2]
                            addr = '0x' + (raw.hex() if isinstance(raw, bytes) else raw)[-40:]
                            try:
                                key = Web3.to_checksum_address(addr).lower()
                                if key not in users_found:
                                    users_found[key] = 'COMPOUND_V3'
                            except Exception:
                                pass
                    c3_events += len(logs)
                except Exception as e:
                    logger.debug(f'  C3 {label} chunk {chunk_start}-{chunk_end}: {e}')
                    await asyncio.sleep(2)
                chunk_start = chunk_end + 1
                await asyncio.sleep(0.2)

        logger.info(f'Compound V3: {c3_events} events → {sum(1 for v in users_found.values() if v == "COMPOUND_V3")} unique users')
        logger.info(f'Total unique users from event scan: {len(users_found)}')

        if not users_found:
            logger.info('COLD START: no users found — check RPC connectivity')
            return

        # ── Phase 1: Multicall3 batch screening ───────────────────────
        # Instead of 1 RPC call per user (O(n)), we batch 250 users per call
        # so 1 000 users = 4 calls.  Only at-risk users get individual follow-ups.
        aave_users = [u for u, p in users_found.items() if p == 'AAVE_V3']
        c3_users   = [u for u, p in users_found.items() if p == 'COMPOUND_V3']

        logger.info(f'Multicall3 batch screening: {len(aave_users)} Aave + {len(c3_users)} C3 users...')
        aave_hf_map  = await self._run(self.reader.batch_aave_health,  aave_users)
        c3_liq_set   = await self._run(
            self.reader.batch_comet_liquidatable, c3_users, COMET_USDC
        )
        logger.info(
            f'Batch screening done: '
            f'{len(aave_hf_map)} Aave borrowers | {len(c3_liq_set)} C3 liquidatable'
        )

        # ── Phase 2: Evaluate each discovered user ────────────────────
        cached = at_risk = liquidatable = skipped_dust = healthy = 0
        total  = len(users_found)
        processed = 0

        for user, proto in users_found.items():
            processed += 1
            if processed % 100 == 0 or processed == total:
                logger.info(f'  Cold start progress: {processed}/{total} '
                            f'(cached={cached} at_risk={at_risk} liq={liquidatable} dust={skipped_dust})')

            # Aave V3 evaluation — skip individual RPC for clearly healthy users
            if proto == 'AAVE_V3':
                hf_batch = aave_hf_map.get(user.lower())
                if hf_batch is None:
                    # Not in the batch result → no debt or RPC error → skip
                    self.registry.add_user(user, 'AAVE_V3', current_block)
                    healthy += 1
                    continue
                if hf_batch >= 1.10:
                    # Healthy — register but skip costly read_position
                    self.registry.add_user(user, 'AAVE_V3', current_block)
                    self.registry.update_hf(user, 'AAVE_V3', hf_batch, current_block)
                    healthy += 1
                    continue

                # At risk or liquidatable → fetch full position
                pos = await self._run(self.reader.read_position, user)
                if pos is None:
                    skipped_dust += 1
                    continue
                self._cached_pos[user] = pos
                self.registry.add_user(user, 'AAVE_V3', current_block)
                hf = pos.health_factor
                self.registry.update_hf(user, 'AAVE_V3', float(hf), current_block)
                cached += 1

                col_parts  = self._format_col(pos)
                debt_parts = self._format_debt(pos)
                pos_str    = (f'col:[{", ".join(col_parts)}] debt:[{", ".join(debt_parts)}] '
                              f'col=${float(pos.total_collateral_usd):,.0f} '
                              f'debt=${float(pos.total_debt_usd):,.0f}')

                if hf < Decimal('1.0'):
                    liquidatable += 1
                    logger.warning(f'[ColdStart] LIQUIDATABLE {user[:12]} HF={float(hf):.4f} | {pos_str}')
                    opp = await self._run(self.finder.find_best, pos, current_block)
                    if opp:
                        await self._fire_opportunity(opp, 'COLD_START')
                else:
                    at_risk += 1
                    logger.warning(f'[ColdStart] AT RISK {user[:12]} HF={float(hf):.4f} | {pos_str}')

            # Compound V3 evaluation
            elif proto == 'COMPOUND_V3':
                if user.lower() not in c3_liq_set:
                    # Not liquidatable → register and skip
                    self.registry.add_user(user, 'COMPOUND_V3', current_block)
                    healthy += 1
                    continue

                # Liquidatable → fetch full position for collateral details
                c3_pos = await self._run(self.compound.check_position, user)
                if c3_pos is None:
                    skipped_dust += 1
                    continue
                self.registry.add_user(user, 'COMPOUND_V3', current_block)
                cached += 1
                liquidatable += 1

                opp = await self._run(self._build_c3_opportunity, c3_pos, current_block)
                if opp:
                    logger.warning(
                        f'[ColdStart] C3 LIQUIDATABLE {user[:12]} '
                        f'borrow={float(c3_pos.borrow_usd):.2f} USDC '
                        f'col={opp.col_symbol} net≈${float(opp.net_profit_usd):.4f}'
                    )
                    self.stats['liquidations_attempted'] += 1
                    await self._fire_opportunity(opp, 'COLD_START_C3')
                else:
                    logger.info(
                        f'[ColdStart] C3 {user[:12]} liquidatable but not profitable '
                        f'(borrow={float(c3_pos.borrow_usd):.2f} USDC)'
                    )

        logger.info('=' * 65)
        logger.info(
            f'COLD START COMPLETE: total={total} cached={cached} '
            f'healthy={healthy} at_risk={at_risk} '
            f'liquidatable={liquidatable} skipped={skipped_dust}'
        )
        logger.info('=' * 65)
        self.registry.save()

    # ── Entry point ─────────────────────────────────────────────────

    def run(self):
        logger.info('=' * 65)
        logger.info('MULTI-PROTOCOL HYBRID BOT — Polygon')
        logger.info(f'  Contract  : {CONTRACT_ADDRESS or "NOT DEPLOYED"}')
        logger.info(f'  Min profit: ${self.min_profit_usd}')
        logger.info(f'  Execution : {"ENABLED" if self.execution_enabled else "MONITOR ONLY"}')
        logger.info(f'  Protocols : Aave V3 | Morpho Blue | Compound V3')
        cache_age = self.registry.cache_age_secs()
        n_cached  = len(self.registry.users)
        if n_cached > 0 and cache_age < CACHE_MAX_AGE_SECS:
            logger.info(f'  Startup   : WARM (cache {cache_age/60:.0f} min old, {n_cached} users)')
        elif n_cached > 0:
            logger.info(f'  Startup   : COLD (cache {cache_age/3600:.1f} h old, stale — rescanning {COLD_START_BLOCKS_STALE:,} blocks)')
        else:
            logger.info(f'  Startup   : COLD FIRST-RUN (scanning {COLD_START_BLOCKS_FULL:,} blocks, ~{int(COLD_START_BLOCKS_FULL*2.2/86400)} days)')
        logger.info('=' * 65)

        self._startup()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _main():
            # 1. Startup: warm restart if cache is fresh, else full cold start
            cache_age_now = self.registry.cache_age_secs()
            has_users     = len(self.registry.users) > 0

            if has_users and cache_age_now < CACHE_MAX_AGE_SECS:
                # Fresh cache — skip historical rescan, just refresh HFs + gap scan
                await self._warm_restart()
            elif has_users:
                # Stale cache — rescan a shorter window (stale positions may have changed)
                await self._cold_start_scan(COLD_START_BLOCKS_STALE)
            else:
                # First run — cast widest possible net
                await self._cold_start_scan(COLD_START_BLOCKS_FULL)

            # 2. Start background tasks
            asyncio.create_task(self._http_poll_loop())         # primary event source
            asyncio.create_task(self._price_refresh_loop())
            asyncio.create_task(self._new_user_scan_loop())
            asyncio.create_task(self._heartbeat_loop())
            asyncio.create_task(self._stats_loop())

            # 3. WSS listener (bonus — compensates if HTTP poll misses events)
            await self._listen()

        try:
            loop.run_until_complete(_main())
        except KeyboardInterrupt:
            logger.info('Shutting down...')
            self.registry.save()
        finally:
            loop.close()


# ═══════════════════════════════════════════════════════════════════════
# ENTRY POINT — outer restart loop with exponential backoff
# ═══════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    _delay = 10
    while True:
        try:
            HybridBot().run()
            break
        except KeyboardInterrupt:
            logger.info('Interrupted')
            break
        except Exception as e:
            logger.error(f'Fatal: {e}', exc_info=True)
            logger.warning(f'Restarting in {_delay}s...')
            time.sleep(_delay)
            _delay = min(_delay * 2, 120)
