#!/usr/bin/env python3
"""
PRODUCTION ARBITRAGE BOT — Polygon V2/V3/Algebra cross-venue arbitrage.

Venues covered:
  • QuickSwap V2   (v2_quickswap)  — fully executable
  • Uniswap V3     (v3_uniswap)    — fully executable
  • Algebra/QS V3  (v3_quickswap_algebra) — fully executable (new)
  • Curve          (detection + logging only — no real second leg in math)

Contract: CorrectedSlippageArbitrageContract (onlyOwner + Algebra router)
Routing:  bloXroute private TX when BLOXROUTE_AUTH_HEADER is set;
          public mempool otherwise (no credits needed yet).
Math:     calc.calc_dya() — ternary-search CPMM optimiser; untouched.
"""

import os
import sys
import json
import time
import logging
import threading
import math
from logging.handlers import RotatingFileHandler
from typing import Optional

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
from eth_abi import encode as abi_encode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calc
import curve as curve_mod
import pool_discovery
import mev_protect
import accounting
import route_sanity
import price_math
from telemetry import Telemetry
from oracle_edge import OracleEdgeMonitor

# ============================================================================
# CONFIG
# ============================================================================

# Updated after redeployment — set via env or replace here after deploy
CONTRACT_ADDRESS = Web3.to_checksum_address(
    os.getenv('ARB_CONTRACT_ADDRESS', '0x778BF117E1a6F21BF2a9BAb24303319c49C7b661')
)
CONTRACT_ABI_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'contract', 'CorrectedSlippageArbitrageContract.abi.json',
)

BALANCER_VAULT       = Web3.to_checksum_address('0xBA12222222228d8Ba445958a75a0704d566BF2C8')
QUICKSWAP_V2_ROUTER  = Web3.to_checksum_address('0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff')
UNISWAP_V3_ROUTER    = Web3.to_checksum_address('0xE592427A0AEce92De3Edee1F18E0157C05861564')
ALGEBRA_ROUTER       = Web3.to_checksum_address('0xf5b509bB0909a69B1c207E495f687a596C168E12')

APPROVED_CURVE_POOLS = {
    Web3.to_checksum_address(a) for a in [
        '0x445FE580eF8d70FF569aB36e80c647af338db351',
        '0x5B082Cb0a4C4b7FD71B5E98803A74AdA0beA5cD6',
        '0x3A43a5851a3EaFa49A4e3fdC51B7D7eB623fef78',
        '0xE7a24EF0C5e95Ffb0f6684b813A78F2a3AD7D171',
        '0x751b1e21756bdBC307cbcC5084C042b6266a1d25',
    ]
}

ROUTER_TYPE = {
    'UNISWAP_V2': 0,
    'UNISWAP_V3': 1,
    'SUSHISWAP':  2,
    'QUICKSWAP':  3,
    'CURVE':      4,
    'ONE_INCH':   5,
    'ALGEBRA':    6,   # ← QuickSwap V3 / Algebra (matches contract enum)
}

ARB_PARAMS_TYPE = (
    '(address,uint256,'
    '(address,address,address,uint8,uint256,uint256,uint256,uint24,int128,int128,bool,bytes)[],'
    'uint256,uint256)'
)

V2_FEE            = 0.003
POLL_INTERVAL_S   = float(os.getenv('ARB_POLL_INTERVAL_S', '20'))
DEADLINE_WINDOW_S = 120
GAS_LIMIT         = 1_800_000
SIM_STAGGER_S     = 1.0
NATIVE_GAS_TOKEN_SYM = 'WMATIC'

# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger('arb_bot')
logger.setLevel(logging.INFO)
_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'arb_bot.log')
_fh = RotatingFileHandler(_log_path, maxBytes=10_000_000, backupCount=3)
_fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(_fh)
logger.addHandler(_ch)
for _mod in ('pool_discovery', 'curve', 'calc'):
    logging.getLogger(_mod).addHandler(_ch)


def _mask(s: str) -> str:
    s = s or ''
    return s[:60] + '...(masked)' if len(s) > 60 else s


def _get_algebra_fee(pool_addr: str, stated_fee: Optional[int], rpc=None) -> float:
    """Return Algebra fee as a fraction, never silently treating missing as free."""
    if stated_fee is not None and int(stated_fee) > 0:
        return int(stated_fee) / 1_000_000.0

    key = pool_addr.lower()
    if key in _algebra_fee_cache:
        return _algebra_fee_cache[key]

    if rpc is not None:
        try:
            pool = rpc.w3.eth.contract(
                address=Web3.to_checksum_address(pool_addr),
                abi=_ALGEBRA_STATE_ABI,
            )
            state = rpc.call(pool.functions.globalState().call, retries=2)
            fee = int(state[2])
            if fee > 0:
                value = fee / 1_000_000.0
                _algebra_fee_cache[key] = value
                return value
        except Exception as exc:
            logger.warning(
                f'Algebra fee read failed for {pool_addr[:10]}...; '
                f'using conservative {ALGEBRA_FEE_FALLBACK_BPS:.2f}bps '
                f'({_mask(str(exc))})'
            )

    value = ALGEBRA_FEE_FALLBACK_BPS / 10_000.0
    _algebra_fee_cache[key] = value
    return value


def _fee_fraction(kind: str, raw_fee: int) -> float:
    if kind == 'v2':
        return V2_FEE
    if kind == 'algebra':
        return max(0.0, float(raw_fee or 0) / 1_000_000.0)
    if kind == 'v3':
        return max(0.0, float(raw_fee or 0) / 1_000_000.0)
    return 0.0


def _enrich_opportunity(opp: dict, *, block_number: int, detected_at: float) -> dict:
    """Attach common route/accounting fields without changing calc.py output."""
    opp.setdefault('strategy_type', 'cross_dex')
    opp.setdefault('route_hops', 2)
    opp['route_tokens'] = [opp['addr_b'], opp['addr_a'], opp['addr_b']]
    opp['timestamp_detected'] = detected_at
    opp['block_number'] = block_number
    opp['detection_latency_ms'] = max(0.0, (time.time() - detected_at) * 1000.0)
    opp['direction'] = f"{opp['sym_b']}→{opp['sym_a']}→{opp['sym_b']}"
    opp['price_source'] = 'Polygon pool state: V2 reserves/V3 slot0/Algebra globalState'
    # calc_dya returns the route result after applying both swap fees and
    # price impact. Keep a separately visible estimate for telemetry, but do
    # not subtract it again in accounting.
    cheap_fee = _fee_fraction(opp['cheap_kind'], opp.get('cheap_fee', 0))
    expensive_fee = _fee_fraction(opp['expensive_kind'], opp.get('expensive_fee', 0))
    opp['dex_fees_b_human'] = (
        opp['dya_human'] * cheap_fee + opp['dyb_human'] * expensive_fee
    )
    opp['flashloan_fee_b_human'] = opp['dya_human'] * FLASHLOAN_FEE_BPS / 10_000.0
    opp['liquidity'] = opp.get('liquidity') or opp.get('tvl_usd_est')
    return opp


