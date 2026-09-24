"""Token-order and decimal normalization shared by route calculations."""

import math
from typing import Tuple

from calc import Q96


def v2_human_reserves(
    reserves: Tuple[int, int],
    addr_a: str,
    addr_b: str,
    dec_a: int,
    dec_b: int,
) -> Tuple[float, float]:
    """Return (A, B) reserves, mapping reserve0/1 by protocol address order."""
    r0, r1 = reserves
    if int(addr_a, 16) < int(addr_b, 16):
        amount_a = r0 / 10**dec_a
        amount_b = r1 / 10**dec_b
    else:
        amount_a = r1 / 10**dec_a
        amount_b = r0 / 10**dec_b
    if not all(math.isfinite(x) and x > 0 for x in (amount_a, amount_b)):
        raise ValueError("V2 reserves are not finite and positive")
    return amount_a, amount_b


def v3_oriented_state(
    sqrt_price_x96: int,
    liquidity: int,
    addr_a: str,
    addr_b: str,
    dec_a: int,
    dec_b: int,
) -> Tuple[float, float, float]:
    """Return (B per A, sqrt(B per A), human-unit liquidity).

    sqrtPriceX96 is always token1/token0 in raw units.  All downstream
    quantities are derived from the already oriented human price, including
    the square root used by calc.py.
    """
    if sqrt_price_x96 <= 0 or liquidity <= 0:
        raise ValueError("V3 state is uninitialized")
    a_is_token0 = int(addr_a, 16) < int(addr_b, 16)
    dec0, dec1 = (dec_a, dec_b) if a_is_token0 else (dec_b, dec_a)
    raw_price = (sqrt_price_x96 / Q96) ** 2
    human_token1_per_token0 = raw_price * 10 ** (dec0 - dec1)
    if human_token1_per_token0 <= 0 or not math.isfinite(human_token1_per_token0):
        raise ValueError("V3 price is invalid")
    price_b_per_a = (
        human_token1_per_token0
        if a_is_token0
        else 1.0 / human_token1_per_token0
    )
    sqrt_b_per_a = math.sqrt(price_b_per_a)
    liquidity_human = liquidity / 10 ** ((dec0 + dec1) / 2.0)
    if not math.isfinite(liquidity_human) or liquidity_human <= 0:
        raise ValueError("V3 liquidity is invalid")
    return price_b_per_a, sqrt_b_per_a, liquidity_human
