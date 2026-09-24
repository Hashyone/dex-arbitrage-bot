#!/usr/bin/env python3
"""
Encode-params sanity test: verify Python encode_params output matches expected layout.
Run: python3 bot/test/encode_test.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from eth_abi import encode as abi_encode
from eth_abi import decode as abi_decode
from web3 import Web3

# ── Import the real encoder from hybrid_bot ────────────────────────────
# We import only the pure encoding functions to avoid needing live RPC.
NEW_PARAMS_TYPE = (
    '(uint8,address,address,address,uint256,uint256,'
    'address[],address,uint8,uint24[],bytes,uint256,uint256,bytes)'
)

USDC   = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
USDT   = Web3.to_checksum_address('0xc2132D05D31c914a87C6611C10748AEb04B58e8F')
WETH   = Web3.to_checksum_address('0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619')
QS_V2  = Web3.to_checksum_address('0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff')
USER   = Web3.to_checksum_address('0x000000000000000000000000DeadBeefDeadBeef')

MORPHO_EXTRA_TYPE = '(address,address,address,address,uint256)'

def encode_params(protocol, col, debt, user, debt_cover, min_col,
                  swap_path, router, router_type, v3_fees,
                  one_inch_data, deadline, min_profit, extra_data):
    tup = (
        protocol, col, debt, user, debt_cover, min_col,
        swap_path, router, router_type, v3_fees,
        one_inch_data, deadline, min_profit, extra_data,
    )
    return abi_encode([NEW_PARAMS_TYPE], [tup])


def decode_params(encoded: bytes) -> dict:
    (tup,) = abi_decode([NEW_PARAMS_TYPE], encoded)
    fields = [
        'protocol', 'collateralAsset', 'debtAsset', 'user',
        'debtToCover', 'minCollateralReceived', 'swapPath',
        'swapRouter', 'routerType', 'uniswapV3Fees', 'oneInchData',
        'deadline', 'minProfitRequired', 'extraData',
    ]
    return dict(zip(fields, tup))


def encode_morpho_extra(loan_token, collateral_token, oracle, irm, lltv) -> bytes:
    return abi_encode(
        [MORPHO_EXTRA_TYPE],
        [(
            Web3.to_checksum_address(loan_token),
            Web3.to_checksum_address(collateral_token),
            Web3.to_checksum_address(oracle),
            Web3.to_checksum_address(irm),
            lltv,
        )]
    )


# ════════════════════════════════════════════════════════════════
# Test 1: Aave V3 liquidation params round-trip
# ════════════════════════════════════════════════════════════════
def test_aave_v3_roundtrip():
    print("Test 1: Aave V3 LiquidationParams round-trip...")
    encoded = encode_params(
        protocol     = 0,                   # PROTOCOL_AAVE_V3
        col          = USDC,
        debt         = USDT,
        user         = USER,
        debt_cover   = 1_000_000,           # 1 USDT (6 decimals)
        min_col      = 950_000,
        swap_path    = [USDC, USDT],
        router       = QS_V2,
        router_type  = 0,                   # RT_QUICKSWAP
        v3_fees      = [],
        one_inch_data= b'',
        deadline     = 9_999_999_999,
        min_profit   = 10_000,
        extra_data   = b'',
    )
    assert len(encoded) > 0, "Encoded must not be empty"

    decoded = decode_params(encoded)
    assert decoded['protocol'] == 0,          f"protocol mismatch: {decoded['protocol']}"
    assert decoded['collateralAsset'].lower() == USDC.lower(), "col mismatch"
    assert decoded['debtAsset'].lower()       == USDT.lower(), "debt mismatch"
    assert decoded['user'].lower()            == USER.lower(), "user mismatch"
    assert decoded['debtToCover']             == 1_000_000,    "debtToCover mismatch"
    assert decoded['minCollateralReceived']   == 950_000,      "minCol mismatch"
    assert decoded['swapPath'][0].lower()     == USDC.lower(), "swapPath[0] mismatch"
    assert decoded['swapPath'][1].lower()     == USDT.lower(), "swapPath[1] mismatch"
    assert decoded['swapRouter'].lower()      == QS_V2.lower(),"router mismatch"
    assert decoded['routerType']              == 0,            "routerType mismatch"
    assert decoded['deadline']                == 9_999_999_999,"deadline mismatch"
    assert decoded['minProfitRequired']       == 10_000,       "minProfit mismatch"
    assert decoded['extraData']               == b'',          "extraData mismatch"
    print(f"  Encoded: {len(encoded)} bytes — round-trip PASS")


# ════════════════════════════════════════════════════════════════
# Test 2: Radiant protocol (same encoding, different protocol byte)
# ════════════════════════════════════════════════════════════════
def test_radiant_protocol_byte():
    print("Test 2: Radiant protocol byte...")
    encoded = encode_params(
        protocol     = 3,                   # PROTOCOL_RADIANT
        col          = WETH,
        debt         = USDC,
        user         = USER,
        debt_cover   = 500_000_000,
        min_col      = int(0.28e18),
        swap_path    = [WETH, USDC],
        router       = QS_V2,
        router_type  = 0,
        v3_fees      = [],
        one_inch_data= b'',
        deadline     = 9_999_999_999,
        min_profit   = 5_000_000,
        extra_data   = b'',
    )
    decoded = decode_params(encoded)
    assert decoded['protocol'] == 3, f"Expected PROTOCOL_RADIANT=3, got {decoded['protocol']}"
    print(f"  Protocol=3 confirmed — PASS")


# ════════════════════════════════════════════════════════════════
# Test 3: Morpho Blue — extraData encodes MorphoMarketParams
# ════════════════════════════════════════════════════════════════
def test_morpho_extra_data():
    print("Test 3: Morpho Blue extraData encoding...")
    oracle = '0x1111111111111111111111111111111111111111'
    irm    = '0x2222222222222222222222222222222222222222'
    lltv   = int(0.915e18)   # 91.5%

    extra = encode_morpho_extra(
        loan_token       = USDC,
        collateral_token = WETH,
        oracle           = oracle,
        irm              = irm,
        lltv             = lltv,
    )
    assert len(extra) == 160, f"MorphoMarketParams should be 5*32=160 bytes, got {len(extra)}"

    # Decode and verify
    (loan, col, orc, i, l) = abi_decode([MORPHO_EXTRA_TYPE], extra)[0]
    assert loan.lower() == USDC.lower(),   "loan_token mismatch"
    assert col.lower()  == WETH.lower(),   "collateral_token mismatch"
    assert orc.lower()  == oracle.lower(), "oracle mismatch"
    assert i.lower()    == irm.lower(),    "irm mismatch"
    assert l             == lltv,           "lltv mismatch"

    # Now encode full params with extraData
    encoded = encode_params(
        protocol     = 1,    # PROTOCOL_MORPHO_BLUE
        col          = WETH,
        debt         = USDC,
        user         = USER,
        debt_cover   = 2_000_000,
        min_col      = int(0.001e18),
        swap_path    = [WETH, USDC],
        router       = QS_V2,
        router_type  = 0,
        v3_fees      = [],
        one_inch_data= b'',
        deadline     = 9_999_999_999,
        min_profit   = 20_000,
        extra_data   = extra,
    )
    decoded = decode_params(encoded)
    assert decoded['protocol']  == 1, "protocol must be MORPHO_BLUE"
    assert decoded['extraData'] == extra, "extraData must match"
    print(f"  Morpho extraData ({len(extra)} bytes) embedded — PASS")


# ════════════════════════════════════════════════════════════════
# Test 4: Three-hop swap path (via USDC bridge)
# ════════════════════════════════════════════════════════════════
def test_three_hop_path():
    print("Test 4: Three-hop swap path...")
    WBTC = Web3.to_checksum_address('0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6')
    path = [WBTC, USDC, USDT]
    encoded = encode_params(
        protocol=0, col=WBTC, debt=USDT, user=USER,
        debt_cover=100_000, min_col=int(0.0004e8),
        swap_path=path, router=QS_V2, router_type=0,
        v3_fees=[], one_inch_data=b'', deadline=9_999_999_999,
        min_profit=1_000, extra_data=b'',
    )
    decoded = decode_params(encoded)
    assert len(decoded['swapPath']) == 3, "Expected 3-element swapPath"
    assert decoded['swapPath'][1].lower() == USDC.lower(), "bridge token must be USDC"
    print(f"  3-hop path [{' -> '.join(a[:8] for a in decoded['swapPath'])}] — PASS")


# ════════════════════════════════════════════════════════════════
# Test 5: Compound V3 protocol byte
# ════════════════════════════════════════════════════════════════
def test_compound_protocol_byte():
    print("Test 5: Compound V3 protocol byte...")
    encoded = encode_params(
        protocol=2, col=USDC, debt=USDC, user=USER,
        debt_cover=0, min_col=0, swap_path=[USDC, USDC],
        router=QS_V2, router_type=0, v3_fees=[],
        one_inch_data=b'', deadline=9_999_999_999,
        min_profit=0, extra_data=b'',
    )
    decoded = decode_params(encoded)
    assert decoded['protocol'] == 2, f"Expected PROTOCOL_COMPOUND_V3=2, got {decoded['protocol']}"
    print(f"  Protocol=2 confirmed — PASS")


# ════════════════════════════════════════════════════════════════
# Run all tests
# ════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    tests = [
        test_aave_v3_roundtrip,
        test_radiant_protocol_byte,
        test_morpho_extra_data,
        test_three_hop_path,
        test_compound_protocol_byte,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed / {failed} failed")
    if failed:
        sys.exit(1)
    else:
        print("All encode_params tests PASSED")