def _parse_min_profit_usd() -> float:
    raw     = (os.getenv('MIN_PROFIT_USD') or '').strip()
    cleaned = raw.lstrip('$').replace(',', '').strip()
    if not cleaned:
        logger.warning('MIN_PROFIT_USD not set — defaulting to $5.00')
        return 02.0
    try:
        val = float(cleaned)
        if raw != cleaned:
            logger.warning(f'MIN_PROFIT_USD had non-numeric chars ("{raw}") — parsed as {val}')
        return val
    except ValueError:
        logger.warning(f'MIN_PROFIT_USD="{raw}" is not a valid number — defaulting to $5.00')
        return 02.0


MIN_PROFIT_USD = _parse_min_profit_usd()


def _env_float(name: str, default: float, *, minimum: Optional[float] = None) -> float:
    raw = (os.getenv(name) or '').strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        logger.warning(f'{name}="{raw}" is invalid — defaulting to {default}')
        value = default
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        logger.warning(f'{name}={raw or value} is outside allowed range — defaulting to {default}')
        value = default
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or '').strip().lower()
    if not raw:
        return default
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    if raw in ('0', 'false', 'no', 'off'):
        return False
    logger.warning(f'{name}="{raw}" is invalid — defaulting to {default}')
    return default


PAPER_MODE = _env_bool('PAPER_MODE', True)
MAX_GAS_USD = _env_float('MAX_GAS_USD', 25.0, minimum=0.0)
MAX_SLIPPAGE_BPS = _env_float('MAX_SLIPPAGE_BPS', 100.0, minimum=0.0)
MAX_STATE_AGE = _env_float('MAX_STATE_AGE', 30.0, minimum=0.0)
FLASHLOAN_FEE_BPS = _env_float('FLASHLOAN_FEE_BPS', 9.0, minimum=0.0)
MAX_ROUTE_HOPS = int(_env_float('MAX_ROUTE_HOPS', 2.0, minimum=1.0))
MAX_PROFIT_BPS = _env_float('MAX_PROFIT_BPS', 1_000.0, minimum=1.0)
MAX_TRADE_USD = _env_float('MAX_TRADE_USD', 1_000_000.0, minimum=1.0)
GAS_PRICE_MULTIPLIER = _env_float('GAS_PRICE_MULTIPLIER', 1.15, minimum=1.0)
PRIVATE_SUBMISSION = _env_bool('PRIVATE_SUBMISSION', True)
ALLOW_PUBLIC_SUBMISSION = _env_bool('ALLOW_PUBLIC_SUBMISSION', False)
ORACLE_EDGE_ENABLED = _env_bool('ORACLE_EDGE_ENABLED', True)
ORACLE_POLL_INTERVAL_S = _env_float('ORACLE_POLL_INTERVAL_S', 60.0, minimum=1.0)

# Algebra V1 globalState() ABI.  A zero/missing fee is not accepted as
# zero-cost: the query is retried here and a conservative 0.05% fallback is
# used only when the pool cannot be read.
_ALGEBRA_STATE_ABI = [{
    'name': 'globalState',
    'type': 'function',
    'inputs': [],
    'outputs': [
        {'name': 'price', 'type': 'uint160'},
        {'name': 'tick', 'type': 'int24'},
        {'name': 'fee', 'type': 'uint16'},
    ],
    'stateMutability': 'view',
}]
ALGEBRA_FEE_FALLBACK_BPS = 5.0
_algebra_fee_cache = {}

# ============================================================================
# RPC
# ============================================================================

def _build_onfinality_url() -> Optional[str]:
    raw = (os.getenv('FINALITY_API_KEY') or '').strip()
    if not raw:
        return None
    return raw if raw.startswith('http') else f'https://polygon.api.onfinality.io/rpc?apikey={raw}'


def _build_alchemy_url() -> Optional[str]:
    raw = (os.getenv('ALCHEMY_API_KEY') or '').strip()
    if not raw:
        return None
    return raw if raw.startswith('http') else f'https://polygon-mainnet.g.alchemy.com/v2/{raw}'


class Rpc:
    def __init__(self):
        self._urls = [u for u in (
            _build_onfinality_url(),
            _build_alchemy_url(),
            'https://polygon-rpc.com',
        ) if u]
        if not self._urls:
            sys.exit('ERROR: No RPC configured. Set FINALITY_API_KEY or ALCHEMY_API_KEY.')
        self._idx = 0
        self.w3   = self._connect(self._urls[0])

    def _connect(self, url: str) -> Web3:
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 20}))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        return w3

    def _rotate(self):
        self._idx = (self._idx + 1) % len(self._urls)
        logger.warning(f'Rotating RPC → index {self._idx}')
        self.w3 = self._connect(self._urls[self._idx])

    @staticmethod
    def _is_rate_limited(exc: Exception) -> bool:
        s = str(exc)
        return '429' in s or 'Too Many Requests' in s or 'rate limit' in s.lower()

    def call(self, fn, *args, retries=3, **kwargs):
        last_exc = None
        for attempt in range(retries):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_exc = e
                rl = self._is_rate_limited(e)
                logger.warning(
                    f'RPC call failed (attempt {attempt+1}/{retries})'
                    f'{" [RATE-LIMITED]" if rl else ""}: {_mask(str(e))}'
                )
                if len(self._urls) > 1:
                    self._rotate()
                time.sleep(min(8 * (attempt + 1), 30) if rl else min(2 * (attempt + 1), 8))
        raise last_exc

# ============================================================================
# WALLET / NONCE
# ============================================================================

class NonceManager:
    def __init__(self, rpc: Rpc, address: str):
        self.rpc     = rpc
        self.address = address
        self._lock   = threading.Lock()
        self._next   = None

    def _resync(self):
        self._next = self.rpc.call(
            self.rpc.w3.eth.get_transaction_count, self.address, 'pending'
        )

    def get(self) -> int:
        with self._lock:
            if self._next is None:
                self._resync()
            n = self._next
            self._next += 1
            return n

    def resync(self):
        with self._lock:
            self._resync()


def load_wallet():
    pk = (os.getenv('PRIVATE_KEY') or '').strip()
    if not pk:
        return None
    if not pk.startswith('0x'):
        pk = '0x' + pk
    return Account.from_key(pk)

# ============================================================================
# ABI ENCODING
# ============================================================================

