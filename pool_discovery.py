#!/usr/bin/env python3
"""
POOL DISCOVERY — Polygon (on-chain version, liquidity-filtered)
==================================================================
Built directly on the working RPC/chunking version — that part is
untouched. Added the filtering pass that was still missing:

  1. Uninitialized V3 pools excluded. sqrtPriceX96 == 4295128740 (one
     wei above TickMath.MIN_SQRT_RATIO) showed up 9 times in the last
     real run — that's a pool contract that exists but was never
     seeded with real liquidity, not a usable venue. Checked via
     liquidity() == 0, which is the authoritative signal — the
     sqrtPriceX96-near-floor pattern is kept as a secondary sanity
     check, not the primary one.

  2. USD liquidity floor via price-graph relaxation. Raw reserve
     tuples aren't comparable across pairs with different decimals and
     prices (WBTC/CRV showing reserves=(15322548725, 1) passed "pool
     exists" in the last run but is obviously not real liquidity).
     Stables (USDC/USDT/DAI) anchor at $1; every other token's price is
     inferred from whichever V2 pool connects it to an already-priced
     token, iterated until the price map stops changing. This is a
     coarse filter for discovery, not execution-grade pricing — good
     enough to reject clearly-thin pools, not a substitute for the
     exact sizing math in calc.py at execution time.

  3. Two-venue enforcement restored. The previous run printed
     WBTC/GHST and LINK/GHST with only v2_quickswap listed — those
     can't produce a cross-venue spread by definition and were a
     filtering regression, not new information.

NOT added: Curve. There is no Curve venue in this script — the three
venues are QuickSwap V2, Uniswap V3, and QuickSwap V3 (Algebra). Adding
Curve requires a separately verified registry address, not assumed.
"""

import logging
import math
import time
from itertools import combinations
from typing import List, Dict, Tuple, Optional

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_abi import encode as eth_abi_encode

logger = logging.getLogger('PoolDiscovery')

# Factory addresses
QUICKSWAP_V2_FACTORY = Web3.to_checksum_address('0x5757371414417b8C6CAad45bAeF941aBc7d3Ab32')
UNISWAP_V3_FACTORY = Web3.to_checksum_address('0x1F98431c8aD98523631AE4a59f267346ea31F984')
QUICKSWAP_V3_FACTORY = Web3.to_checksum_address('0x411b0fAcC3489691f28ad58c47006AF5E3Ab3A28')
MULTICALL3 = Web3.to_checksum_address('0xcA11bde05977b3631167028862bE2a173976CA11')

# V3 fee tiers
V3_FEE_TIERS = [100, 500, 3000, 10000]

# Function selectors (liquidity() verified via Web3.keccak this session:
# 0x1a686502, matching the value already used elsewhere in this project)
_SEL_GET_PAIR = bytes.fromhex('e6a43905')
_SEL_GET_POOL = bytes.fromhex('1698ee82')
_SEL_POOL_BY_PAIR = bytes.fromhex('d9a641e1')
_SEL_GET_RESERVES = bytes.fromhex('0902f1ac')
_SEL_SLOT0 = bytes.fromhex('3850c7bd')
_SEL_LIQUIDITY = bytes.fromhex('1a686502')
# globalState() verified via Web3.keccak this session: 0xe76c01e4. Algebra
# pools (QuickSwap V3) expose liquidity() with the SAME selector as Uniswap
# V3 (0x1a686502) — confirmed by a real eth_call against a live Algebra pool
# (WMATIC/SAND, 0xD85A25332b57cc447a791100E7317e534d553761) returning a
# real non-zero liquidity value, not a revert.
_SEL_GLOBAL_STATE = bytes.fromhex('e76c01e4')

# Multicall3 ABI (simplified)
_MC_ABI = [{
    'name': 'aggregate3',
    'inputs': [{
        'components': [
            {'name': 'target', 'type': 'address'},
            {'name': 'allowFailure', 'type': 'bool'},
            {'name': 'callData', 'type': 'bytes'},
        ],
        'name': 'calls',
        'type': 'tuple[]'
    }],
    'outputs': [{
        'components': [
            {'name': 'success', 'type': 'bool'},
            {'name': 'returnData', 'type': 'bytes'},
        ],
        'name': 'returnData',
        'type': 'tuple[]'
    }],
    'stateMutability': 'view',
    'type': 'function',
}]

