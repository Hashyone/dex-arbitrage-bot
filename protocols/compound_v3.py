"""
Compound V3 (Comet) — Polygon monitor module.

Liquidation is executed via the MultiProtocolHybridBot contract (PROTOCOL_COMPOUND_V3 = 2),
which uses a Balancer V2 flash loan:
  1. Flash loan USDC (base token) from Balancer
  2. absorb(contract_address, [user])  — seizes user collateral into Comet reserves
  3. buyCollateral(colAsset, minCol, usdc, contract)  — buys at storeFront discount
  4. Swap collateral → USDC via DEX
  5. Repay Balancer flash loan, sweep profit to owner

No direct wallet absorb — everything goes through the flash loan contract.

Comet USDC market on Polygon: 0xF25212E676D1F7F89Cd72fFEe66158f541246445
Base token (bridged USDC):    0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
"""

import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from web3 import Web3

logger = logging.getLogger('HybridBot.CompoundV3')

# ── Market addresses ───────────────────────────────────────────────────
COMET_USDC_ADDR = '0xF25212E676D1F7F89Cd72fFEe66158f541246445'
COMET_BASE_TOKEN = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'   # bridged USDC

# Reverse-lookup: address.lower() → symbol (populated from on-chain getAssetInfo)
_KNOWN_SYMBOLS: dict[str, str] = {
    '0x7ceb23fd6bc0add59e62ac25578270cff1b9f619': 'WETH',
    '0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6': 'WBTC',
    '0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270': 'WPOL',
    '0x03b54a6e9a984069379fae1a4fc4dbae93b3bccd': 'wstETH',
    '0x53e0bca35ec356bd5dddfebbbd1fc0fd03fabad39': 'LINK',
}