def encode_swap(token_in, token_out, router, router_type, expected_out=0, fee=0,
                curve_in_idx=0, curve_out_idx=0, use_underlying=False):
    return (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        Web3.to_checksum_address(router),
        router_type,
        int(expected_out),
        1,
        0,
        int(fee),
        int(curve_in_idx),
        int(curve_out_idx),
        bool(use_underlying),
        b'',
    )


def encode_arbitrage_calldata(flashloan_token, flashloan_amount, swaps,
                               expected_profit, deadline) -> bytes:
    params = (
        Web3.to_checksum_address(flashloan_token),
        int(flashloan_amount),
        list(swaps),
        int(expected_profit),
        int(deadline),
    )
    return abi_encode([ARB_PARAMS_TYPE], [params])

# ============================================================================
# ROUTER DISPATCH
# ============================================================================

def _venue_router(kind: str, fee: int):
    """Return (router_address, router_type_int, fee_for_encoding)."""
    if kind == 'v2':
        return QUICKSWAP_V2_ROUTER, ROUTER_TYPE['QUICKSWAP'], 0
    if kind == 'algebra':
        # Algebra router ignores fee at the call level — dynamic per-pool
        return ALGEBRA_ROUTER, ROUTER_TYPE['ALGEBRA'], 0
    # Default: Uniswap V3
    return UNISWAP_V3_ROUTER, ROUTER_TYPE['UNISWAP_V3'], fee

# ============================================================================
# V2 / V3 OPPORTUNITY DETECTION (math unchanged from research prototype)
# ============================================================================

def find_v2_v3_opportunities(confirmed_pairs: list) -> list:
    opportunities = []
    for entry in confirmed_pairs:
        sym_a, sym_b = entry['sym_a'], entry['sym_b']
        v2 = next((e for e in entry['venues'] if e['venue'] == 'v2_quickswap'), None)
        v3_candidates = [e for e in entry['venues'] if e['venue'] == 'v3_uniswap']
        if not v2 or not v2.get('reserves') or not v3_candidates:
            continue

        dec_a   = pool_discovery.TOKEN_DECIMALS.get(sym_a, 18)
        dec_b   = pool_discovery.TOKEN_DECIMALS.get(sym_b, 18)
        addr_a, addr_b = v2['addr_a'], v2['addr_b']

        try:
            amt_a_v2, amt_b_v2 = price_math.v2_human_reserves(
                v2['reserves'], addr_a, addr_b, dec_a, dec_b
            )
        except (TypeError, ValueError, OverflowError):
            continue

        p_v2 = amt_b_v2 / amt_a_v2
        L_v2 = (amt_a_v2 * amt_b_v2) ** 0.5
        s_v2 = (amt_b_v2 / amt_a_v2) ** 0.5

        for v3 in v3_candidates:
            sqrt_p = v3.get('sqrt_price_x96')
            liq    = v3.get('liquidity')
            if not sqrt_p or not liq:
                continue

            try:
                p_v3, s_v3, liq_v3_human = price_math.v3_oriented_state(
                    sqrt_p, liq, v3['addr_a'], v3['addr_b'], dec_a, dec_b
                )
            except (TypeError, ValueError, OverflowError):
                continue

            v2_fee_bps = V2_FEE * 10000
            v3_fee_bps = v3['fee'] / 1_000_000 * 10000
            spread_bps = (p_v2 / p_v3 - 1) * 10000
            if abs(spread_bps) <= (v2_fee_bps + v3_fee_bps):
                continue

            if p_v2 < p_v3:
                pool_a = [(s_v2, float('inf'), L_v2)]
                pool_b = [(0.0, s_v3, liq_v3_human)]
                fa, fb = V2_FEE, v3['fee'] / 1_000_000
                cheap_kind, expensive_kind = 'v2', 'v3'
                cheap_fee, expensive_fee   = 0, v3['fee']
            else:
                pool_a = [(s_v3, float('inf'), liq_v3_human)]
                pool_b = [(0.0, s_v2, L_v2)]
                fa, fb = v3['fee'] / 1_000_000, V2_FEE
                cheap_kind, expensive_kind = 'v3', 'v2'
                cheap_fee, expensive_fee   = v3['fee'], 0

            try:
                dya, dyb, _, _ = calc.calc_dya(pool_a, pool_b, fa, fb)
            except (AssertionError, ZeroDivisionError) as e:
                logger.debug(f'{sym_a}/{sym_b}: calc error ({e})')
                continue
            if dya <= 0:
                continue
            gross_profit_b = dyb - dya
            if gross_profit_b <= 0:
                continue

            opportunities.append({
                'sym_a': sym_a, 'sym_b': sym_b,
                'addr_a': addr_a, 'addr_b': addr_b,
                'dec_a': dec_a, 'dec_b': dec_b,
                'dya_human': dya, 'dyb_human': dyb,
                'gross_profit_b_human': gross_profit_b,
                'spread_bps': spread_bps,
                'cheap_kind': cheap_kind, 'expensive_kind': expensive_kind,
                'cheap_fee': cheap_fee, 'expensive_fee': expensive_fee,
            })
    return opportunities