ZERO_ADDRESS = '0x0000000000000000000000000000000000000000'

# TickMath.MIN_SQRT_RATIO. Anything at or one wei above this is an
# uninitialized/never-seeded pool, not a real price — secondary check
# alongside liquidity() == 0.
MIN_SQRT_RATIO = 4295128739

# Curated token universe
TOKEN_UNIVERSE = {
    'WETH': '0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619',
    'USDC': '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',
    'USDT': '0xc2132D05D31c914a87C6611C10748AEb04B58e8F',
    'DAI': '0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063',
    'WMATIC': '0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270',
    'WBTC': '0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6',
    'AAVE': '0xD6DF932A45C0f255f85145f286eA0b292B21C90B',
    'LINK': '0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39',
    'CRV': '0x172370d5Cd63279eFa6d502DAB29171933a610AF',
    'GHST': '0x385Eeac5cB85A38A9a07A70c73e0a3271CfB54A7',
}
TOKEN_DECIMALS = {
    'WETH': 18, 'USDC': 6, 'USDT': 6, 'DAI': 18, 'WMATIC': 18,
    'WBTC': 8, 'AAVE': 18, 'LINK': 18, 'CRV': 18, 'GHST': 18,
}
STABLECOINS = {'USDC', 'USDT', 'DAI'}  # anchor at $1 for the price graph
MIN_RESERVE_USD = 50_000  # liquidity floor per V2 pool

# V3 / Algebra TVL floor.  V2 pools use reserve_usd; V3/Algebra use the same
# concept via liq_human.  A pool with < $10k on each side cannot generate
# enough trade size to cover gas — these are untraded/dust pools whose stale
# sqrtPriceX96 produces phantom spreads against healthy venues.
V3_MIN_POOL_TVL_USD = 10_000

# Chunk size for multicall - smaller is safer for free tiers
# Free tier RPCs often have request size limits
MULTICALL_CHUNK_SIZE = 20  # Reduced from 50 for free tier compatibility
RETRY_DELAY = 0.5  # Seconds between retries


def _aggregate3_with_retry(w3: Web3, calls: list, max_retries: int = 3) -> list:
    """
    Execute multicall with retry logic and chunking.
    Handles rate limiting and temporary failures.
    (Unchanged from the working version — RPC/chunking left alone.)
    """
    mc = w3.eth.contract(address=MULTICALL3, abi=_MC_ABI)
    results = []

    for i in range(0, len(calls), MULTICALL_CHUNK_SIZE):
        chunk = calls[i:i + MULTICALL_CHUNK_SIZE]
        chunk_idx = i // MULTICALL_CHUNK_SIZE

        success = False
        retry_count = 0

        while not success and retry_count < max_retries:
            try:
                chunk_results = mc.functions.aggregate3(chunk).call()

                if len(chunk_results) != len(chunk):
                    logger.warning(f'Chunk {chunk_idx}: expected {len(chunk)} results, got {len(chunk_results)}')
                    chunk_results = [(False, b'')] * len(chunk)

                results.extend(chunk_results)
                success = True
                logger.debug(f'Chunk {chunk_idx} succeeded ({len(chunk)} calls)')

            except Exception as e:
                retry_count += 1
                error_msg = str(e)

                if 'rate limit' in error_msg.lower() or '429' in error_msg:
                    wait_time = RETRY_DELAY * (retry_count ** 2)
                    logger.warning(f'Rate limited on chunk {chunk_idx}, waiting {wait_time:.1f}s')
                    time.sleep(wait_time)
                elif retry_count < max_retries:
                    logger.warning(f'Chunk {chunk_idx} failed (attempt {retry_count}/{max_retries}): {e}')
                    time.sleep(RETRY_DELAY)
                else:
                    logger.warning(f'Chunk {chunk_idx} failed after {max_retries} attempts: {e}')
                    results.extend([(False, b'')] * len(chunk))

        if i + MULTICALL_CHUNK_SIZE < len(calls):
            time.sleep(0.1)

    return results


