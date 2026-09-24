"""
Morpho Blue — Polygon monitor module.

Watches Morpho Blue markets for liquidatable positions.
Uses the supplyShares-based health factor calculation.

Morpho Blue liquidation condition: LTV > LLTV (loan-to-value > liquidation threshold)
"""

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from web3 import Web3

logger = logging.getLogger('HybridBot.Morpho')

# Morpho Blue on Polygon
MORPHO_BLUE_ADDRESS = '0x9dC1Cf03C47513f64C3Ca6b226f4B2B9Da36e281'

MORPHO_BLUE_ABI = [
    # market(id) -> (totalSupplyAssets, totalSupplyShares, totalBorrowAssets, totalBorrowShares, lastUpdate, fee)
    {
        'name': 'market',
        'inputs': [{'name': 'id', 'type': 'bytes32'}],
        'outputs': [
            {'name': 'totalSupplyAssets', 'type': 'uint128'},
            {'name': 'totalSupplyShares', 'type': 'uint128'},
            {'name': 'totalBorrowAssets', 'type': 'uint128'},
            {'name': 'totalBorrowShares', 'type': 'uint128'},
            {'name': 'lastUpdate', 'type': 'uint128'},
            {'name': 'fee', 'type': 'uint128'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
    # position(id, user) -> (supplyShares, borrowShares, collateral)
    {
        'name': 'position',
        'inputs': [{'name': 'id', 'type': 'bytes32'}, {'name': 'user', 'type': 'address'}],
        'outputs': [
            {'name': 'supplyShares', 'type': 'uint256'},
            {'name': 'borrowShares', 'type': 'uint256'},
            {'name': 'collateral',   'type': 'uint128'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
    # idToMarketParams(id)
    {
        'name': 'idToMarketParams',
        'inputs': [{'name': 'id', 'type': 'bytes32'}],
        'outputs': [
            {'name': 'loanToken',       'type': 'address'},
            {'name': 'collateralToken', 'type': 'address'},
            {'name': 'oracle',          'type': 'address'},
            {'name': 'irm',             'type': 'address'},
            {'name': 'lltv',            'type': 'uint256'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
]

# Morpho Blue event topics
MORPHO_BORROW_TOPIC = Web3.keccak(
    text='Borrow(bytes32,address,address,address,uint256,uint256)'
).hex()
MORPHO_SUPPLY_COLLATERAL_TOPIC = Web3.keccak(
    text='SupplyCollateral(bytes32,address,address,uint256)'
).hex()

WAD = Decimal('1000000000000000000')  # 1e18


@dataclass
class MorphoMarket:
    id:               str   # bytes32 hex
    loan_token:       str
    collateral_token: str
    oracle:           str
    irm:              str
    lltv:             Decimal
    total_supply:     Decimal = Decimal('0')
    total_borrow:     Decimal = Decimal('0')


@dataclass
class MorphoPosition:
    user:            str
    market_id:       str
    supply_shares:   int
    borrow_shares:   int
    collateral:      int
    ltv_estimate:    Decimal = Decimal('0')
    is_liquidatable: bool    = False


class MorphoMonitor:
    """
    Monitors Morpho Blue markets on Polygon for liquidation opportunities.
    """

    def __init__(self, w3: Web3):
        self.w3       = w3
        self.contract = w3.eth.contract(
            address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
            abi=MORPHO_BLUE_ABI,
        )
        self.markets:   dict[str, MorphoMarket]   = {}   # id -> market
        self.positions: dict[str, MorphoPosition] = {}   # user:market_id -> position
        self._last_scan_block = 0

    def compute_market_id(self, loan_token: str, collateral_token: str,
                          oracle: str, irm: str, lltv: int) -> str:
        """Compute Morpho market ID from params (keccak256 of ABI-encoded params)."""
        from eth_abi import encode
        encoded = encode(
            ['address', 'address', 'address', 'address', 'uint256'],
            [
                Web3.to_checksum_address(loan_token),
                Web3.to_checksum_address(collateral_token),
                Web3.to_checksum_address(oracle),
                Web3.to_checksum_address(irm),
                lltv,
            ]
        )
        return '0x' + Web3.keccak(encoded).hex()

    def load_markets(self, market_params_list: list[dict]):
        """
        Register markets to monitor.
        market_params_list = [
          {'loan_token': '0x...', 'collateral_token': '0x...', 'oracle': '0x...', 'irm': '0x...', 'lltv': int},
          ...
        ]
        """
        for mp in market_params_list:
            market_id = self.compute_market_id(
                mp['loan_token'], mp['collateral_token'],
                mp['oracle'], mp['irm'], mp['lltv']
            )
            self.markets[market_id] = MorphoMarket(
                id=market_id,
                loan_token=mp['loan_token'],
                collateral_token=mp['collateral_token'],
                oracle=mp['oracle'],
                irm=mp['irm'],
                lltv=Decimal(mp['lltv']) / WAD,
            )
            logger.info(
                f'Registered Morpho market {market_id[:16]}... '
                f'{mp.get("loan_token","")[:8]}/{mp.get("collateral_token","")[:8]}'
            )

    def fetch_market_state(self, market_id: str) -> Optional[MorphoMarket]:
        """Refresh on-chain market totals."""
        try:
            mid_bytes = bytes.fromhex(market_id.removeprefix('0x'))
            result = self.contract.functions.market(mid_bytes).call()
            m = self.markets.get(market_id)
            if m:
                m.total_supply = Decimal(result[0])
                m.total_borrow = Decimal(result[2])
            return m
        except Exception as e:
            logger.warning(f'Morpho market fetch failed {market_id[:16]}: {e}')
            return None

    def check_position(self, market_id: str, user: str) -> Optional[MorphoPosition]:
        """
        Check one user's position in one market.
        Returns a MorphoPosition with is_liquidatable set if unhealthy.
        """
        try:
            mid_bytes = bytes.fromhex(market_id.removeprefix('0x'))
            user_cs   = Web3.to_checksum_address(user)
            pos       = self.contract.functions.position(mid_bytes, user_cs).call()

            supply_shares, borrow_shares, collateral = pos[0], pos[1], pos[2]

            if borrow_shares == 0:
                return None  # No debt — no liquidation risk

            market = self.markets.get(market_id)
            if not market or market.total_supply == 0 or market.total_borrow == 0:
                return None

            # Estimate LTV: borrowAssets / collateral (rough, no oracle)
            # Full calculation requires oracle price — done by bot's opportunity finder
            mp = MorphoPosition(
                user=user,
                market_id=market_id,
                supply_shares=supply_shares,
                borrow_shares=borrow_shares,
                collateral=collateral,
            )

            # Rough borrow assets
            borrow_assets = (borrow_shares * int(market.total_borrow)) // (int(market.total_supply) or 1)

            if collateral > 0:
                mp.ltv_estimate = Decimal(borrow_assets) / Decimal(collateral)
                mp.is_liquidatable = mp.ltv_estimate > market.lltv

            key = f'{user.lower()}:{market_id}'
            self.positions[key] = mp
            return mp

        except Exception as e:
            logger.warning(f'Morpho position check failed {user[:10]}/{market_id[:16]}: {e}')
            return None

    def get_borrow_events(self, from_block: int, to_block: int) -> list[dict]:
        """
        Fetch Morpho Borrow events to discover borrowers.
        Returns list of {'user': addr, 'market_id': hex, 'block': int}
        """
        results = []
        try:
            logs = self.w3.eth.get_logs({
                'address':   Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
                'topics':    [MORPHO_BORROW_TOPIC],
                'fromBlock': from_block,
                'toBlock':   to_block,
            })
            for log in logs:
                if len(log['topics']) >= 3:
                    market_id = log['topics'][1].hex()
                    onbehalf  = '0x' + log['topics'][2].hex()[-40:]
                    results.append({
                        'user':      Web3.to_checksum_address(onbehalf),
                        'market_id': '0x' + market_id,
                        'block':     log['blockNumber'],
                    })
        except Exception as e:
            logger.warning(f'Morpho borrow event fetch failed: {e}')
        return results

    def scan_liquidatable(self, users_by_market: dict[str, list[str]]) -> list[MorphoPosition]:
        """
        Check a batch of users across markets for liquidatability.
        users_by_market = {market_id: [user_addr, ...]}
        """
        liquidatable = []
        for market_id, users in users_by_market.items():
            self.fetch_market_state(market_id)
            for user in users:
                pos = self.check_position(market_id, user)
                if pos and pos.is_liquidatable:
                    liquidatable.append(pos)
                    logger.info(
                        f'[Morpho] Liquidatable: {user[:12]} '
                        f'LTV={float(pos.ltv_estimate):.4f} '
                        f'LLTV={float(self.markets[market_id].lltv):.4f}'
                    )
        return liquidatable
