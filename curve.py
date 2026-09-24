#!/usr/bin/env python3
"""
CURVE — Polygon discovery + pricing
======================================
Design decision, stated explicitly: this module does NOT reimplement
Curve's StableSwap D/y Newton's-method invariant locally. That math is
easy to get subtly wrong (rate-adjustment for lending-pool wrapped
tokens like amUSDC differs from plain pools, and a wrong reimplementation
would silently mis-size trades rather than error loudly). Two real bugs
this session already came from smaller mistakes than that would be
(token0/token1 ordering, a doubled RPC URL) — not repeating that pattern
here. Instead:

  - Discovery: query the real on-chain registry (via the AddressProvider,
    which is deployed at the SAME address on every chain Curve supports —
    verified via Curve's own docs, not a guessed Polygon-specific address).
  - Pricing: call the pool's own get_dy(i, j, dx) view function directly.
    Free (view call), exact, matches what execution would actually produce.
  - Sizing: numerical search over get_dy() calls, not a closed-form
    solve. Profit(dx) = value_out_on_other_venue(get_dy_curve(dx)) - dx
    is concave for a StableSwap pool (same shape argument as any AMM —
    monotonic increasing marginal cost), so ternary search converges
    correctly without needing the invariant math at all.

ADDRESS PROVIDER: 0x0000000022D53366457F9d5E68Ec105046FC4383 — documented
by Curve as immutable and identical across every deployment. This is the
verifiable entry point; nothing else here is hardcoded from memory.
"""

import logging
from typing import Optional

from web3 import Web3
from eth_abi import decode as eth_abi_decode

logger = logging.getLogger('Curve')

ADDRESS_PROVIDER = Web3.to_checksum_address('0x0000000022D53366457F9d5E68Ec105046FC4383')
MULTICALL3 = Web3.to_checksum_address('0xcA11bde05977b3631167028862bE2a173976CA11')

# Selectors verified via Web3.keccak this session, not guessed.
_SEL_GET_ADDRESS = bytes.fromhex('493f4f74')   # get_address(uint256)
_SEL_POOL_COUNT = bytes.fromhex('956aae3a')    # pool_count()
_SEL_POOL_LIST = bytes.fromhex('3a1d5d8e')     # pool_list(uint256)
_SEL_GET_COINS = bytes.fromhex('9ac90d3d')     # get_coins(address) -> address[8]
_SEL_GET_BALANCES = bytes.fromhex('92e3cc2d')  # get_balances(address) -> uint256[8]
_SEL_GET_DY = bytes.fromhex('5e0d443f')        # get_dy(int128,int128,uint256)

_MC_ABI = [{
    'name': 'aggregate3',
    'inputs': [{'components': [
        {'name': 'target', 'type': 'address'},
        {'name': 'allowFailure', 'type': 'bool'},
        {'name': 'callData', 'type': 'bytes'},
    ], 'name': 'calls', 'type': 'tuple[]'}],
    'outputs': [{'components': [
        {'name': 'success', 'type': 'bool'},
        {'name': 'returnData', 'type': 'bytes'},
    ], 'name': 'returnData', 'type': 'tuple[]'}],
    'stateMutability': 'view', 'type': 'function',
}]

ZERO_ADDRESS = '0x' + '00' * 20
MULTICALL_CHUNK_SIZE = 20  # same conservative floor as pool_discovery.py


def _aggregate3_chunked(w3: Web3, calls: list) -> list:
    mc = w3.eth.contract(address=MULTICALL3, abi=_MC_ABI)
    results = []
    for i in range(0, len(calls), MULTICALL_CHUNK_SIZE):
        chunk = calls[i:i + MULTICALL_CHUNK_SIZE]
        try:
            results.extend(mc.functions.aggregate3(chunk).call())
        except Exception as e:
            logger.warning(f'Curve multicall chunk failed: {e}')
            results.extend([(False, b'')] * len(chunk))
    return results


def get_registry_address(w3: Web3) -> Optional[str]:
    """id=0 on the AddressProvider is documented as the Main Registry."""
    calldata = _SEL_GET_ADDRESS + (0).to_bytes(32, 'big')
    try:
        result = w3.eth.call({'to': ADDRESS_PROVIDER, 'data': calldata})
        addr = Web3.to_checksum_address('0x' + result[-20:].hex())
        if addr.lower() == ZERO_ADDRESS:
            return None
        return addr
    except Exception as e:
        logger.error(f'Failed to fetch Curve registry from AddressProvider: {e}')
        return None