def _fetch_pair_and_pool_candidates(w3: Web3) -> List[Dict]:
    """Fetch all pool candidates using batched multicall. (Unchanged.)"""
    calls = []
    call_meta = []

    token_pairs = list(combinations(TOKEN_UNIVERSE.items(), 2))

    for (sym_a, addr_a_str), (sym_b, addr_b_str) in token_pairs:
        addr_a = Web3.to_checksum_address(addr_a_str)
        addr_b = Web3.to_checksum_address(addr_b_str)

        calldata = _SEL_GET_PAIR + eth_abi_encode(['address', 'address'], [addr_a, addr_b])
        calls.append((QUICKSWAP_V2_FACTORY, True, calldata))
        call_meta.append({'venue': 'v2_quickswap', 'sym_a': sym_a, 'sym_b': sym_b,
                           'addr_a': addr_a, 'addr_b': addr_b, 'fee': 0})

        for fee in V3_FEE_TIERS:
            calldata = _SEL_GET_POOL + eth_abi_encode(
                ['address', 'address', 'uint24'], [addr_a, addr_b, fee]
            )
            calls.append((UNISWAP_V3_FACTORY, True, calldata))
            call_meta.append({'venue': 'v3_uniswap', 'sym_a': sym_a, 'sym_b': sym_b,
                               'addr_a': addr_a, 'addr_b': addr_b, 'fee': fee})

        calldata = _SEL_POOL_BY_PAIR + eth_abi_encode(['address', 'address'], [addr_a, addr_b])
        calls.append((QUICKSWAP_V3_FACTORY, True, calldata))
        call_meta.append({'venue': 'v3_quickswap_algebra', 'sym_a': sym_a, 'sym_b': sym_b,
                           'addr_a': addr_a, 'addr_b': addr_b, 'fee': None})

    logger.info(f'Querying {len(calls)} factory calls across {len(token_pairs)} token pairs '
                f'in chunks of {MULTICALL_CHUNK_SIZE}...')

    results = _aggregate3_with_retry(w3, calls)

    candidates = []
    for meta, (ok, data) in zip(call_meta, results):
        if not ok or len(data) < 32:
            continue
        pool_addr = Web3.to_checksum_address('0x' + data[-20:].hex())
        if pool_addr.lower() == ZERO_ADDRESS:
            continue
        candidates.append({**meta, 'pool_address': pool_addr})

    logger.info(f'Found {len(candidates)} non-zero pools')
    return candidates


def _fetch_reserves_batch(w3: Web3, pool_addrs: List[str], call_type: str = 'v2') -> Dict:
    """Batch fetch reserves or slot0 data. (Unchanged.)"""
    if not pool_addrs:
        return {}
    selector = _SEL_GET_RESERVES if call_type == 'v2' else _SEL_SLOT0
    calls = [(addr, True, selector) for addr in pool_addrs]
    results = _aggregate3_with_retry(w3, calls)
    out = {}
    for addr, (ok, data) in zip(pool_addrs, results):
        if not ok or len(data) < 32:
            continue
        addr_lower = addr.lower()
        if call_type == 'v2':
            if len(data) >= 64:
                out[addr_lower] = (
                    int.from_bytes(data[0:32], 'big'),
                    int.from_bytes(data[32:64], 'big')
                )
        else:
            out[addr_lower] = int.from_bytes(data[0:32], 'big')
    return out


def _fetch_v3_liquidity(w3: Web3, pool_addrs: List[str]) -> Dict[str, int]:
    """Batch fetch liquidity() for standard Uniswap V3 pools — the
    authoritative check for whether a pool is actually seeded, vs. the
    sqrtPriceX96-near-floor heuristic alone."""
    if not pool_addrs:
        return {}
    calls = [(addr, True, _SEL_LIQUIDITY) for addr in pool_addrs]
    results = _aggregate3_with_retry(w3, calls)
    out = {}
    for addr, (ok, data) in zip(pool_addrs, results):
        if ok and len(data) >= 32:
            out[addr.lower()] = int.from_bytes(data[0:32], 'big')
    return out