def find_algebra_opportunities(confirmed_pairs: list, rpc=None) -> list:
    """
    Find executable Algebra cross-venue opportunities.
    Previously detection-only; now returns full opp dicts for execution
    because the contract now has the ALGEBRA router type (enum value 6).
    """
    opportunities = []
    for entry in confirmed_pairs:
        sym_a, sym_b = entry['sym_a'], entry['sym_b']
        algebra_candidates = [e for e in entry['venues']
                               if e['venue'] == 'v3_quickswap_algebra']
        other_candidates   = [e for e in entry['venues']
                               if e['venue'] in ('v2_quickswap', 'v3_uniswap')]
        if not algebra_candidates or not other_candidates:
            continue

        dec_a = pool_discovery.TOKEN_DECIMALS.get(sym_a, 18)
        dec_b = pool_discovery.TOKEN_DECIMALS.get(sym_b, 18)

        def _v3_style(e):
            sqrt_p = e.get('sqrt_price_x96')
            liq    = e.get('liquidity')
            if not sqrt_p or not liq:
                return None
            a_is_token0  = int(e['addr_a'], 16) < int(e['addr_b'], 16)
            dec0, dec1   = (dec_a, dec_b) if a_is_token0 else (dec_b, dec_a)
            try:
                return price_math.v3_oriented_state(
                    sqrt_p, liq, e['addr_a'], e['addr_b'], dec_a, dec_b
                )
            except (TypeError, ValueError, OverflowError):
                return None

        for alg_e in algebra_candidates:
            alg_data = _v3_style(alg_e)
            if not alg_data:
                continue
            p_alg, s_alg, liq_alg = alg_data
            alg_fee_frac = _get_algebra_fee(
                alg_e['pool_address'],
                alg_e.get('algebra_fee'),
                rpc,
            )
            addr_a = alg_e['addr_a']
            addr_b = alg_e['addr_b']

            for other_e in other_candidates:
                if other_e['venue'] == 'v2_quickswap':
                    if not other_e.get('reserves'):
                        continue
                    oa, ob   = other_e['addr_a'], other_e['addr_b']
                    try:
                        amt_a, amt_b = price_math.v2_human_reserves(
                            other_e['reserves'], oa, ob, dec_a, dec_b
                        )
                    except (TypeError, ValueError, OverflowError):
                        continue
                    p_other      = amt_b / amt_a
                    s_other      = p_other ** 0.5
                    L_other      = (amt_a * amt_b) ** 0.5
                    other_fee    = V2_FEE
                    other_kind   = 'v2'
                    other_fee_raw = 0
                else:  # v3_uniswap
                    od = _v3_style(other_e)
                    if not od:
                        continue
                    p_other, s_other, L_other = od
                    other_fee     = other_e['fee'] / 1_000_000
                    other_kind    = 'v3'
                    other_fee_raw = other_e['fee']

                fee_bps_sum = (alg_fee_frac + other_fee) * 10000
                spread_bps  = (p_other / p_alg - 1) * 10000
                if abs(spread_bps) <= fee_bps_sum:
                    continue

                if p_other < p_alg:
                    # other is cheap, Algebra is expensive
                    pool_a = [(s_other, float('inf'), L_other)]
                    pool_b = [(0.0, s_alg, liq_alg)]
                    fa, fb = other_fee, alg_fee_frac
                    cheap_kind, expensive_kind = other_kind, 'algebra'
                    cheap_fee_raw, expensive_fee_raw = other_fee_raw, round(alg_fee_frac * 1_000_000)
                else:
                    # Algebra is cheap, other is expensive
                    pool_a = [(s_alg, float('inf'), liq_alg)]
                    pool_b = [(0.0, s_other, L_other)]
                    fa, fb = alg_fee_frac, other_fee
                    cheap_kind, expensive_kind = 'algebra', other_kind
                    cheap_fee_raw, expensive_fee_raw = round(alg_fee_frac * 1_000_000), other_fee_raw

                try:
                    dya, dyb, _, _ = calc.calc_dya(pool_a, pool_b, fa, fb)
                except (AssertionError, ZeroDivisionError):
                    continue
                if dya <= 0:
                    continue
                gross_profit_b = dyb - dya
                if gross_profit_b <= 0:
                    continue

                opportunities.append({
                    'sym_a': sym_a, 'sym_b': sym_b,
                    'addr_a': addr_a, 'addr_b': addr_b,
                    'dec_a': dec_a, 'dec_b': dec_b,
                    'dya_human': dya, 'dyb_human': dyb,
                    'gross_profit_b_human': gross_profit_b,
                    'spread_bps': spread_bps,
                    'cheap_kind': cheap_kind, 'expensive_kind': expensive_kind,
                    'cheap_fee': cheap_fee_raw, 'expensive_fee': expensive_fee_raw,
                })
    return opportunities

# ============================================================================
# CALLDATA BUILDER
# ============================================================================

def build_arb_calldata(opp: dict) -> Optional[dict]:
    ok, reason = route_sanity.validate_opportunity(
        {**opp, '_token_universe': pool_discovery.TOKEN_UNIVERSE},
        max_profit_bps=MAX_PROFIT_BPS,
        max_trade_usd=MAX_TRADE_USD,
    )
    if not ok:
        logger.warning(
            f'[REJECT] {opp.get("sym_a")}/{opp.get("sym_b")}: '
            f'route sanity failed ({reason})'
        )
        return None

    dec_b            = opp['dec_b']
    flashloan_amount = int(opp['dya_human'] * (10 ** dec_b))
    if flashloan_amount <= 0 or not math.isfinite(opp['dya_human']):
        return None

    curve_pool = opp.get('curve_pool')
    c_i        = opp.get('curve_i', 0)
    c_j        = opp.get('curve_j', 0)

    # For Curve legs the pool address IS the router; coin indices go in the swap step.
    if opp['cheap_kind'] == 'curve':
        router1, rtype1, fee1 = curve_pool, ROUTER_TYPE['CURVE'], 0
        ci1, co1 = c_i, c_j
    else:
        router1, rtype1, fee1 = _venue_router(opp['cheap_kind'], opp['cheap_fee'])
        ci1, co1 = 0, 0

    if opp['expensive_kind'] == 'curve':
        router2, rtype2, fee2 = curve_pool, ROUTER_TYPE['CURVE'], 0
        ci2, co2 = c_i, c_j
    else:
        router2, rtype2, fee2 = _venue_router(opp['expensive_kind'], opp['expensive_fee'])
        ci2, co2 = 0, 0

    if not router1 or not router2:
        return None

    swap1 = encode_swap(opp['addr_b'], opp['addr_a'], router1, rtype1, fee=fee1,
                        curve_in_idx=ci1, curve_out_idx=co1)
    swap2 = encode_swap(opp['addr_a'], opp['addr_b'], router2, rtype2, fee=fee2,
                        curve_in_idx=ci2, curve_out_idx=co2)

    expected_profit = int(opp['gross_profit_b_human'] * (10 ** dec_b))
    if expected_profit <= 0:
        return None
    deadline        = int(time.time()) + DEADLINE_WINDOW_S

    calldata = encode_arbitrage_calldata(
        opp['addr_b'], flashloan_amount, [swap1, swap2], expected_profit, deadline,
    )
    return {
        'calldata':        calldata,
        'flashloan_amount': flashloan_amount,
        'expected_profit':  expected_profit,
        'deadline':         deadline,
        'route_tokens': [opp['addr_b'], opp['addr_a'], opp['addr_b']],
    }

# ============================================================================
# CURVE — full execution pipeline (Curve↔V2 two-leg arbitrage)
# ============================================================================