def discover_curve_pools(w3: Web3, token_universe: dict) -> list[dict]:
    """
    Enumerates real pools from the on-chain registry, keeps only pools
    where 2+ coins are in token_universe (symbol -> address dict, same
    shape as pool_discovery.py's TOKEN_UNIVERSE).

    Returns list of dicts: {pool_address, coins: [addr,...], balances:
    [int,...], matched_symbols: [sym,...], matched_indices: [i,...]}
    """
    registry = get_registry_address(w3)
    if not registry:
        logger.error('Could not resolve Curve registry — skipping Curve entirely')
        return []
    logger.info(f'Curve registry resolved: {registry}')

    count_result = w3.eth.call({'to': registry, 'data': _SEL_POOL_COUNT})
    pool_count = int.from_bytes(count_result[-32:], 'big')
    logger.info(f'Curve registry reports {pool_count} pools on Polygon')

    if pool_count == 0:
        return []

    list_calls = [
        (registry, True, _SEL_POOL_LIST + i.to_bytes(32, 'big'))
        for i in range(pool_count)
    ]
    list_results = _aggregate3_chunked(w3, list_calls)
    pool_addrs = []
    for ok, data in list_results:
        if ok and len(data) >= 32:
            addr = Web3.to_checksum_address('0x' + data[-20:].hex())
            if addr.lower() != ZERO_ADDRESS:
                pool_addrs.append(addr)

    logger.info(f'{len(pool_addrs)} non-zero pool addresses from registry')

    coins_calls = [(registry, True, _SEL_GET_COINS + Web3.to_bytes(hexstr=a).rjust(32, b'\x00'))
                   for a in pool_addrs]
    coins_results = _aggregate3_chunked(w3, coins_calls)

    balances_calls = [(registry, True, _SEL_GET_BALANCES + Web3.to_bytes(hexstr=a).rjust(32, b'\x00'))
                       for a in pool_addrs]
    balances_results = _aggregate3_chunked(w3, balances_calls)

    universe_by_addr = {addr.lower(): sym for sym, addr in token_universe.items()}

    matched_pools = []
    for pool_addr, (c_ok, c_data), (b_ok, b_data) in zip(pool_addrs, coins_results, balances_results):
        if not c_ok or len(c_data) < 32 * 8:
            continue
        coins = [
            Web3.to_checksum_address('0x' + c_data[i * 32 + 12:(i + 1) * 32].hex())
            for i in range(8)
        ]
        matched = [
            (i, universe_by_addr[c.lower()])
            for i, c in enumerate(coins)
            if c.lower() in universe_by_addr
        ]
        if len(matched) < 2:
            continue

        balances = [0] * 8
        if b_ok and len(b_data) >= 32 * 8:
            balances = [int.from_bytes(b_data[i * 32:(i + 1) * 32], 'big') for i in range(8)]

        matched_pools.append(dict(
            pool_address=pool_addr,
            coins=coins,
            balances=balances,
            matched=matched,  # list of (coin_index, symbol)
        ))

    logger.info(f'{len(matched_pools)} Curve pools involve 2+ tokens from TOKEN_UNIVERSE')
    return matched_pools


def get_dy_onchain(w3: Web3, pool_address: str, i: int, j: int, dx: int) -> Optional[int]:
    """Direct on-chain get_dy(i, j, dx) call — the pricing source of
    truth, not a local reimplementation."""
    calldata = _SEL_GET_DY + i.to_bytes(32, 'big', signed=True) + j.to_bytes(32, 'big', signed=True) + dx.to_bytes(32, 'big')
    try:
        result = w3.eth.call({'to': pool_address, 'data': calldata})
        return int.from_bytes(result[-32:], 'big')
    except Exception as e:
        logger.debug(f'get_dy failed on {pool_address} ({i}->{j}, dx={dx}): {e}')
        return None


def find_optimal_size_ternary(
    w3: Web3, pool_address: str, i: int, j: int,
    other_venue_price_fn,  # callable(amount_out_j) -> value received on the other venue, same units as dx
    dx_lo: int, dx_hi: int, iterations: int = 40,
) -> tuple[int, float]:
    """
    Ternary search for the profit-maximizing dx on the Curve leg.
    profit(dx) = other_venue_price_fn(get_dy_onchain(dx)) - dx
    Concave because get_dy_onchain is concave (standard AMM property —
    same monotonic-increasing-marginal-cost argument used throughout
    this project for CPMM/CLMM) and other_venue_price_fn is applied to
    a concave quantity through what should be another concave function
    on the far side; ternary search is the right tool here specifically
    because we deliberately avoided a closed-form solve.

    Returns (best_dx, best_profit). best_profit <= 0 means no real
    edge exists at current on-chain state — same "reject, don't force
    it" behavior as size_existing_spread elsewhere in this project.
    """
    def profit(dx: int) -> float:
        if dx <= 0:
            return -float('inf')
        dy = get_dy_onchain(w3, pool_address, i, j, dx)
        if dy is None:
            return -float('inf')
        return other_venue_price_fn(dy) - dx

    lo, hi = dx_lo, dx_hi
    for _ in range(iterations):
        if hi - lo < 2:
            break
        m1 = lo + (hi - lo) // 3
        m2 = hi - (hi - lo) // 3
        if profit(m1) < profit(m2):
            lo = m1
        else:
            hi = m2

    best_dx = (lo + hi) // 2
    return best_dx, profit(best_dx)


if __name__ == '__main__':
    import os
    from dotenv import load_dotenv
    from web3.middleware import ExtraDataToPOAMiddleware
    load_dotenv()

    logging.basicConfig(level=logging.INFO)

    alchemy_key = os.getenv('ALCHEMY_API_KEY', '').strip()
    if not alchemy_key:
        print('Set ALCHEMY_API_KEY.')
        raise SystemExit(1)
    rpc_url = alchemy_key if alchemy_key.startswith('http') else \
        f'https://polygon-mainnet.g.alchemy.com/v2/{alchemy_key}'

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 30}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

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

    pools = discover_curve_pools(w3, TOKEN_UNIVERSE)
    print(f'\nFound {len(pools)} Curve pools matching TOKEN_UNIVERSE:\n')
    for p in pools:
        syms = [s for _, s in p['matched']]
        print(f'{p["pool_address"]}: {syms}')
        for idx, sym in p['matched']:
            print(f'    [{idx}] {sym} balance={p["balances"][idx]}')