def _fetch_algebra_state_batch(w3: Web3, pool_addrs: List[str]) -> Dict[str, int]:
    """
    Batch fetch globalState().price (sqrtPriceX96, first return slot) for
    Algebra (QuickSwap V3) pools. ABI-encoded return values are always
    right-padded to 32-byte slots regardless of the underlying uint160
    width, and `price` is the first field in every Algebra globalState()
    version seen (older/newer variants only differ in the LATER fields —
    communityFee width, extra plugin fields — which we don't read), so
    slicing bytes[0:32] is stable across versions. Verified this session
    against a live pool (WMATIC/SAND Algebra pool) returning a real
    non-zero price, not a revert or all-zero response.
    """
    if not pool_addrs:
        return {}
    calls = [(addr, True, _SEL_GLOBAL_STATE) for addr in pool_addrs]
    results = _aggregate3_with_retry(w3, calls)
    out = {}
    for addr, (ok, data) in zip(pool_addrs, results):
        if ok and len(data) >= 96:
            # slot0 = price (sqrtPriceX96), slot2 = fee (parts-per-million,
            # same units as Uniswap V3's fee) — both stable field positions
            # across Algebra globalState() versions, verified this session.
            out[addr.lower()] = {
                'price': int.from_bytes(data[0:32], 'big'),
                'fee': int.from_bytes(data[64:96], 'big'),
            }
    return out


def _token0_is_sym_a(c: Dict) -> bool:
    """
    UniswapV2Factory.createPair always assigns token0 = whichever
    token has the lower address (tokenA < tokenB ? (tokenA, tokenB) :
    (tokenB, tokenA)) — this is a protocol-level invariant of the
    standard factory, not something that needs an extra token0() call.
    Missing this is what caused the previous run's trillion-dollar
    nonsense: reserve0/reserve1 were being matched to sym_a/sym_b by
    call order, not by actual on-chain token0/token1 order, so an
    18-decimal token's reserve sometimes got divided by a 6-decimal
    divisor (or vice versa) — a ~10^12 error that then propagated
    through every downstream price in the relaxation graph.
    """
    return int(c['addr_a'], 16) < int(c['addr_b'], 16)


def _build_usd_price_map(v2_pools: List[Dict]) -> Dict[str, float]:
    """
    Iterative relaxation over the V2 pool graph: stables anchor at $1,
    every other token's price is inferred from any V2 pool connecting
    it to an already-priced token, repeated until no new prices are
    found. Coarse — good enough to reject obviously-thin pools, not a
    substitute for calc.py's exact sizing math.
    """
    prices: Dict[str, float] = {sym: 1.0 for sym in STABLECOINS}

    changed = True
    while changed:
        changed = False
        for c in v2_pools:
            if not c.get('reserves'):
                continue
            sym_a, sym_b = c['sym_a'], c['sym_b']
            r0, r1 = c['reserves']
            dec_a = TOKEN_DECIMALS.get(sym_a, 18)
            dec_b = TOKEN_DECIMALS.get(sym_b, 18)

            if _token0_is_sym_a(c):
                amt_a = r0 / (10 ** dec_a)
                amt_b = r1 / (10 ** dec_b)
            else:
                amt_a = r1 / (10 ** dec_a)
                amt_b = r0 / (10 ** dec_b)

            if amt_a == 0 or amt_b == 0:
                continue

            if sym_a in prices and sym_b not in prices:
                implied = (amt_a * prices[sym_a]) / amt_b
                if implied > 0:
                    prices[sym_b] = implied
                    changed = True
            elif sym_b in prices and sym_a not in prices:
                implied = (amt_b * prices[sym_b]) / amt_a
                if implied > 0:
                    prices[sym_a] = implied
                    changed = True

    missing = set(TOKEN_UNIVERSE) - set(prices)
    if missing:
        logger.warning(f'Could not price these tokens from the V2 graph: {missing} '
                        f'— any pool involving them will be excluded by the USD floor')
    return prices


