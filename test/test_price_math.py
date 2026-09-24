import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from calc import Q96
from price_math import v2_human_reserves, v3_oriented_state


ADDR_LOW = "0x0000000000000000000000000000000000000001"
ADDR_HIGH = "0x0000000000000000000000000000000000000002"


def test_v2_reserve_order_and_decimals_are_aligned():
    # token A is token1, so reserve1 is A and reserve0 is B.
    amount_a, amount_b = v2_human_reserves(
        (2 * 10**18, 3_000_000), ADDR_HIGH, ADDR_LOW, 6, 18
    )
    assert amount_a == 3.0
    assert amount_b == 2.0


def test_v3_token1_orientation_inverts_price_and_sqrt_consistently():
    # Raw token1/token0 = 2.  With A as token1, B/A = 1/2.
    sqrt_price = int(math.sqrt(2.0) * Q96)
    price, sqrt_price_oriented, liquidity = v3_oriented_state(
        sqrt_price,
        10**12,
        ADDR_HIGH,
        ADDR_LOW,
        6,
        6,
    )
    assert math.isclose(price, 0.5, rel_tol=1e-9)
    assert math.isclose(sqrt_price_oriented, math.sqrt(0.5), rel_tol=1e-9)
    assert liquidity > 0
