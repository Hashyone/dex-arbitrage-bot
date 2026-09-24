"""
Cross-Protocol Arbitrage Scanner
=================================
Monitors lending rates across Aave V3 and Compound V3 on Polygon.
Radiant excluded — Polygon deployment is inactive.

Rate conversion (confirmed correct from raw chain data, 2026-05-05):
  - Aave V3: rates are stored as ANNUALISED values in RAY units (1e27).
    APY = rate_ray / 1e27  — NO seconds-per-year multiplication.
    Verified: USDC rd[2] = 26_624_256_446... / 1e27 = 2.66% APY ✓
  - Compound V3: getSupplyRate/getBorrowRate return per-second rates
    scaled to 1e18 (uint64).
    APY = (rate / 1e18) × 31_536_000
    Verified: USDC supply_rate = 853_313_579 / 1e18 × 31_536_000 = 2.69% APY ✓
  - Token amounts always normalised using each token's own decimals()
    value — never hardcoded. USDC=6, WBTC=8, WETH=18 etc.
  - Oracle prices always 8 decimals (divide by 1e8).
  - Sanity ceiling: APY > 50% is discarded as a data error.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from web3 import Web3

logger = logging.getLogger('HybridBot.ArbScanner')

# ── Scale constants ────────────────────────────────────────────────────
RAY              = Decimal('1000000000000000000000000000')  # 1e27 — Aave RAY
COMPOUND_SCALE   = Decimal('1000000000000000000')            # 1e18 — Compound per-sec scale
SECONDS_PER_YEAR = Decimal('31536000')

# Sanity ceiling — any APY above this is a data/ABI error, not a real opportunity
MAX_SANE_APY = Decimal('0.50')   # 50%

# ── Protocol addresses (Polygon mainnet) ──────────────────────────────
AAVE_V3_POOL       = '0x794a61358D6845594F94dc1DB02A252b5b4814aD'
AAVE_DATA_PROVIDER = '0x69FA688f1Dc47d4B5d8029D5a35FB7a548310654'
AAVE_ORACLE        = '0xb023e699F5a33916Ea823A16485e259257cA8Bd1'

# ── ABIs ──────────────────────────────────────────────────────────────

# Aave V3 Pool.getReserveData — 15-field tuple, Polygon V3 deployment
# Field layout confirmed against Aave V3 source + raw chain logs:
#   [0]  configuration              uint256
#   [1]  liquidityIndex             uint128
#   [2]  currentLiquidityRate       uint128  ← SUPPLY rate (annualised RAY)
#   [3]  variableBorrowIndex        uint128
#   [4]  currentVariableBorrowRate  uint128  ← BORROW rate (annualised RAY)
#   [5]  currentStableBorrowRate    uint128
#   [6]  lastUpdateTimestamp        uint40
#   [7]  id                         uint16
#   [8]  aTokenAddress              address
#   [9]  stableDebtTokenAddress     address
#   [10] variableDebtTokenAddress   address
#   [11] interestRateStrategyAddr   address
#   [12] accruedToTreasury          uint128
#   [13] unbacked                   uint128
#   [14] isolationModeTotalDebt     uint128
AAVE_V3_RESERVE_DATA_ABI = [
    {
        'name': 'getReserveData',
        'inputs': [{'name': 'asset', 'type': 'address'}],
        'outputs': [
            {'name': 'configuration',              'type': 'uint256'},
            {'name': 'liquidityIndex',             'type': 'uint128'},
            {'name': 'currentLiquidityRate',       'type': 'uint128'},
            {'name': 'variableBorrowIndex',        'type': 'uint128'},
            {'name': 'currentVariableBorrowRate',  'type': 'uint128'},
            {'name': 'currentStableBorrowRate',    'type': 'uint128'},
            {'name': 'lastUpdateTimestamp',        'type': 'uint40'},
            {'name': 'id',                         'type': 'uint16'},
            {'name': 'aTokenAddress',              'type': 'address'},
            {'name': 'stableDebtTokenAddress',     'type': 'address'},
            {'name': 'variableDebtTokenAddress',   'type': 'address'},
            {'name': 'interestRateStrategyAddress','type': 'address'},
            {'name': 'accruedToTreasury',          'type': 'uint128'},
            {'name': 'unbacked',                   'type': 'uint128'},
            {'name': 'isolationModeTotalDebt',     'type': 'uint128'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
]

# Aave PoolDataProvider.getReserveData — for TVL figures
# Field layout confirmed:
#   [2] totalAToken         uint256  ← total supply (raw token units)
#   [3] totalStableDebt     uint256
#   [4] totalVariableDebt   uint256  ← total borrow (raw token units)
POOL_DATA_PROVIDER_ABI = [
    {
        'name': 'getReserveData',
        'inputs': [{'name': 'asset', 'type': 'address'}],
        'outputs': [
            {'name': 'unbacked',                  'type': 'uint256'},
            {'name': 'accruedToTreasuryScaled',   'type': 'uint256'},
            {'name': 'totalAToken',               'type': 'uint256'},
            {'name': 'totalStableDebt',           'type': 'uint256'},
            {'name': 'totalVariableDebt',         'type': 'uint256'},
            {'name': 'liquidityRate',             'type': 'uint256'},
            {'name': 'variableBorrowRate',        'type': 'uint256'},
            {'name': 'stableBorrowRate',          'type': 'uint256'},
            {'name': 'averageStableBorrowRate',   'type': 'uint256'},
            {'name': 'liquidityIndex',            'type': 'uint256'},
            {'name': 'variableBorrowIndex',       'type': 'uint256'},
            {'name': 'lastUpdateTimestamp',       'type': 'uint40'},
        ],
        'stateMutability': 'view', 'type': 'function',
    },
]

# Compound V3 Comet — rate and TVL functions
COMET_RATE_ABI = [
    {
        'name': 'getSupplyRate',
        'inputs': [{'name': 'utilization', 'type': 'uint256'}],
        'outputs': [{'name': '', 'type': 'uint64'}],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'getBorrowRate',
        'inputs': [{'name': 'utilization', 'type': 'uint256'}],
        'outputs': [{'name': '', 'type': 'uint64'}],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'getUtilization',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'uint256'}],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'baseToken',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'address'}],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'totalSupply',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'uint256'}],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'totalBorrow',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'uint256'}],
        'stateMutability': 'view', 'type': 'function',
    },
]

ORACLE_ABI = [
    {
        'name': 'getAssetPrice',
        'inputs': [{'name': 'asset', 'type': 'address'}],
        'outputs': [{'name': '', 'type': 'uint256'}],
        'stateMutability': 'view', 'type': 'function',
    },
]

ERC20_META_ABI = [
    {
        'name': 'decimals',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'uint8'}],
        'stateMutability': 'view', 'type': 'function',
    },
    {
        'name': 'symbol',
        'inputs': [],
        'outputs': [{'name': '', 'type': 'string'}],
        'stateMutability': 'view', 'type': 'function',
    },
]


# ══════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class ProtocolRate:
    """Interest rates for one token on one protocol — APYs as plain fractions."""
    protocol:         str
    token:            str          # lowercase address
    token_symbol:     str          = ''
    token_decimals:   int          = 18
    supply_apy:       Decimal      = Decimal('0')   # e.g. 0.0266 = 2.66%
    borrow_apy:       Decimal      = Decimal('0')
    total_supply_usd: Decimal      = Decimal('0')
    total_borrow_usd: Decimal      = Decimal('0')
    timestamp:        float        = field(default_factory=time.time)


@dataclass
class RateSpreadOpportunity:
    """Cross-protocol rate arb opportunity."""
    token:                      str
    token_symbol:               str
    supply_protocol:            str
    borrow_protocol:            str
    supply_apy:                 Decimal
    borrow_apy:                 Decimal
    spread_apy:                 Decimal
    max_flash_size_usd:         Decimal
    estimated_daily_profit_usd: Decimal


@dataclass
class LiquidationDiscountOpportunity:
    """Ranked liquidation opportunity across protocols."""
    protocol:         str
    user:             str
    collateral_asset: str
    debt_asset:       str
    collateral_usd:   Decimal
    debt_usd:         Decimal
    liq_bonus_pct:    Decimal
    gross_profit_usd: Decimal
    gas_cost_usd:     Decimal
    net_profit_usd:   Decimal
    priority_score:   Decimal


# ══════════════════════════════════════════════════════════════════════
# ARB SCANNER
# ══════════════════════════════════════════════════════════════════════

class ArbScanner:
    """
    Scans Aave V3 and Compound V3 for cross-protocol rate spreads.
    All decimal handling is explicit and verified against raw chain data.
    """

    def __init__(self, w3: Web3):
        self.w3       = w3
        self._aave_v3 = w3.eth.contract(
            address=Web3.to_checksum_address(AAVE_V3_POOL),
            abi=AAVE_V3_RESERVE_DATA_ABI,
        )
        self._aave_dp = w3.eth.contract(
            address=Web3.to_checksum_address(AAVE_DATA_PROVIDER),
            abi=POOL_DATA_PROVIDER_ABI,
        )
        self._oracle  = w3.eth.contract(
            address=Web3.to_checksum_address(AAVE_ORACLE),
            abi=ORACLE_ABI,
        )

        # Caches — avoid repeated RPC calls for static token metadata
        self._decimals_cache: dict[str, int] = {}
        self._symbol_cache:   dict[str, str] = {}

        self._scan_interval: int  = 60     # seconds between scans
        self._debug_raw:     bool = True   # raw field logging on first scan only

    # ── Token metadata ─────────────────────────────────────────────────

    def _get_decimals(self, addr: str) -> int:
        """
        Fetch and cache token decimals.
        Never assumed — USDC=6, WBTC=8, WETH=18, WPOL=18 etc.
        """
        al = addr.lower()
        if al in self._decimals_cache:
            return self._decimals_cache[al]
        try:
            c = self.w3.eth.contract(
                address=Web3.to_checksum_address(addr), abi=ERC20_META_ABI
            )
            d = int(c.functions.decimals().call())
            self._decimals_cache[al] = d
            logger.debug(f'Cached decimals {addr[:10]}... = {d}')
            return d
        except Exception as e:
            logger.warning(f'decimals() failed for {addr[:10]}: {e} — defaulting to 18')
            self._decimals_cache[al] = 18
            return 18

    def _get_symbol(self, addr: str) -> str:
        al = addr.lower()
        if al in self._symbol_cache:
            return self._symbol_cache[al]
        try:
            c = self.w3.eth.contract(
                address=Web3.to_checksum_address(addr), abi=ERC20_META_ABI
            )
            s = c.functions.symbol().call()
            self._symbol_cache[al] = s
            return s
        except Exception:
            s = addr[:8]
            self._symbol_cache[al] = s
            return s

    def _token_price_usd(self, addr: str) -> Decimal:
        """Aave oracle always returns price with 8 decimals."""
        try:
            raw   = self._oracle.functions.getAssetPrice(
                Web3.to_checksum_address(addr)
            ).call()
            price = Decimal(raw) / Decimal('1e8')
            logger.debug(
                f'Oracle {self._get_symbol(addr)}: raw={raw} '
                f'→ ${float(price):.6f}'
            )
            return price
        except Exception as e:
            logger.warning(f'Oracle price failed {addr[:10]}: {e}')
            return Decimal('0')

    def _raw_to_human(self, raw_amount: int, decimals: int) -> Decimal:
        """
        Convert raw token amount to human-readable units using token's decimals.
        USDC: 1_000_000 → 1.0 (6 dec)
        WETH: 1_000_000_000_000_000_000 → 1.0 (18 dec)
        """
        return Decimal(raw_amount) / Decimal(10 ** decimals)

    # ── Rate conversion ────────────────────────────────────────────────

    @staticmethod
    def _ray_to_apy(rate_ray: int, label: str = '') -> Decimal:
        """
        Aave V3 stores ANNUALISED rates in RAY units (1e27).
        APY = rate_ray / 1e27   — NO per-second multiplication needed.

        Verified from raw chain data (2026-05-05):
          USDC currentLiquidityRate = 26_624_256_446_143_008_627_239_582
          26_624_256_446... / 1e27 = 0.02662 = 2.66% APY ✓
        """
        if rate_ray <= 0:
            return Decimal('0')
        apy = Decimal(rate_ray) / RAY
        logger.debug(f'  _ray_to_apy {label}: raw={rate_ray} → APY={float(apy)*100:.4f}%')
        return apy

    @staticmethod
    def _compound_rate_to_apy(rate_raw: int, label: str = '') -> Decimal:
        """
        Compound V3 stores per-second rates scaled to 1e18 (uint64).
        APY = (rate / 1e18) × SECONDS_PER_YEAR

        Verified from raw chain data (2026-05-05):
          USDC supply_rate = 853_313_579
          853_313_579 / 1e18 × 31_536_000 = 0.02691 = 2.69% APY ✓
        """
        if rate_raw <= 0:
            return Decimal('0')
        rate_per_sec = Decimal(rate_raw) / COMPOUND_SCALE
        apy          = rate_per_sec * SECONDS_PER_YEAR
        logger.debug(
            f'  _compound_rate_to_apy {label}: raw={rate_raw} '
            f'per_sec={float(rate_per_sec):.12f} '
            f'APY={float(apy)*100:.4f}%'
        )
        return apy

    def _sanity_check_apy(
        self, apy: Decimal, label: str
    ) -> Optional[Decimal]:
        """
        Reject any APY above MAX_SANE_APY (50%).
        Triggered APY almost always means wrong field index or scale constant.
        Returns apy if sane, None if not.
        """
        if apy > MAX_SANE_APY:
            logger.error(
                f'SANITY FAIL: {label} APY={float(apy)*100:.2f}% exceeds '
                f'{float(MAX_SANE_APY)*100:.0f}% ceiling — discarding. '
                f'Check field indices or scale constant.'
            )
            return None
        return apy

    # ── Rate fetching ──────────────────────────────────────────────────

    def fetch_aave_rates(self, tokens: dict[str, str]) -> list[ProtocolRate]:
        """
        Fetch Aave V3 supply and borrow APYs for each token.
        tokens = {'USDC': '0x2791...', 'WETH': '0x7ceB...'}
        """
        rates = []
        for symbol, addr in tokens.items():
            try:
                cs       = Web3.to_checksum_address(addr)
                decimals = self._get_decimals(addr)
                price    = self._token_price_usd(addr)

                rd = self._aave_v3.functions.getReserveData(cs).call()

                # Raw field logging on first scan for verification
                if self._debug_raw:
                    field_names = [
                        'configuration', 'liquidityIndex',
                        'currentLiquidityRate', 'variableBorrowIndex',
                        'currentVariableBorrowRate', 'currentStableBorrowRate',
                        'lastUpdateTimestamp', 'id', 'aTokenAddress',
                        'stableDebtTokenAddress', 'variableDebtTokenAddress',
                        'interestRateStrategyAddress', 'accruedToTreasury',
                        'unbacked', 'isolationModeTotalDebt',
                    ]
                    logger.info(f'[DEBUG] Aave V3 getReserveData({symbol}) raw fields:')
                    for i, (name, val) in enumerate(zip(field_names, rd)):
                        logger.info(f'  [{i}] {name} = {val}')

                # rd[2] = currentLiquidityRate  (supply, annualised RAY)
                # rd[4] = currentVariableBorrowRate (borrow, annualised RAY)
                supply_apy = self._sanity_check_apy(
                    self._ray_to_apy(rd[2], f'Aave {symbol} supply'),
                    f'Aave {symbol} supply'
                )
                borrow_apy = self._sanity_check_apy(
                    self._ray_to_apy(rd[4], f'Aave {symbol} borrow'),
                    f'Aave {symbol} borrow'
                )

                if supply_apy is None or borrow_apy is None:
                    logger.error(
                        f'Aave {symbol}: sanity check failed — skipping. '
                        f'Check field indices in AAVE_V3_RESERVE_DATA_ABI.'
                    )
                    continue

                # TVL from data provider — use token's actual decimals
                total_supply_usd = Decimal('0')
                total_borrow_usd = Decimal('0')
                try:
                    dp = self._aave_dp.functions.getReserveData(cs).call()

                    if self._debug_raw:
                        dp_names = [
                            'unbacked', 'accruedToTreasuryScaled',
                            'totalAToken', 'totalStableDebt',
                            'totalVariableDebt', 'liquidityRate',
                            'variableBorrowRate', 'stableBorrowRate',
                            'averageStableBorrowRate', 'liquidityIndex',
                            'variableBorrowIndex', 'lastUpdateTimestamp',
                        ]
                        logger.info(
                            f'[DEBUG] DataProvider getReserveData({symbol}):'
                        )
                        for i, (name, val) in enumerate(zip(dp_names, dp)):
                            logger.info(f'  [{i}] {name} = {val}')

                    if price > 0:
                        # dp[2] = totalAToken (supply in raw token units)
                        # dp[3] + dp[4] = stable + variable debt
                        total_supply_usd = (
                            self._raw_to_human(dp[2], decimals) * price
                        )
                        total_borrow_usd = (
                            self._raw_to_human(dp[3] + dp[4], decimals) * price
                        )
                except Exception as e:
                    logger.warning(f'Aave {symbol} TVL fetch failed: {e}')

                rates.append(ProtocolRate(
                    protocol='AAVE_V3',
                    token=addr.lower(),
                    token_symbol=symbol,
                    token_decimals=decimals,
                    supply_apy=supply_apy,
                    borrow_apy=borrow_apy,
                    total_supply_usd=total_supply_usd,
                    total_borrow_usd=total_borrow_usd,
                ))

                logger.info(
                    f'Aave V3 {symbol} ({decimals}dec): '
                    f'supply={float(supply_apy)*100:.3f}% '
                    f'borrow={float(borrow_apy)*100:.3f}% '
                    f'TVL=${float(total_supply_usd):,.0f}'
                )

            except Exception as e:
                logger.warning(f'Aave V3 rate fetch {symbol}: {e}', exc_info=True)

        return rates

    def fetch_compound_rates(
        self, comet_addresses: dict[str, str]
    ) -> list[ProtocolRate]:
        """
        Fetch Compound V3 (Comet) supply and borrow rates.
        comet_addresses = {'USDC': '0xF252...'}
        """
        rates = []
        for symbol, addr in comet_addresses.items():
            try:
                comet = self.w3.eth.contract(
                    address=Web3.to_checksum_address(addr),
                    abi=COMET_RATE_ABI,
                )

                utilization = comet.functions.getUtilization().call()
                supply_rate = comet.functions.getSupplyRate(utilization).call()
                borrow_rate = comet.functions.getBorrowRate(utilization).call()
                base_token  = comet.functions.baseToken().call()
                decimals    = self._get_decimals(base_token)
                base_symbol = self._get_symbol(base_token)
                price       = self._token_price_usd(base_token)

                if self._debug_raw:
                    logger.info(f'[DEBUG] Compound V3 {symbol} raw values:')
                    logger.info(f'  base_token  = {base_token}')
                    logger.info(f'  base_symbol = {base_symbol}')
                    logger.info(f'  decimals    = {decimals}')
                    logger.info(
                        f'  utilization = {utilization}  '
                        f'(={float(Decimal(utilization)/Decimal("1e18"))*100:.2f}% '
                        f'of 1e18 scale)'
                    )
                    logger.info(
                        f'  supply_rate = {supply_rate}  '
                        f'(uint64 per-sec, scale=1e18)'
                    )
                    logger.info(
                        f'  borrow_rate = {borrow_rate}  '
                        f'(uint64 per-sec, scale=1e18)'
                    )

                supply_apy = self._sanity_check_apy(
                    self._compound_rate_to_apy(supply_rate, f'C3 {symbol} supply'),
                    f'Compound {symbol} supply'
                )
                borrow_apy = self._sanity_check_apy(
                    self._compound_rate_to_apy(borrow_rate, f'C3 {symbol} borrow'),
                    f'Compound {symbol} borrow'
                )

                if supply_apy is None or borrow_apy is None:
                    logger.error(
                        f'Compound {symbol}: sanity check failed — skipping. '
                        f'COMPOUND_SCALE is currently {COMPOUND_SCALE}.'
                    )
                    continue

                # TVL — must use base token's actual decimals (USDC = 6)
                total_supply_usd = Decimal('0')
                total_borrow_usd = Decimal('0')
                try:
                    raw_supply = comet.functions.totalSupply().call()
                    raw_borrow = comet.functions.totalBorrow().call()
                    if price > 0:
                        total_supply_usd = (
                            self._raw_to_human(raw_supply, decimals) * price
                        )
                        total_borrow_usd = (
                            self._raw_to_human(raw_borrow, decimals) * price
                        )
                except Exception as e:
                    logger.warning(f'Compound {symbol} TVL fetch: {e}')

                rates.append(ProtocolRate(
                    protocol='COMPOUND_V3',
                    token=base_token.lower(),
                    token_symbol=base_symbol,
                    token_decimals=decimals,
                    supply_apy=supply_apy,
                    borrow_apy=borrow_apy,
                    total_supply_usd=total_supply_usd,
                    total_borrow_usd=total_borrow_usd,
                ))

                logger.info(
                    f'Compound V3 {symbol} base={base_symbol} ({decimals}dec): '
                    f'supply={float(supply_apy)*100:.3f}% '
                    f'borrow={float(borrow_apy)*100:.3f}% '
                    f'util={float(Decimal(utilization)/Decimal("1e18"))*100:.1f}% '
                    f'TVL=${float(total_supply_usd):,.0f}'
                )

            except Exception as e:
                logger.warning(
                    f'Compound V3 rate fetch {symbol}: {e}', exc_info=True
                )

        return rates

    # ── Spread analysis ────────────────────────────────────────────────

    def find_rate_spreads(
        self,
        all_rates: list[ProtocolRate],
        min_spread_apy: Decimal = Decimal('0.005'),   # 0.5% minimum
    ) -> list[RateSpreadOpportunity]:
        """
        Find cross-protocol rate spread opportunities.
        Groups by underlying token address — never compares different tokens
        even if they share a symbol. Ensures decimal and price parity.
        Returns opportunities sorted by estimated daily profit descending.
        """
        by_token: dict[str, list[ProtocolRate]] = {}
        for r in all_rates:
            by_token.setdefault(r.token.lower(), []).append(r)

        opportunities = []

        for token_addr, rates in by_token.items():
            if len(rates) < 2:
                continue

            for i, r_a in enumerate(rates):
                for r_b in rates[i + 1:]:
                    for supply_r, borrow_r in [(r_a, r_b), (r_b, r_a)]:
                        spread = supply_r.supply_apy - borrow_r.borrow_apy

                        if spread < min_spread_apy:
                            continue

                        # Position size capped by available TVL on both sides
                        max_size = min(
                            supply_r.total_supply_usd or Decimal('50000'),
                            borrow_r.total_borrow_usd or Decimal('50000'),
                            Decimal('2000000'),   # $2M hard cap
                        )

                        daily_profit = max_size * spread / Decimal('365')

                        logger.debug(
                            f'Spread: {supply_r.protocol} supply '
                            f'{float(supply_r.supply_apy)*100:.3f}% > '
                            f'{borrow_r.protocol} borrow '
                            f'{float(borrow_r.borrow_apy)*100:.3f}% | '
                            f'spread={float(spread)*100:.3f}% | '
                            f'max=${float(max_size):,.0f} | '
                            f'daily≈${float(daily_profit):.2f}'
                        )

                        opportunities.append(RateSpreadOpportunity(
                            token=token_addr,
                            token_symbol=(
                                supply_r.token_symbol or borrow_r.token_symbol
                            ),
                            supply_protocol=supply_r.protocol,
                            borrow_protocol=borrow_r.protocol,
                            supply_apy=supply_r.supply_apy,
                            borrow_apy=borrow_r.borrow_apy,
                            spread_apy=spread,
                            max_flash_size_usd=max_size,
                            estimated_daily_profit_usd=daily_profit,
                        ))

        opportunities.sort(
            key=lambda x: x.estimated_daily_profit_usd, reverse=True
        )
        return opportunities

    def rank_liquidation_opportunities(
        self,
        liq_opportunities: list[dict],
        gas_price_gwei: float = 100,
        gas_per_liq: int = 500_000,
        matic_price_usd: float = 0.5,
    ) -> list[LiquidationDiscountOpportunity]:
        """Rank liquidation opportunities by net profit."""
        matic_p  = Decimal(str(matic_price_usd))
        gas_cost = (
            Decimal(str(gas_price_gwei))
            * Decimal(str(gas_per_liq))
            * Decimal('1e-9')
            * matic_p
        )
        ranked = []
        for opp in liq_opportunities:
            col_usd   = Decimal(str(opp.get('collateral_usd', 0)))
            bonus_pct = Decimal(str(opp.get('liq_bonus_pct', 0)))
            gross     = col_usd * bonus_pct
            net       = gross - gas_cost
            score     = net + bonus_pct * Decimal('1000')
            ranked.append(LiquidationDiscountOpportunity(
                protocol=opp['protocol'],
                user=opp['user'],
                collateral_asset=opp['collateral_asset'],
                debt_asset=opp['debt_asset'],
                collateral_usd=col_usd,
                debt_usd=Decimal(str(opp.get('debt_usd', 0))),
                liq_bonus_pct=bonus_pct,
                gross_profit_usd=gross,
                gas_cost_usd=gas_cost,
                net_profit_usd=net,
                priority_score=score,
            ))
        ranked.sort(key=lambda x: x.priority_score, reverse=True)
        return ranked

    # ── Main scan loop ─────────────────────────────────────────────────

    async def run_scan_loop(
        self,
        token_map:     dict[str, str],
        comet_markets: dict[str, str],
        radiant_pool:  str,          # kept for API compatibility — not used
        callback,
        min_spread_apy: Decimal = Decimal('0.005'),  # Conservative default
        ):
        """
        Async loop: every _scan_interval seconds fetch rates from all active
        protocols, find spreads, invoke callback(opportunities).

        Radiant excluded — Polygon deployment is inactive.
        Raw field logging active on first scan only, then disabled.
        """
        first_scan = True

        while True:
            try:
                await asyncio.sleep(self._scan_interval)

                self._debug_raw = first_scan

                all_rates: list[ProtocolRate] = []
                all_rates.extend(self.fetch_aave_rates(token_map))
                all_rates.extend(self.fetch_compound_rates(comet_markets))
                # Radiant excluded — Polygon deployment inactive
                # all_rates.extend(self.fetch_radiant_rates(radiant_pool, token_map))

                if first_scan:
                    logger.info(
                        '[ArbScanner] First scan complete — '
                        'raw field logging disabled for normal operation'
                    )
                    first_scan = False

                spreads = self.find_rate_spreads(all_rates,min_spread_apy=min_spread_apy)

                if spreads:
                    best = spreads[0]
                    logger.info(
                        f'[ArbScanner] Best: '
                        f'{best.supply_protocol}→{best.borrow_protocol} '
                        f'{best.token_symbol} '
                        f'spread={float(best.spread_apy)*100:.3f}% APY | '
                        f'max_size=${float(best.max_flash_size_usd):,.0f} | '
                        f'≈${float(best.estimated_daily_profit_usd):.2f}/day'
                    )
                    await callback(spreads)
                else:
                    logger.info('[ArbScanner] No profitable rate spreads this cycle')

            except Exception as e:
                logger.warning(f'[ArbScanner] Scan error: {e}', exc_info=True)