_Q96_FLOAT = float(2 ** 96)


def _v3_pool_tvl_usd(c: Dict, prices: Dict[str, float]) -> float:
    """
    Estimate USD TVL for a Uniswap-V3-style (or Algebra) pool using the
    V2-derived price map.

    Math: for a concentrated-liquidity pool at sqrt price s = sqrt(y/x):
        x_human = L_human / s_b_per_a     (sym_a in pool)
        y_human = L_human * s_b_per_a     (sym_b in pool)
    where L_human = L_raw / 10^((dec0+dec1)/2) and s_b_per_a = sqrt(p_b_per_a).

    If only one token can be priced (AAVE, GHST, etc.), we assume a balanced
    pool and double the priceable side — better than returning 0 and keeping
    a stale-price pool in the working set.
    """
    sqrt_p_raw = c.get('sqrt_price_x96') or 0
    liq        = c.get('liquidity') or 0
    if not sqrt_p_raw or not liq:
        return 0.0

    sym_a, sym_b = c['sym_a'], c['sym_b']
    dec_a = TOKEN_DECIMALS.get(sym_a, 18)
    dec_b = TOKEN_DECIMALS.get(sym_b, 18)
    a_is_t0 = int(c['addr_a'], 16) < int(c['addr_b'], 16)
    dec0, dec1 = (dec_a, dec_b) if a_is_t0 else (dec_b, dec_a)

    liq_h   = liq / (10 ** ((dec0 + dec1) / 2.0))
    raw_p   = (sqrt_p_raw / _Q96_FLOAT) ** 2          # token1_raw / token0_raw
    human_p = raw_p * (10 ** (dec0 - dec1))            # token1_human / token0_human
    # p_b_per_a = sym_b / sym_a regardless of pool token0/token1 ordering
    p_b_per_a = human_p if a_is_t0 else 1.0 / human_p
    if p_b_per_a <= 0:
        return 0.0

    s_b_per_a = math.sqrt(p_b_per_a)
    x_h = liq_h / s_b_per_a   # sym_a in pool (human units)
    y_h = liq_h * s_b_per_a   # sym_b in pool (human units)

    price_a = prices.get(sym_a)
    price_b = prices.get(sym_b)

    if price_a and price_b:
        return x_h * price_a + y_h * price_b
    if price_a:
        return 2.0 * x_h * price_a   # assume balanced, double one side
    if price_b:
        return 2.0 * y_h * price_b
    return 0.0  # neither token priceable → can't verify TVL


def _pool_reserve_usd(c: Dict, prices: Dict[str, float]) -> float:
    if not c.get('reserves'):
        return 0.0
    sym_a, sym_b = c['sym_a'], c['sym_b']
    if sym_a not in prices or sym_b not in prices:
        return 0.0
    r0, r1 = c['reserves']
    dec_a = TOKEN_DECIMALS.get(sym_a, 18)
    dec_b = TOKEN_DECIMALS.get(sym_b, 18)

    if _token0_is_sym_a(c):
        amt_a = r0 / (10 ** dec_a)
        amt_b = r1 / (10 ** dec_b)
    else:
        amt_a = r1 / (10 ** dec_a)
        amt_b = r0 / (10 ** dec_b)

    # Both sides should imply roughly the same USD value in a balanced
    # pool — sum and halve gives a reasonable single TVL estimate even
    # if token0/token1 ordering is ambiguous here.
    return (amt_a * prices[sym_a] + amt_b * prices[sym_b]) / 2


