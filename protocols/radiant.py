"""
Radiant Capital — Polygon monitor module.

Radiant Capital V2 is an Aave V2 fork (with LayerZero cross-chain).
The liquidation interface is identical to Aave V2.
Events are identical: Borrow, Repay, Deposit, LiquidationCall.

Radiant Lending Pool Proxy (Polygon): 0x2032b9A8e9F7e76768CA9271003d3e43E1616B1F
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from web3 import Web3

logger = logging.getLogger('HybridBot.Radiant')

# ─── Radiant Contract Addresses (Polygon) ─────────────────────────────
RADIANT_POOL          = '0x2032b9A8e9F7e76768CA9271003d3e43E1616B1F'
RADIANT_DATA_PROVIDER = '0x8F86403A4DE0BB5791fa46B8e795C547942fE4Cf'
RADIANT_ORACLE        = '0x2C2879B5a6B3B7F5b7F38c5e4cE7f0f83B9F3D6a'  # may vary

# Aave V2-style ABI subset (Radiant is a fork)
POOL_ABI = [
    {
        'name': 'getUserAccountData',
        'inputs': [{'name': 'user', 'type': 'address'}],
        'outputs': [
            {'name': 'totalCollateralETH',          'type': 'uint256'},
            {'name': 'totalDebtETH',                'type': 'uint256'},
            {'name': 'availableBorrowsETH',         'type': 'uint256'},
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
    {
        'name': 'paused',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'bool'}],
        'stateMutability': 'view', 'type': 'function',
    },
]

DATA_PROVIDER_ABI = [
    {
        'name': 'getUserReserveData',
        'inputs': [
            {'name': 'asset',   'type': 'address'},
            {'name': 'user',    'type': 'address'},
        ],
        'outputs': [
            {'name': 'currentATokenBalance',         'type': 'uint256'},
            {'name': 'currentStableDebt',            'type': 'uint256'},
            {'name': 'currentVariableDebt',          'type': 'uint256'},
            {'name': 'principalStableDebt',          'type': 'uint256'},
            {'name': 'scaledVariableDebt',           'type': 'uint256'},
            {'name': 'stableBorrowRate',             'type': 'uint256'},
            {'name': 'liquidityRate',                'type': 'uint256'},
            {'name': 'stableRateLastUpdated',        'type': 'uint40'},
            {'name': 'usageAsCollateralEnabled',     'type': 'bool'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'getReserveConfigurationData',
        'inputs': [{'name': 'asset', 'type': 'address'}],
        'outputs': [
            {'name': 'decimals',               'type': 'uint256'},
            {'name': 'ltv',                    'type': 'uint256'},
            {'name': 'liquidationThreshold',   'type': 'uint256'},
            {'name': 'liquidationBonus',       'type': 'uint256'},
            {'name': 'reserveFactor',          'type': 'uint256'},
            {'name': 'usageAsCollateralEnabled','type': 'bool'},
            {'name': 'borrowingEnabled',       'type': 'bool'},
            {'name': 'stableBorrowRateEnabled','type': 'bool'},
            {'name': 'isActive',               'type': 'bool'},
            {'name': 'isFrozen',               'type': 'bool'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
]

# Events — identical to Aave V2 signatures
BORROW_TOPIC = Web3.keccak(
    text='Borrow(address,address,address,uint256,uint256,uint256,uint16)'
).hex()
REPAY_TOPIC = Web3.keccak(
    text='Repay(address,address,address,uint256)'
).hex()
DEPOSIT_TOPIC = Web3.keccak(
    text='Deposit(address,address,address,uint256,uint16)'
).hex()
LIQ_TOPIC = Web3.keccak(
    text='LiquidationCall(address,address,address,uint256,uint256,address,bool)'
).hex()

WAD = Decimal('1000000000000000000')


@dataclass
class RadiantReserve:
    symbol:            str
    address:           str
    decimals:          int
    liq_bonus:         Decimal   # e.g. 1.05
    liq_threshold:     Decimal   # e.g. 0.80
    is_active:         bool
    is_frozen:         bool


@dataclass
class RadiantPosition:
    user:            str
    health_factor:   Decimal
    total_col_eth:   Decimal
    total_debt_eth:  Decimal
    collateral:      list = field(default_factory=list)
    debt:            list = field(default_factory=list)
    is_liquidatable: bool = False


class RadiantMonitor:
    """
    Monitors Radiant Capital positions for liquidation opportunities.
    Uses Aave V2-compatible interfaces.
    """

    def __init__(self, w3: Web3):
        self.w3   = w3
        self.pool = w3.eth.contract(
            address=Web3.to_checksum_address(RADIANT_POOL),
            abi=POOL_ABI,
        )
        self.data_provider = w3.eth.contract(
            address=Web3.to_checksum_address(RADIANT_DATA_PROVIDER),
            abi=DATA_PROVIDER_ABI,
        )
        self.reserves:  dict[str, RadiantReserve]   = {}
        self.positions: dict[str, RadiantPosition]  = {}
        self._initialized = False

    def initialize(self):
        """Load all Radiant reserves."""
        try:
            is_paused = self.pool.functions.paused().call()
            if is_paused:
                logger.warning('Radiant pool is PAUSED — liquidations will revert')

            reserves_list = self.pool.functions.getReservesList().call()
            logger.info(f'Radiant: {len(reserves_list)} reserves found')

            for asset in reserves_list:
                try:
                    cfg = self.data_provider.functions.getReserveConfigurationData(
                        Web3.to_checksum_address(asset)
                    ).call()
                    decimals, _, liq_threshold, liq_bonus, _, _, _, _, is_active, is_frozen = cfg

                    if not is_active:
                        continue

                    self.reserves[asset.lower()] = RadiantReserve(
                        symbol='',  # would need ERC20 symbol call
                        address=asset,
                        decimals=decimals,
                        liq_bonus=Decimal(liq_bonus) / Decimal('10000'),
                        liq_threshold=Decimal(liq_threshold) / Decimal('10000'),
                        is_active=is_active,
                        is_frozen=is_frozen,
                    )
                except Exception as e:
                    logger.debug(f'Radiant reserve load failed {asset}: {e}')

            self._initialized = True
            logger.info(f'Radiant monitor initialized: {len(self.reserves)} active reserves')
        except Exception as e:
            logger.error(f'Radiant init failed: {e}')

    def check_user_health(self, user: str) -> Optional[RadiantPosition]:
        """Fetch user account data from Radiant."""
        try:
            user_cs = Web3.to_checksum_address(user)
            data    = self.pool.functions.getUserAccountData(user_cs).call()

            total_col, total_debt, _, _, _, hf = data

            if total_debt == 0:
                return None

            hf_dec = Decimal(hf) / WAD
            pos    = RadiantPosition(
                user=user,
                health_factor=hf_dec,
                total_col_eth=Decimal(total_col) / WAD,
                total_debt_eth=Decimal(total_debt) / WAD,
                is_liquidatable=(hf < int(WAD)),
            )
            self.positions[user.lower()] = pos

            if pos.is_liquidatable:
                logger.info(
                    f'[Radiant] Liquidatable: {user[:12]} '
                    f'HF={float(hf_dec):.4f} '
                    f'debt={float(pos.total_debt_eth):.4f} ETH'
                )
            return pos

        except Exception as e:
            logger.warning(f'Radiant health check {user[:10]}: {e}')
            return None

    def get_user_reserve_data(self, user: str, asset: str) -> Optional[dict]:
        """Get detailed per-reserve position for a user."""
        try:
            result = self.data_provider.functions.getUserReserveData(
                Web3.to_checksum_address(asset),
                Web3.to_checksum_address(user),
            ).call()
            return {
                'a_token_balance': result[0],
                'stable_debt':     result[1],
                'variable_debt':   result[2],
                'use_as_collateral': result[8],
            }
        except Exception as e:
            logger.warning(f'Radiant getUserReserveData {user[:10]}/{asset[:10]}: {e}')
            return None

    def get_borrow_events(self, from_block: int, to_block: int) -> list[str]:
        """Fetch Borrow events to discover active borrowers."""
        users = set()
        try:
            logs = self.w3.eth.get_logs({
                'address':   Web3.to_checksum_address(RADIANT_POOL),
                'topics':    [BORROW_TOPIC],
                'fromBlock': from_block,
                'toBlock':   to_block,
            })
            for log in logs:
                if len(log['topics']) >= 3:
                    # topic[2] = onBehalfOf
                    user_addr = '0x' + log['topics'][2].hex()[-40:]
                    users.add(Web3.to_checksum_address(user_addr))
        except Exception as e:
            logger.warning(f'Radiant borrow events failed: {e}')
        return list(users)

    def scan_liquidatable(self, users: list[str]) -> list[RadiantPosition]:
        """Check a batch of users, return those that are liquidatable."""
        results = []
        for user in users:
            pos = self.check_user_health(user)
            if pos and pos.is_liquidatable:
                results.append(pos)
        return results