# ── Comet ABI (view + write functions used by bot and contract) ─────────
COMET_ABI = [
    {'name': 'isLiquidatable',
     'inputs': [{'name': 'account', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'bool'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'isAbsorbPaused',
     'inputs': [],
     'outputs': [{'name': '', 'type': 'bool'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'isBuyPaused',
     'inputs': [],
     'outputs': [{'name': '', 'type': 'bool'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'baseToken',
     'inputs': [],
     'outputs': [{'name': '', 'type': 'address'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'borrowBalanceOf',
     'inputs': [{'name': 'account', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'collateralBalanceOf',
     'inputs': [
         {'name': 'account', 'type': 'address'},
         {'name': 'asset',   'type': 'address'},
     ],
     'outputs': [{'name': '', 'type': 'uint128'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'getAssetInfo',
     'inputs': [{'name': 'i', 'type': 'uint8'}],
     'outputs': [{
         'components': [
             {'name': 'offset',                    'type': 'uint8'},
             {'name': 'asset',                     'type': 'address'},
             {'name': 'priceFeed',                 'type': 'address'},
             {'name': 'scale',                     'type': 'uint64'},
             {'name': 'borrowCollateralFactor',    'type': 'uint64'},
             {'name': 'liquidateCollateralFactor', 'type': 'uint64'},
             {'name': 'liquidationFactor',         'type': 'uint64'},
             {'name': 'supplyCap',                 'type': 'uint128'},
         ],
         'name': '', 'type': 'tuple',
     }],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'numAssets',
     'inputs': [],
     'outputs': [{'name': '', 'type': 'uint8'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'quoteCollateral',
     'inputs': [
         {'name': 'asset',      'type': 'address'},
         {'name': 'baseAmount', 'type': 'uint256'},
     ],
     'outputs': [{'name': '', 'type': 'uint256'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'getReserves',
     'inputs': [],
     'outputs': [{'name': '', 'type': 'int256'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'getLiquidationMargin',
     'inputs': [{'name': 'account', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'int256'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'totalSupply',
     'inputs': [],
     'outputs': [{'name': '', 'type': 'uint256'}],
     'stateMutability': 'view', 'type': 'function'},
    {'name': 'totalBorrow',
     'inputs': [],
     'outputs': [{'name': '', 'type': 'uint256'}],
     'stateMutability': 'view', 'type': 'function'},
    # Write functions (called by the hybrid contract inside flash loan callback — NOT by bot directly)
    {'name': 'absorb',
     'inputs': [
         {'name': 'absorber',  'type': 'address'},
         {'name': 'accounts',  'type': 'address[]'},
     ],
     'outputs': [], 'stateMutability': 'nonpayable', 'type': 'function'},
    {'name': 'buyCollateral',
     'inputs': [
         {'name': 'asset',      'type': 'address'},
         {'name': 'minAmount',  'type': 'uint256'},
         {'name': 'baseAmount', 'type': 'uint256'},
         {'name': 'recipient',  'type': 'address'},
     ],
     'outputs': [], 'stateMutability': 'nonpayable', 'type': 'function'},
]

# ── Event topics — used for user discovery ─────────────────────────────
# Supply(address indexed from, address indexed dst, uint256 amount)
SUPPLY_TOPIC            = Web3.keccak(text='Supply(address,address,uint256)').hex()
# Withdraw(address indexed src, address indexed to, uint256 amount)
WITHDRAW_TOPIC          = Web3.keccak(text='Withdraw(address,address,uint256)').hex()
# SupplyCollateral(address indexed from, address indexed dst, address indexed asset, uint256 amount)
SUPPLY_COLLATERAL_TOPIC = Web3.keccak(text='SupplyCollateral(address,address,address,uint256)').hex()

ALL_COMET_TOPICS = [SUPPLY_TOPIC, WITHDRAW_TOPIC, SUPPLY_COLLATERAL_TOPIC]


@dataclass
class CometCollateralInfo:
    asset:                       str
    price_feed:                  str
    liquidate_collateral_factor: Decimal
    liquidation_factor:          Decimal   # storeFront discount factor
    scale:                       int       # e.g. 1e18 for WETH, 1e8 for WBTC
    decimals:                    int       # log10(scale)
    symbol:                      str = ''


@dataclass
class CompoundV3Position:
    user:               str
    market_address:     str
    base_token:         str
    borrow_balance:     int           # raw USDC units (6 decimals)
    borrow_usd:         Decimal
    collaterals:        dict = field(default_factory=dict)   # asset_cs -> raw_balance
    is_liquidatable:    bool = False
    absorb_paused:      bool = False
    buy_paused:         bool = False


class CompoundV3Monitor:
    """
    Monitors Compound V3 Comet USDC market on Polygon.
    Execution is routed through the MultiProtocolHybridBot contract
    using a Balancer flash loan — bot calls contract.executeLiquidation()
    with protocol=COMPOUND_V3, not Comet directly.
    """

    USDC_DECIMALS = 6

    def __init__(self, w3: Web3):
        self.w3          = w3
        self.market_addr = COMET_USDC_ADDR
        self.contract    = w3.eth.contract(
            address=Web3.to_checksum_address(self.market_addr),
            abi=COMET_ABI,
        )
        self.base_token:    str = COMET_BASE_TOKEN
        self.asset_infos:   dict[str, CometCollateralInfo] = {}
        self._absorb_paused: bool = False
        self._buy_paused:   bool = False

    def initialize(self):
        """Load market metadata: base token + all accepted collateral assets."""
        try:
            self.base_token     = self.contract.functions.baseToken().call()
            num_assets          = self.contract.functions.numAssets().call()
            self._absorb_paused = self.contract.functions.isAbsorbPaused().call()
            self._buy_paused    = self.contract.functions.isBuyPaused().call()

            self.asset_infos = {}
            for i in range(num_assets):
                info  = self.contract.functions.getAssetInfo(i).call()
                asset = info[1]    # address
                scale = int(info[3])
                # Compute decimals from scale (e.g. 1000000000000000000 → 18)
                try:
                    dec = round(math.log10(scale)) if scale > 0 else 18
                except Exception:
                    dec = 18
                symbol = _KNOWN_SYMBOLS.get(asset.lower(), asset[:8])
                self.asset_infos[asset.lower()] = CometCollateralInfo(
                    asset=asset,
                    price_feed=info[2],
                    liquidate_collateral_factor=Decimal(info[5]) / Decimal(10 ** 18),
                    liquidation_factor=Decimal(info[6]) / Decimal(10 ** 18),
                    scale=scale,
                    decimals=dec,
                    symbol=symbol,
                )

            logger.info(
                f'Compound V3 USDC initialized: {num_assets} collaterals, '
                f'absorb_paused={self._absorb_paused} buy_paused={self._buy_paused}'
            )
            for al, ci in self.asset_infos.items():
                logger.info(
                    f'  {ci.symbol:>8} {al[:12]}... '
                    f'liqCF={float(ci.liquidate_collateral_factor):.3f} '
                    f'liqFactor={float(ci.liquidation_factor):.3f} '
                    f'dec={ci.decimals}'
                )
        except Exception as e:
            logger.error(f'Compound V3 init failed: {e}', exc_info=True)

    def get_reserves(self) -> int:
        """Comet protocol reserves (positive = can pay absorbers, negative = insolvent)."""
        try:
            return int(self.contract.functions.getReserves().call())
        except Exception:
            return 0

    def check_position(self, user: str) -> Optional[CompoundV3Position]:
        """
        Read a user's Comet position.  Returns None if they have no borrow balance.
        Populates is_liquidatable and collaterals dict.
        """
        try:
            user_cs = Web3.to_checksum_address(user)
        except Exception:
            return None
        try:
            borrow_balance = self.contract.functions.borrowBalanceOf(user_cs).call()
            if borrow_balance == 0:
                return None

            is_liq = self.contract.functions.isLiquidatable(user_cs).call()

            collaterals: dict[str, int] = {}
            for asset_lower, ci in self.asset_infos.items():
                try:
                    bal = self.contract.functions.collateralBalanceOf(
                        user_cs, ci.asset
                    ).call()
                    if bal > 0:
                        collaterals[ci.asset] = int(bal)
                except Exception:
                    pass

            pos = CompoundV3Position(
                user=user,
                market_address=self.market_addr,
                base_token=self.base_token,
                borrow_balance=borrow_balance,
                borrow_usd=Decimal(borrow_balance) / Decimal(10 ** self.USDC_DECIMALS),
                collaterals=collaterals,
                is_liquidatable=is_liq,
                absorb_paused=self._absorb_paused,
                buy_paused=self._buy_paused,
            )
            if is_liq:
                logger.info(
                    f'[CompV3] Liquidatable: {user[:12]}... '
                    f'borrow={borrow_balance / 1e6:.2f} USDC '
                    f'collateral_assets={len(collaterals)}'
                )
            return pos
        except Exception as e:
            logger.debug(f'CompV3 check_position {user[:12]}: {e}')
            return None

    def quote_collateral(self, asset_addr: str, base_amount: int) -> int:
        """
        Call Comet.quoteCollateral(asset, baseAmount) → collateral units received.
        baseAmount = USDC (6-decimal) to spend via buyCollateral.
        Returns 0 on error.
        """
        try:
            return int(self.contract.functions.quoteCollateral(
                Web3.to_checksum_address(asset_addr), base_amount
            ).call())
        except Exception as e:
            logger.debug(f'quoteCollateral {asset_addr[:12]} {base_amount}: {e}')
            return 0

    def get_supply_events(self, from_block: int, to_block: int) -> list[str]:
        """Fetch Comet events to discover active borrowers (for cold start / user scan)."""
        users: set[str] = set()
        for topic in ALL_COMET_TOPICS:
            try:
                logs = self.w3.eth.get_logs({
                    'address':   Web3.to_checksum_address(self.market_addr),
                    'topics':    [topic],
                    'fromBlock': from_block,
                    'toBlock':   to_block,
                })
                for log in logs:
                    tpcs = log.get('topics', [])
                    if len(tpcs) >= 3:
                        raw  = tpcs[2]
                        addr = '0x' + (raw.hex() if isinstance(raw, bytes) else raw)[-40:]
                        try:
                            users.add(Web3.to_checksum_address(addr).lower())
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f'CompV3 event fetch topic={topic[:10]}: {e}')
        return list(users)