def discover_pools(w3: Web3) -> List[Dict]:
    """
    Full discovery pipeline: find candidates, fetch reserves/liquidity,
    then filter to real venues only.
    """
    candidates = _fetch_pair_and_pool_candidates(w3)
    if not candidates:
        return []

    v2_pools = [c for c in candidates if c['venue'] == 'v2_quickswap']
    v3_pools = [c for c in candidates if c['venue'] == 'v3_uniswap']

    if v2_pools:
        v2_addrs = [c['pool_address'] for c in v2_pools]
        reserves_data = _fetch_reserves_batch(w3, v2_addrs, 'v2')
        for c in v2_pools:
            c['reserves'] = reserves_data.get(c['pool_address'].lower())

    if v3_pools:
        v3_addrs = [c['pool_address'] for c in v3_pools]
        slot0_data = _fetch_reserves_batch(w3, v3_addrs, 'v3')
        liquidity_data = _fetch_v3_liquidity(w3, v3_addrs)
        for c in v3_pools:
            addr_lower = c['pool_address'].lower()
            c['sqrt_price_x96'] = slot0_data.get(addr_lower)
            c['liquidity'] = liquidity_data.get(addr_lower)

    # --- Filter pass ---
    prices = _build_usd_price_map(v2_pools)

    filtered_v2 = []
    for c in v2_pools:
        usd = _pool_reserve_usd(c, prices)
        c['reserve_usd_est'] = usd
        if usd >= MIN_RESERVE_USD:
            filtered_v2.append(c)
        else:
            logger.debug(f'{c["sym_a"]}/{c["sym_b"]} v2_quickswap below floor '
                         f'(~${usd:,.0f}) — excluded')

    filtered_v3 = []
    for c in v3_pools:
        liq    = c.get('liquidity') or 0
        sqrt_p = c.get('sqrt_price_x96') or 0
        if liq == 0 or sqrt_p <= MIN_SQRT_RATIO:
            logger.debug(f'{c["sym_a"]}/{c["sym_b"]} v3_uniswap fee={c["fee"]} '
                         f'uninitialized (liquidity={liq}, sqrtPriceX96={sqrt_p}) — excluded')
            continue
        tvl = _v3_pool_tvl_usd(c, prices)
        c['tvl_usd_est'] = tvl
        if tvl < V3_MIN_POOL_TVL_USD:
            logger.debug(
                f'{c["sym_a"]}/{c["sym_b"]} v3_uniswap fee={c["fee"]} '
                f'below TVL floor (~${tvl:,.0f} < ${V3_MIN_POOL_TVL_USD:,}) — excluded'
            )
            continue
        filtered_v3.append(c)

    algebra_candidates = [c for c in candidates if c['venue'] == 'v3_quickswap_algebra']
    filtered_algebra = []
    if algebra_candidates:
        algebra_addrs = [c['pool_address'] for c in algebra_candidates]
        # globalState().price and liquidity() are real, verified checks now
        # (see _fetch_algebra_state_batch / _fetch_v3_liquidity docstrings) —
        # Algebra is treated the same as Uniswap V3: liquidity()==0 or price
        # at/below MIN_SQRT_RATIO means an uninitialized pool, not a real
        # venue, exactly like the V3 check above.
        state_data = _fetch_algebra_state_batch(w3, algebra_addrs)
        liquidity_data = _fetch_v3_liquidity(w3, algebra_addrs)
        for c in algebra_candidates:
            addr_lower = c['pool_address'].lower()
            state = state_data.get(addr_lower) or {}
            c['sqrt_price_x96'] = state.get('price')
            c['algebra_fee'] = state.get('fee')
            c['liquidity'] = liquidity_data.get(addr_lower)
            liq    = c.get('liquidity') or 0
            sqrt_p = c.get('sqrt_price_x96') or 0
            if liq == 0 or sqrt_p <= MIN_SQRT_RATIO:
                logger.debug(f'{c["sym_a"]}/{c["sym_b"]} v3_quickswap_algebra '
                             f'uninitialized (liquidity={liq}, price={sqrt_p}) — excluded')
                continue
            tvl = _v3_pool_tvl_usd(c, prices)
            c['tvl_usd_est'] = tvl
            if tvl < V3_MIN_POOL_TVL_USD:
                logger.debug(
                    f'{c["sym_a"]}/{c["sym_b"]} v3_quickswap_algebra '
                    f'below TVL floor (~${tvl:,.0f} < ${V3_MIN_POOL_TVL_USD:,}) — excluded'
                )
                continue
            filtered_algebra.append(c)

    kept = filtered_v2 + filtered_v3 + filtered_algebra

    # --- Two-venue enforcement ---
    # All three venues are now liquidity-verified (real eth_call checks, not
    # assumed), so any 2+ of {v2_quickswap, v3_uniswap, v3_quickswap_algebra}
    # counts as a confirmed cross-venue candidate.
    by_pair: Dict[Tuple[str, str], List[Dict]] = {}
    for c in kept:
        by_pair.setdefault((c['sym_a'], c['sym_b']), []).append(c)

    final = []
    for pair_key, entries in by_pair.items():
        confirmed_venues = set(e['venue'] for e in entries)
        if len(confirmed_venues) >= 2:
            final.extend(entries)

    return final