def _v2_getamountout(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    """QuickSwap/Uniswap V2 CPMM getAmountOut (integer, 0.3% fee)."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    net = amount_in * 997
    return (reserve_out * net) // (reserve_in * 1000 + net)


def find_curve_opportunities(raw_candidates: list, rpc: Rpc) -> list:
    """
    Find executable Curve↔QuickSwap V2 cross-venue arbitrage opportunities.

    Two directions are tried for every (Curve pool, V2 pool) pair:
      A) Curve-first  — flash sym_i, Curve sym_i→sym_j, V2 sym_j→sym_i
      B) V2-first     — flash sym_j, V2 sym_j→sym_i,   Curve sym_i→sym_j

    The ternary search uses the real V2 CPMM formula for the second leg.
    All amounts are raw uint256 throughout; converted to human units only
    when storing in the opportunity dict (so build_arb_calldata works
    identically for Curve and non-Curve opportunities).

    Note: Curve pools must be approved in the contract via addCurvePool()
    before real execution. The eth_call simulation gate catches non-approved
    pools gracefully.
    """
    try:
        curve_pools = rpc.call(
            curve_mod.discover_curve_pools, rpc.w3, pool_discovery.TOKEN_UNIVERSE
        )
    except Exception as e:
        logger.warning(f'[Curve] discovery failed: {_mask(str(e))}')
        return []

    logger.info(f'[Curve] {len(curve_pools)} pools matched TOKEN_UNIVERSE')

    # Build V2 pool lookup: frozenset({sym_a, sym_b}) → entry dict
    v2_by_pair: dict = {}
    for c in raw_candidates:
        if c['venue'] == 'v2_quickswap' and c.get('reserves'):
            key = frozenset([c['sym_a'], c['sym_b']])
            v2_by_pair[key] = c

    opportunities = []

    for pool in curve_pools:
        matched   = pool['matched']
        balances  = pool['balances']
        pool_addr = pool['pool_address']
        if len(matched) < 2:
            continue

        pool_contract = rpc.w3.eth.contract(
            Web3.to_checksum_address(pool_addr),
            abi=[{
                'name': 'get_dy', 'type': 'function',
                'inputs': [
                    {'name': 'i', 'type': 'int128'},
                    {'name': 'j', 'type': 'int128'},
                    {'name': 'dx', 'type': 'uint256'},
                ],
                'outputs': [{'name': '', 'type': 'uint256'}],
                'stateMutability': 'view',
            }]
        )

        for ma in range(len(matched)):
            i_coin_idx, sym_i = matched[ma]
            dec_i = pool_discovery.TOKEN_DECIMALS.get(sym_i, 18)
            bal_i = balances[i_coin_idx] if i_coin_idx < len(balances) else 0
            if bal_i == 0:
                continue

            for mb in range(len(matched)):
                if mb == ma:
                    continue
                j_coin_idx, sym_j = matched[mb]
                dec_j = pool_discovery.TOKEN_DECIMALS.get(sym_j, 18)
                bal_j = balances[j_coin_idx] if j_coin_idx < len(balances) else 0
                if bal_j == 0:
                    continue

                v2 = v2_by_pair.get(frozenset([sym_i, sym_j]))
                if not v2:
                    continue

                # Align V2 reserves to (sym_i, sym_j) order
                r0_raw, r1_raw = v2['reserves']
                a_is_t0 = int(v2['addr_a'], 16) < int(v2['addr_b'], 16)
                if v2['sym_a'] == sym_i:
                    r_i_raw = r0_raw if a_is_t0 else r1_raw
                    r_j_raw = r1_raw if a_is_t0 else r0_raw
                else:
                    r_i_raw = r1_raw if a_is_t0 else r0_raw
                    r_j_raw = r0_raw if a_is_t0 else r1_raw
                if r_i_raw == 0 or r_j_raw == 0:
                    continue

                addr_i = pool_discovery.TOKEN_ADDRESSES[sym_i]
                addr_j = pool_discovery.TOKEN_ADDRESSES[sym_j]

                # Capture loop vars for closures
                _r_i, _r_j = r_i_raw, r_j_raw
                _ic, _jc   = i_coin_idx, j_coin_idx

                def _curve_get_dy(dx: int, _ic=_ic, _jc=_jc) -> int:
                    try:
                        return pool_contract.functions.get_dy(_ic, _jc, dx).call()
                    except Exception:
                        return 0

                # ── Direction A: Curve cheap → V2 expensive ───────────────
                # Flash sym_i, spend on Curve (sym_i→sym_j), sell sym_j at V2
                # Pre-check: sample at 0.1% of pool depth (1 RPC call).
                # Typical stablecoin spread is <1 bp — this gate fires instantly
                # when there's no spread, avoiding 60+ RPC calls per pair.
                dx_hi_a = int(bal_i * 0.02)
                if dx_hi_a > 1:
                    dx_sample_a = max(1, dx_hi_a // 200)
                    dy_sample_a = _curve_get_dy(dx_sample_a)
                    pre_ret_a   = _v2_getamountout(dy_sample_a, _r_j, _r_i)
                    if pre_ret_a > dx_sample_a:
                        def _profit_a(dx: int, _ri=_r_i, _rj=_r_j) -> int:
                            dy_j = _curve_get_dy(dx)
                            ret  = _v2_getamountout(dy_j, _rj, _ri)
                            return ret - dx

                        lo_a, hi_a = 0, dx_hi_a
                        for _ in range(20):
                            if hi_a - lo_a < 2:
                                break
                            m1 = lo_a + (hi_a - lo_a) // 3
                            m2 = hi_a - (hi_a - lo_a) // 3
                            if _profit_a(m1) < _profit_a(m2):
                                lo_a = m1
                            else:
                                hi_a = m2
                        best_dx_a  = (lo_a + hi_a) // 2
                        best_pft_a = _profit_a(best_dx_a)

                        if best_pft_a > 0:
                            opportunities.append({
                                'sym_a': sym_j, 'sym_b': sym_i,
                                'addr_a': addr_j, 'addr_b': addr_i,
                                'dec_a': dec_j,  'dec_b': dec_i,
                                'dya_human':            best_dx_a  / 10**dec_i,
                                'dyb_human':            (best_dx_a + best_pft_a) / 10**dec_i,
                                'gross_profit_b_human': best_pft_a / 10**dec_i,
                                'spread_bps': 0,
                                'cheap_kind': 'curve', 'expensive_kind': 'v2',
                                'cheap_fee':  0,        'expensive_fee':  0,
                                'curve_pool': pool_addr,
                                'curve_i': i_coin_idx, 'curve_j': j_coin_idx,
                            })
                            logger.info(
                                f'[Curve] {sym_i}/{sym_j} Curve→V2 '
                                f'dx={best_dx_a} profit={best_pft_a} {sym_i}'
                            )

                # ── Direction B: V2 cheap → Curve expensive ───────────────
                # Flash sym_j, sell at V2 (sym_j→sym_i), sell sym_i at Curve
                dx_hi_b = int(bal_j * 0.02)
                if dx_hi_b > 1:
                    dx_sample_b = max(1, dx_hi_b // 200)
                    dy_sample_b = _v2_getamountout(dx_sample_b, _r_j, _r_i)
                    pre_ret_b   = _curve_get_dy(dy_sample_b)
                    if pre_ret_b <= dx_sample_b:
                        continue

                    def _profit_b(dx: int, _ri=_r_i, _rj=_r_j) -> int:
                        dy_i  = _v2_getamountout(dx, _rj, _ri)
                        ret_j = _curve_get_dy(dy_i)
                        return ret_j - dx

                    lo_b, hi_b = 0, dx_hi_b
                    for _ in range(20):
                        if hi_b - lo_b < 2:
                            break
                        m1 = lo_b + (hi_b - lo_b) // 3
                        m2 = hi_b - (hi_b - lo_b) // 3
                        if _profit_b(m1) < _profit_b(m2):
                            lo_b = m1
                        else:
                            hi_b = m2
                    best_dx_b  = (lo_b + hi_b) // 2
                    best_pft_b = _profit_b(best_dx_b)

                    if best_pft_b > 0:
                        opportunities.append({
                            'sym_a': sym_i, 'sym_b': sym_j,
                            'addr_a': addr_i, 'addr_b': addr_j,
                            'dec_a': dec_i,   'dec_b': dec_j,
                            'dya_human':            best_dx_b  / 10**dec_j,
                            'dyb_human':            (best_dx_b + best_pft_b) / 10**dec_j,
                            'gross_profit_b_human': best_pft_b / 10**dec_j,
                            'spread_bps': 0,
                            'cheap_kind': 'v2', 'expensive_kind': 'curve',
                            'cheap_fee':  0,    'expensive_fee':  0,
                            'curve_pool': pool_addr,
                            'curve_i': i_coin_idx, 'curve_j': j_coin_idx,
                        })
                        logger.info(
                            f'[Curve] {sym_i}/{sym_j} V2→Curve '
                            f'dx={best_dx_b} profit={best_pft_b} {sym_j}'
                        )

    return opportunities

# ============================================================================
# EXECUTION
# ============================================================================

class Executor:
    def __init__(self, rpc: Rpc, wallet, contract, telemetry: Optional[Telemetry] = None):
        self.rpc               = rpc
        self.wallet            = wallet
        self.contract          = contract
        self.nonces            = NonceManager(rpc, wallet.address) if wallet else None
        self.execution_enabled = _env_bool('EXECUTION_ENABLED', False)
        self.telemetry         = telemetry or Telemetry()

    def simulate(self, calldata: bytes) -> tuple:
        """eth_call dry-run — live-state profitability confirmation.

        The contract's onlyOwner modifier is only enforced for real TX signing,
        NOT for eth_call simulations (which bypass msg.sender checks in most
        RPC implementations when called with the owner address).
        """
        try:
            self.rpc.call(
                self.contract.functions.executeArbitrage(calldata).call,
                {'from': self.wallet.address if self.wallet else CONTRACT_ADDRESS},
            )
            return True, 'simulation OK'
        except Exception as e:
            return False, str(e)

    def estimate_gas(self, calldata: bytes, gas_price_wei: int) -> int:
        from_addr = self.wallet.address if self.wallet else CONTRACT_ADDRESS
        return int(self.rpc.call(
            self.contract.functions.executeArbitrage(calldata).estimate_gas,
            {'from': from_addr, 'gasPrice': int(gas_price_wei)},
            retries=2,
        ))

    def execute(self, opp: dict, built: dict, economics, gas_price_wei: int):
        label = (f"{opp['sym_a']}/{opp['sym_b']} "
                 f"({opp['cheap_kind']}→{opp['expensive_kind']})")
        candidate_id = opp.get('opportunity_id')
        started = time.time()

        ok, msg = self.simulate(built['calldata'])
        if not ok:
            logger.info(
                f'[SKIP] {label}: eth_call reverted — not profitable at live state '
                f'({_mask(msg)})'
            )
            self.telemetry.rejection(
                opp, 'ETH_CALL_REVERT', simulation_latency_ms=(time.time() - started) * 1000.0,
                error=_mask(msg),
            )
            return
        self.telemetry.emit(
            'simulation_succeeded',
            opportunity_id=candidate_id,
            simulation_latency_ms=(time.time() - started) * 1000.0,
            **economics.as_dict(),
        )

        logger.info(
            f'[CONFIRMED] {label}: simulation passed — '
            f'gross ~{opp["gross_profit_b_human"]:.6f} {opp["sym_b"]} '
            f'(spread {opp["spread_bps"]:.1f}bps), net ~${economics.net_profit_usd:.2f}'
        )

        if not self.wallet:
            logger.info('[DRY-RUN] No PRIVATE_KEY — detection-only, not submitting.')
            self.telemetry.emit(
                'paper_execution',
                opportunity_id=candidate_id,
                execution_result='NO_WALLET',
                net_profit_usd=economics.net_profit_usd,
            )
            return
        if PAPER_MODE:
            logger.info('[PAPER] PAPER_MODE=true — simulation passed; not submitting.')
            self.telemetry.emit(
                'paper_execution',
                opportunity_id=candidate_id,
                execution_result='PAPER_MODE',
                net_profit_usd=economics.net_profit_usd,
            )
            return
        if not self.execution_enabled:
            logger.info('[DRY-RUN] EXECUTION_ENABLED≠true — opportunity found but not submitted.')
            self.telemetry.emit(
                'paper_execution',
                opportunity_id=candidate_id,
                execution_result='EXECUTION_DISABLED',
            )
            return
        if PRIVATE_SUBMISSION and not mev_protect.is_bloxroute_configured() and not ALLOW_PUBLIC_SUBMISSION:
            logger.warning(
                f'[SKIP] {label}: private submission required but bloXroute is not configured'
            )
            self.telemetry.rejection(opp, 'PRIVATE_SUBMISSION_UNAVAILABLE')
            return

        try:
            nonce    = self.nonces.get()
            gas_price = int(gas_price_wei * 1.15)
            submission_started = time.time()
            tx = self.contract.functions.executeArbitrage(built['calldata']).build_transaction({
                'from':     self.wallet.address,
                'nonce':    nonce,
                'gas':      int(economics.gas_units * 1.3),
                'gasPrice': gas_price,
                'chainId':  137,
            })

            signed   = self.wallet.sign_transaction(tx)
            tx_hash  = mev_protect.submit_transaction(
                self.rpc.w3,
                signed,
                allow_public_fallback=ALLOW_PUBLIC_SUBMISSION or not PRIVATE_SUBMISSION,
            )
            logger.info(f'[SUBMITTED] {label}: {tx_hash}')
            logger.info(f'  polygonscan: https://polygonscan.com/tx/{tx_hash}')
            self.telemetry.emit(
                'transaction_submitted',
                opportunity_id=candidate_id,
                submission_latency_ms=(time.time() - submission_started) * 1000.0,
                submission_path='private' if mev_protect.is_bloxroute_configured() else 'public',
                tx_hash=str(tx_hash),
                nonce=nonce,
                execution_result='SUBMITTED',
            )
        except Exception as e:
            logger.error(f'[EXEC ERROR] {label}: {_mask(str(e))}')
            self.telemetry.rejection(opp, 'SUBMISSION_FAILED', error=_mask(str(e)))
            if self.nonces:
                self.nonces.resync()

# ============================================================================
# MAIN LOOP
# ============================================================================

def _estimate_net_profit_usd(opp: dict, prices: dict,
                              gas_price_wei: Optional[int],
                              gas_units: int = GAS_LIMIT) -> Optional[float]:
    price_b      = prices.get(opp['sym_b'])
    price_native = prices.get(NATIVE_GAS_TOKEN_SYM)
    if price_b is None or price_native is None or gas_price_wei is None:
        return None
    economics = accounting.calculate_route_economics(
        route_profit_after_dex_fees_token=opp['gross_profit_b_human'],
        flashloan_amount_token=opp['dya_human'],
        flashloan_fee_bps=FLASHLOAN_FEE_BPS,
        gas_units=gas_units,
        gas_price_wei=gas_price_wei,
        native_usd=price_native,
        token_usd=price_b,
        dex_fees_token=opp.get('dex_fees_b_human', 0.0),
        expected_slippage_token=opp.get('expected_slippage_b_human', 0.0),
    )
    return economics.net_profit_usd


def _calculate_economics(
    opp: dict, prices: dict, gas_price_wei: Optional[int], gas_units: int
):
    price_b = prices.get(opp['sym_b'])
    price_native = prices.get(NATIVE_GAS_TOKEN_SYM)
    if price_b is None or price_native is None or gas_price_wei is None:
        return None
    opp['price_b_usd'] = price_b
    return accounting.calculate_route_economics(
        route_profit_after_dex_fees_token=opp['gross_profit_b_human'],
        flashloan_amount_token=opp['dya_human'],
        flashloan_fee_bps=FLASHLOAN_FEE_BPS,
        gas_units=gas_units,
        gas_price_wei=gas_price_wei,
        native_usd=price_native,
        token_usd=price_b,
        dex_fees_token=opp.get('dex_fees_b_human', 0.0),
        expected_slippage_token=opp.get('expected_slippage_b_human', 0.0),
    )


def run_once(
    rpc: Rpc,
    executor: Executor,
    telemetry: Optional[Telemetry] = None,
    oracle_monitor: Optional[OracleEdgeMonitor] = None,
):
    logger.info('--- Poll cycle start ---')
    telemetry = telemetry or getattr(executor, 'telemetry', None) or Telemetry()
    cycle_started = time.time()

    try:
        block_number = rpc.call(lambda: rpc.w3.eth.block_number)
        raw_candidates = rpc.call(pool_discovery.discover_pools, rpc.w3)
    except Exception as e:
        logger.error(f'Pool discovery failed: {_mask(str(e))}')
        return

    by_pair = {}
    for c in raw_candidates:
        by_pair.setdefault((c['sym_a'], c['sym_b']), []).append(c)
    confirmed_pairs = [dict(sym_a=k[0], sym_b=k[1], venues=v) for k, v in by_pair.items()]

    # ── V2 vs Uniswap V3 ─────────────────────────────────────────────────
    v2v3_opps = find_v2_v3_opportunities(confirmed_pairs)
    logger.info(f'V2/V3 scan: {len(confirmed_pairs)} pairs, '
                f'{len(v2v3_opps)} above-fee opportunities')

    # ── Algebra cross-venue ───────────────────────────────────────────────
    algebra_opps = find_algebra_opportunities(confirmed_pairs, rpc)
    if algebra_opps:
        logger.info(f'Algebra scan: {len(algebra_opps)} executable opportunities '
                    f'(Algebra router type 6 supported in contract)')
    else:
        logger.info('Algebra scan: 0 candidates clear fees this cycle')

    # ── Curve↔V2 cross-venue ─────────────────────────────────────────────
    curve_opps = find_curve_opportunities(raw_candidates, rpc)
    if curve_opps:
        logger.info(f'Curve scan: {len(curve_opps)} executable opportunities')
    else:
        logger.info('Curve scan: 0 opportunities this cycle')

    # Merge all executable opportunities
    opportunities = v2v3_opps + algebra_opps + curve_opps

    v2_raw = [c for c in raw_candidates if c['venue'] == 'v2_quickswap']
    prices = pool_discovery._build_usd_price_map(v2_raw)

    if oracle_monitor and ORACLE_EDGE_ENABLED:
        for finding in oracle_monitor.observe(
            rpc.w3,
            block_number=block_number,
            dex_prices=prices,
        ):
            finding['rejection_reason'] = 'RESEARCH_ONLY_UNVALIDATED'
            telemetry.emit(
                'oracle_transition_observed',
                strategy_type='oracle_transition',
                block_number=block_number,
                **finding,
            )
            logger.info(
                f'[RESEARCH ONLY] {finding["oracle_type"]} {finding["asset"]}: '
                f'new={finding["new_value"]:.8g}, '
                f'DEX deviation={finding["deviation_bps"]}'
            )

    if not opportunities:
        telemetry.emit(
            'cycle_completed',
            block_number=block_number,
            strategy_type='cross_dex',
            opportunities_detected=0,
            rejection_reason='NO_PRICE_DISLOCATION',
            cycle_latency_ms=(time.time() - cycle_started) * 1000.0,
        )
        logger.info('--- Poll cycle complete (no opportunities) ---')
        return

    try:
        gas_price_wei = rpc.call(lambda: rpc.w3.eth.gas_price)
    except Exception as e:
        gas_price_wei = None
        logger.warning(f'Gas price fetch failed — skipping execution: {_mask(str(e))}')

    # Best opportunity per pair (collapse fee-tier duplicates)
    best_per_pair = {}
    for opp in opportunities:
        label = f"{opp['sym_a']}/{opp['sym_b']} ({opp['cheap_kind']}→{opp['expensive_kind']})"
        _enrich_opportunity(opp, block_number=block_number, detected_at=cycle_started)
        telemetry.candidate_detected(opp)

        if 'curve' in (opp['cheap_kind'], opp['expensive_kind']):
            logger.info(f'[SKIP] {label}: Curve route is not validated for execution')
            telemetry.rejection(opp, 'UNVALIDATED_CURVE_ROUTE')
            continue
        ok, sanity_reason = route_sanity.validate_opportunity(
            {**opp, '_token_universe': pool_discovery.TOKEN_UNIVERSE},
            max_profit_bps=MAX_PROFIT_BPS,
            max_trade_usd=MAX_TRADE_USD,
        )
        if not ok:
            logger.info(f'[SKIP] {label}: route sanity rejected ({sanity_reason})')
            telemetry.rejection(opp, sanity_reason)
            continue
        if opp['route_hops'] > MAX_ROUTE_HOPS:
            telemetry.rejection(opp, 'ROUTE_HOPS_INVALID')
            continue
        if time.time() - opp['timestamp_detected'] > MAX_STATE_AGE:
            telemetry.rejection(opp, 'STALE_STATE')
            continue

        built = build_arb_calldata(opp)
        if not built:
            telemetry.rejection(opp, 'CALLDATA_BUILD_FAILED')
            continue
        if gas_price_wei is None:
            telemetry.rejection(opp, 'GAS_PRICE_UNAVAILABLE')
            continue
        try:
            estimated_gas = executor.estimate_gas(built['calldata'], gas_price_wei)
        except Exception as exc:
            logger.info(f'[SKIP] {label}: exact gas estimate failed ({_mask(str(exc))})')
            telemetry.rejection(opp, 'GAS_ESTIMATE_FAILED', error=_mask(str(exc)))
            continue
        execution_gas_price_wei = int(gas_price_wei * GAS_PRICE_MULTIPLIER)
        opp['estimated_gas'] = estimated_gas
        opp['gas_price_wei'] = execution_gas_price_wei
        economics = _calculate_economics(
            opp, prices, execution_gas_price_wei, estimated_gas
        )
        if economics is None:
            logger.info(f'[SKIP] {label}: cannot establish USD price — skipping defensively')
            telemetry.rejection(opp, 'USD_PRICE_UNAVAILABLE')
            continue
        opp['gas_cost_usd'] = economics.gas_cost_usd
        opp['net_profit_usd'] = economics.net_profit_usd
        if economics.gas_cost_usd > MAX_GAS_USD:
            logger.info(f'[SKIP] {label}: gas ~${economics.gas_cost_usd:.2f} exceeds cap')
            telemetry.rejection(opp, 'GAS_TOO_EXPENSIVE')
            continue
        if opp.get('expected_slippage_bps', 0.0) > MAX_SLIPPAGE_BPS:
            telemetry.rejection(opp, 'SLIPPAGE_TOO_HIGH')
            continue
        if economics.net_profit_usd < MIN_PROFIT_USD:
            logger.info(
                f'[SKIP] {label}: net ~${economics.net_profit_usd:.2f} '
                f'< threshold ${MIN_PROFIT_USD:.2f}'
            )
            telemetry.rejection(opp, 'NET_PROFIT_BELOW_THRESHOLD')
            continue
        key = (opp['sym_a'], opp['sym_b'], opp['cheap_kind'], opp['expensive_kind'])
        if key not in best_per_pair or economics.net_profit_usd > best_per_pair[key][1].net_profit_usd:
            best_per_pair[key] = (opp, economics, built)

    candidates = sorted(best_per_pair.values(), key=lambda t: t[1].net_profit_usd, reverse=True)
    if candidates:
        logger.info(f'{len(candidates)} candidate(s) clear net-profit gate '
                    f'(threshold ${MIN_PROFIT_USD:.2f}) — running eth_call simulation')

    for i, (opp, economics, built) in enumerate(candidates):
        executor.execute(opp, built, economics, economics.gas_price_wei)
        if i < len(candidates) - 1:
            time.sleep(SIM_STAGGER_S)

    logger.info('--- Poll cycle complete ---')


def main():
    logger.info('=' * 70)
    logger.info('PRODUCTION ARBITRAGE BOT starting')
    logger.info(f'Contract      : {CONTRACT_ADDRESS}')
    logger.info(f'Algebra router: {ALGEBRA_ROUTER}')
    logger.info(f'bloXroute     : {mev_protect.is_bloxroute_configured()}')
    logger.info(f'MIN_PROFIT_USD: ${MIN_PROFIT_USD:.2f}')
    logger.info(
        f'PAPER_MODE: {PAPER_MODE} | MAX_GAS_USD: ${MAX_GAS_USD:.2f} | '
        f'FLASHLOAN_FEE_BPS: {FLASHLOAN_FEE_BPS:.2f} | '
        f'MAX_ROUTE_HOPS: {MAX_ROUTE_HOPS}'
    )

    rpc = Rpc()
    blk = rpc.call(lambda: rpc.w3.eth.block_number)
    logger.info(f'Polygon block : {blk:,}')

    os.makedirs(os.path.dirname(CONTRACT_ABI_PATH), exist_ok=True)
    with open(CONTRACT_ABI_PATH) as f:
        abi = json.load(f)
    contract = rpc.w3.eth.contract(address=CONTRACT_ADDRESS, abi=abi)

    wallet = load_wallet()
    if wallet:
        logger.info(f'Wallet: {wallet.address}')
        bal = rpc.call(lambda: rpc.w3.eth.get_balance(wallet.address))
        logger.info(f'MATIC : {bal / 1e18:.4f}')
        # Confirm ownership
        try:
            owner = contract.functions.owner().call()
            if owner.lower() != wallet.address.lower():
                logger.error(
                    f'Wallet {wallet.address} is NOT contract owner ({owner}) — '
                    f'executeArbitrage (onlyOwner) will revert!'
                )
        except Exception:
            pass
    else:
        logger.warning('PRIVATE_KEY not set — DETECTION-ONLY mode')

    execution_enabled = (os.getenv('EXECUTION_ENABLED') or '').strip().lower() == 'true'
    logger.info(f'EXECUTION_ENABLED: {execution_enabled}')
    if not mev_protect.is_bloxroute_configured():
        logger.warning('No BLOXROUTE_AUTH_HEADER — will use PUBLIC mempool (no front-run protection)')

    telemetry = Telemetry()
    oracle_monitor = OracleEdgeMonitor(
        pool_discovery.TOKEN_UNIVERSE,
        poll_interval_s=ORACLE_POLL_INTERVAL_S,
    )
    executor = Executor(rpc, wallet, contract, telemetry)

    while True:
        cycle_start = time.time()
        try:
            run_once(rpc, executor, telemetry, oracle_monitor)
        except Exception as e:
            logger.error(f'Poll cycle error: {_mask(str(e))}', exc_info=True)
        elapsed = time.time() - cycle_start
        sleep   = max(0.0, POLL_INTERVAL_S - elapsed)
        logger.debug(f'Cycle took {elapsed:.1f}s, sleeping {sleep:.1f}s')
        time.sleep(sleep)


if __name__ == '__main__':
    main()
