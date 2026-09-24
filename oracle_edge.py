"""Research-only Polygon oracle transition monitor.

This module observes verified Aave V3 and Compound V3 Polygon price sources,
compares them with the bot's DEX price map, and records transitions.  It never
returns an executable opportunity and never submits a transaction.  An
oracle update is only a research signal until an exact atomic route survives
quoting and live simulation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from web3 import Web3

logger = logging.getLogger("arb_bot.oracle_edge")

AAVE_ORACLE = Web3.to_checksum_address("0xb023e699F5a33916Ea823A16485e259257cA8Bd1")
COMET_USDC = Web3.to_checksum_address("0xF25212E676D1F7F89Cd72fFEe66158f541246445")

AAVE_ORACLE_ABI = [
    {
        "name": "getAssetsPrices",
        "inputs": [{"name": "assets", "type": "address[]"}],
        "outputs": [{"name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    }
]
COMET_ABI = [
    {
        "name": "numAssets",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "getAssetInfo",
        "inputs": [{"name": "i", "type": "uint8"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "offset", "type": "uint8"},
                    {"name": "asset", "type": "address"},
                    {"name": "priceFeed", "type": "address"},
                    {"name": "scale", "type": "uint64"},
                    {"name": "borrowCollateralFactor", "type": "uint64"},
                    {"name": "liquidateCollateralFactor", "type": "uint64"},
                    {"name": "liquidationFactor", "type": "uint64"},
                    {"name": "supplyCap", "type": "uint128"},
                ],
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
]
AGGREGATOR_ABI = [
    {
        "name": "decimals",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "latestRoundData",
        "inputs": [],
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class OracleObservation:
    oracle_address: str
    oracle_type: str
    asset: str
    previous_value: Optional[float]
    new_value: float
    update_block: int
    update_timestamp: int
    deviation_bps: Optional[float]
    protocol_consumer: str
    external_market_price: Optional[float]
    seconds_since_update: Optional[float]
    research_only: bool = True
    atomic_validation: str = "not_attempted"

    def as_dict(self) -> Dict:
        return asdict(self)


def _deviation_bps(oracle_price: float, market_price: Optional[float]):
    if not market_price or oracle_price <= 0:
        return None
    return (market_price / oracle_price - 1.0) * 10_000.0


class OracleEdgeMonitor:
    """Rate-limited state observer with an in-memory previous-value ledger."""

    def __init__(
        self,
        assets: Dict[str, str],
        *,
        poll_interval_s: float = 60.0,
        min_transition_bps: float = 1.0,
    ):
        self.assets = {
            symbol: Web3.to_checksum_address(address)
            for symbol, address in assets.items()
        }
        self.poll_interval_s = max(1.0, poll_interval_s)
        self.min_transition_bps = abs(min_transition_bps)
        self._last_poll = 0.0
        self._values: Dict[str, float] = {}
        self._last_update: Dict[str, int] = {}

    def _observe(
        self,
        *,
        key: str,
        oracle_address: str,
        oracle_type: str,
        asset: str,
        value: float,
        update_block: int,
        update_timestamp: int,
        consumer: str,
        market_price: Optional[float],
        now: float,
    ) -> Optional[OracleObservation]:
        previous = self._values.get(key)
        previous_update = self._last_update.get(key)
        self._values[key] = value
        self._last_update[key] = update_block
        changed = previous is None or abs(value - previous) > max(abs(value), 1.0) * 1e-12
        if not changed and previous_update == update_block:
            return None
        deviation = _deviation_bps(value, market_price)
        transition_bps = (
            None if previous in (None, 0) else (value / previous - 1.0) * 10_000.0
        )
        if previous is not None and abs(transition_bps or 0.0) < self.min_transition_bps:
            return None
        return OracleObservation(
            oracle_address=oracle_address,
            oracle_type=oracle_type,
            asset=asset,
            previous_value=previous,
            new_value=value,
            update_block=update_block,
            update_timestamp=update_timestamp,
            deviation_bps=deviation,
            protocol_consumer=consumer,
            external_market_price=market_price,
            seconds_since_update=max(0.0, now - update_timestamp)
            if update_timestamp
            else None,
        )

    def observe(
        self,
        w3: Web3,
        *,
        block_number: int,
        block_timestamp: Optional[int] = None,
        dex_prices: Optional[Dict[str, float]] = None,
        force: bool = False,
    ) -> List[Dict]:
        now = time.time()
        if not force and now - self._last_poll < self.poll_interval_s:
            return []
        self._last_poll = now
        dex_prices = dex_prices or {}
        block_timestamp = block_timestamp or int(now)
        observations: List[Dict] = []

        try:
            oracle = w3.eth.contract(address=AAVE_ORACLE, abi=AAVE_ORACLE_ABI)
            assets = list(self.assets.values())
            prices = oracle.functions.getAssetsPrices(assets).call()
            for (symbol, address), raw_price in zip(self.assets.items(), prices):
                value = float(raw_price) / 1e8
                observation = self._observe(
                    key=f"aave:{address.lower()}",
                    oracle_address=AAVE_ORACLE,
                    oracle_type="AAVE_V3_PRICE_ORACLE",
                    asset=symbol,
                    value=value,
                    update_block=block_number,
                    update_timestamp=block_timestamp,
                    consumer="Aave V3 Pool",
                    market_price=dex_prices.get(symbol),
                    now=now,
                )
                if observation:
                    observations.append(observation.as_dict())
        except Exception as exc:
            logger.warning("Aave oracle observation unavailable: %s", str(exc)[:160])

        try:
            comet = w3.eth.contract(address=COMET_USDC, abi=COMET_ABI)
            num_assets = min(int(comet.functions.numAssets().call()), 32)
            for index in range(num_assets):
                info = comet.functions.getAssetInfo(index).call()
                asset = Web3.to_checksum_address(info[1])
                feed = Web3.to_checksum_address(info[2])
                symbol = next(
                    (name for name, address in self.assets.items()
                     if address.lower() == asset.lower()),
                    asset,
                )
                aggregator = w3.eth.contract(address=feed, abi=AGGREGATOR_ABI)
                decimals = int(aggregator.functions.decimals().call())
                round_data = aggregator.functions.latestRoundData().call()
                answer = int(round_data[1])
                updated_at = int(round_data[3])
                if answer <= 0:
                    continue
                value = answer / 10 ** decimals
                observation = self._observe(
                    key=f"compound:{feed.lower()}",
                    oracle_address=feed,
                    oracle_type="COMPOUND_V3_PRICE_FEED",
                    asset=symbol,
                    value=value,
                    update_block=block_number,
                    update_timestamp=updated_at,
                    consumer=COMET_USDC,
                    market_price=dex_prices.get(symbol),
                    now=now,
                )
                if observation:
                    observations.append(observation.as_dict())
        except Exception as exc:
            logger.warning("Compound oracle observation unavailable: %s", str(exc)[:160])

        return observations