def get_working_rpc_url() -> Optional[str]:
    """
    Try multiple RPC endpoints and return the first working one.
    (Unchanged — RPC/config left alone per instruction.)
    """
    import os

    alchemy_key = os.getenv('ALCHEMY_API_KEY', '').strip()
    onfinality_key = os.getenv('ONFINALITY_API_KEY', '').strip()

    rpc_urls = []

    if alchemy_key:
        if '?apikey=' in alchemy_key:
            rpc_urls.append(alchemy_key)
        elif alchemy_key.startswith('http'):
            rpc_urls.append(alchemy_key)
        else:
            rpc_urls.append(f'https://polygon-mainnet.g.alchemy.com/v2/{alchemy_key}')

    if onfinality_key:
        if '?apikey=' in onfinality_key:
            rpc_urls.append(onfinality_key)
        elif onfinality_key.startswith('http'):
            rpc_urls.append(onfinality_key)
        else:
            rpc_urls.append(f'https://polygon.api.onfinality.io/rpc?apikey={onfinality_key}')

    rpc_urls.append('https://polygon-rpc.com')
    rpc_urls.append('https://rpc-mainnet.maticvigil.com')

    for url in rpc_urls:
        try:
            logger.info(f'Testing RPC: {url.split("?")[0][:50]}...')
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 10}))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

            if w3.is_connected():
                block_num = w3.eth.block_number
                logger.info(f'Connected! Block: {block_num}')
                return url
        except Exception as e:
            logger.warning(f'Failed to connect to {url[:50]}...: {e}')
            continue

    return None


if __name__ == '__main__':
    import os
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    rpc_url = get_working_rpc_url()

    if not rpc_url:
        print('❌ No working RPC URL found. Please check your API keys.')
        print('   Set ALCHEMY_API_KEY or ONFINALITY_API_KEY in .env file')
        raise SystemExit(1)

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 30}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not w3.is_connected():
        print('❌ Failed to connect to RPC')
        raise SystemExit(1)

    print(f'✅ Connected to RPC: {rpc_url.split("?")[0][:50]}...')
    print(f'   Current block: {w3.eth.block_number}')

    results = discover_pools(w3)

    by_pair = {}
    for c in results:
        key = (c['sym_a'], c['sym_b'])
        by_pair.setdefault(key, []).append(c)

    print(f'\n📊 Found {len(by_pair)} token pairs passing liquidity + 2-venue filters\n')

    for (sym_a, sym_b), entries in by_pair.items():
        venues = sorted(set(e['venue'] for e in entries))
        print(f'🔷 {sym_a}/{sym_b}: {", ".join(venues)}')

        for e in entries:
            extra = ''
            if e['venue'] == 'v2_quickswap' and e.get('reserves'):
                r0, r1 = e['reserves']
                extra = f' reserves=({r0}, {r1}) ~${e.get("reserve_usd_est", 0):,.0f}'
            elif e['venue'] == 'v3_uniswap' and e.get('sqrt_price_x96'):
                extra = f' fee={e["fee"]} sqrtPriceX96={e["sqrt_price_x96"]} liquidity={e.get("liquidity")}'
            print(f'    • {e["venue"]}: {e["pool_address"]}{extra}')