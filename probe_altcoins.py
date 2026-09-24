"""
One-off diagnostic: probe a candidate list of Polygon altcoins for QuickSwap
V2 + Uniswap V3 pool liquidity against the EXISTING TOKEN_UNIVERSE (so we
reuse the already-verified price graph for USD estimates).

Does NOT modify pool_discovery.py or TOKEN_UNIVERSE. Read-only eth_calls
only, no writes, no execution. Prints a report of candidates that have
"sensible" (not dust, not mega-cap-efficient) liquidity on both venues,
which is what would make a spread more likely to persist rather than be
instantly arbed away.

Run manually: `cd bot && python3 probe_altcoins.py`
"""
import logging
import os
from typing import Dict, List

from dotenv import load_dotenv
from web3 import Web3

import pool_discovery as pd

load_dotenv()
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('probe_altcoins')

# Candidate altcoins with historically real (non-dust) Polygon liquidity,
# addresses verified against the canonical QuickSwap Polygon token list.
CANDIDATES = {
    'SAND':    ('0xBbba073C31bF03b8ACf7c28EF0738DeCF3695683', 18),
    'UNI':     ('0xb33EaAd8d922B1083446DC23f610c2567fB5180f', 18),
    'stMATIC': ('0x3A58a54C066FdC0f2D55FC9C89F0415C92eBf3C4', 18),
    'MaticX':  ('0xfa68FB4628DFF1028CFEc22b4162FCcd0d45efb6', 18),
    'LDO':     ('0xC3C7d422809852031b44ab29EEC9F1EfF2A58756', 18),
    'GNS':     ('0xE5417Af564e4bFDA1c483642db72007871397896', 18),
    'OM':      ('0xC3Ec80343D2bae2F8E680FDADDe7C17E71E114ea', 18),
    'WOO':     ('0x1B815d120B3eF02039Ee11dC2d33DE7aA4a8C603', 18),
    'TEL':     ('0xdF7837DE1F2Fa4631D716CF2502f8b230F1dcc32', 2),
    'QUICK':   ('0xB5C064F955D8e7F38fE0460C556a72987494eE17', 18),
}

# "Sensible" liquidity band: enough depth that a real trade isn't 100% price
# impact, but not so deep that professional MEV bots have already competed
# the spread down to dust (which is what's happening on WETH/USDC/WBTC etc).
MIN_SENSIBLE_USD = 20_000
MAX_SENSIBLE_USD = 3_000_000


def main():
    rpc_url = pd.get_working_rpc_url()
    if not rpc_url:
        print('No working RPC URL found.')
        return
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 30}))

    # Extend the universe used ONLY for this probe run (local dict, not the
    # module-level pool_discovery.TOKEN_UNIVERSE — that stays untouched).
    extended_universe = dict(pd.TOKEN_UNIVERSE)
    extended_decimals = dict(pd.TOKEN_DECIMALS)
    for sym, (addr, dec) in CANDIDATES.items():
        extended_universe[sym] = Web3.to_checksum_address(addr)
        extended_decimals[sym] = dec

    orig_universe, orig_decimals = pd.TOKEN_UNIVERSE, pd.TOKEN_DECIMALS
    pd.TOKEN_UNIVERSE = extended_universe
    pd.TOKEN_DECIMALS = extended_decimals
    try:
        pools = pd.discover_pools(w3)
    finally:
        pd.TOKEN_UNIVERSE = orig_universe
        pd.TOKEN_DECIMALS = orig_decimals

    by_pair: Dict[tuple, List[Dict]] = {}
    for c in pools:
        by_pair.setdefault((c['sym_a'], c['sym_b']), []).append(c)

    print(f'\n{len(by_pair)} pairs passed the 2-venue + liquidity-floor filter '
          f'(existing universe + {len(CANDIDATES)} candidates)\n')

    candidate_syms = set(CANDIDATES)
    interesting = []
    for (sym_a, sym_b), entries in sorted(by_pair.items()):
        if sym_a not in candidate_syms and sym_b not in candidate_syms:
            continue  # already-known major pair, not what we're looking for
        venues = sorted(set(e['venue'] for e in entries))
        v2_entry = next((e for e in entries if e['venue'] == 'v2_quickswap'), None)
        tvl = v2_entry.get('reserve_usd_est', 0) if v2_entry else None
        sensible = tvl is not None and MIN_SENSIBLE_USD <= tvl <= MAX_SENSIBLE_USD
        flag = '<-- SENSIBLE RANGE' if sensible else ''
        tvl_str = f'~${tvl:,.0f}' if tvl is not None else 'no V2 pool (V3-only, no USD est.)'
        print(f'{sym_a}/{sym_b:8s} venues={venues!s:45s} v2_tvl={tvl_str} {flag}')
        if sensible:
            interesting.append((sym_a, sym_b, tvl, venues))

    print('\n--- Candidates worth adding to TOKEN_UNIVERSE ---')
    if not interesting:
        print('None of the probed altcoins cleared both the liquidity floor '
              'AND had confirmed 2-venue (V2+V3) presence in the sensible '
              f'${MIN_SENSIBLE_USD:,}-${MAX_SENSIBLE_USD:,} TVL band.')
    else:
        for sym_a, sym_b, tvl, venues in sorted(interesting, key=lambda x: x[2]):
            print(f'  {sym_a}/{sym_b}: ~${tvl:,.0f} TVL, venues={venues}')


if __name__ == '__main__':
    main()
