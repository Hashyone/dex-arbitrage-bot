"""Fail-closed checks for token ordering, decimals, and route magnitudes."""

import math
from typing import Dict, Tuple

from web3 import Web3


DEFAULT_MAX_PROFIT_BPS = 1_000.0  # 10%; override for a different universe
DEFAULT_MAX_TRADE_USD = 1_000_000.0


def _finite_positive(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and value > 0


def validate_pair_addresses(opp: Dict) -> Tuple[bool, str]:
    """Ensure symbols, canonical Polygon addresses, and route roles agree."""
    sym_a, sym_b = opp.get("sym_a"), opp.get("sym_b")
    addr_a, addr_b = opp.get("addr_a"), opp.get("addr_b")
    if not sym_a or not sym_b or sym_a == sym_b:
        return False, "TOKEN_PAIR_INVALID"
    if not addr_a or not addr_b:
        return False, "TOKEN_ADDRESS_MISSING"
    try:
        addr_a = Web3.to_checksum_address(addr_a)
        addr_b = Web3.to_checksum_address(addr_b)
    except Exception:
        return False, "TOKEN_ADDRESS_INVALID"
    if addr_a.lower() == addr_b.lower():
        return False, "TOKEN_ADDRESSES_IDENTICAL"

    universe = getattr(opp.get("_token_universe_module"), "TOKEN_UNIVERSE", None)
    if universe is None:
        # Callers may provide the canonical map explicitly to keep this module
        # usable in isolated unit tests.
        universe = opp.get("_token_universe") or {}
    if universe:
        expected_a = universe.get(sym_a)
        expected_b = universe.get(sym_b)
        if not expected_a or not expected_b:
            return False, "TOKEN_NOT_IN_CANONICAL_UNIVERSE"
        if addr_a.lower() != expected_a.lower() or addr_b.lower() != expected_b.lower():
            return False, "TOKEN_ADDRESS_SYMBOL_MISMATCH"
    return True, "OK"


def validate_opportunity(
    opp: Dict,
    *,
    max_profit_bps: float = DEFAULT_MAX_PROFIT_BPS,
    max_trade_usd: float = DEFAULT_MAX_TRADE_USD,
) -> Tuple[bool, str]:
    """Validate the current bot's B→A→B flashloan route before encoding."""
    ok, reason = validate_pair_addresses(opp)
    if not ok:
        return ok, reason
    for key in ("dec_a", "dec_b"):
        value = opp.get(key)
        if not isinstance(value, int) or value < 0 or value > 36:
            return False, "TOKEN_DECIMALS_INVALID"

    amount = opp.get("dya_human")
    output = opp.get("dyb_human")
    profit = opp.get("gross_profit_b_human")
    if not all(_finite_positive(v) for v in (amount, output)):
        return False, "ROUTE_AMOUNT_INVALID"
    if not isinstance(profit, (int, float)) or not math.isfinite(float(profit)):
        return False, "PROFIT_AMOUNT_INVALID"
    if profit <= 0:
        return False, "NO_PROFIT"

    profit_bps = profit / amount * 10_000.0
    if profit_bps > max_profit_bps:
        return False, "PROFIT_MAGNITUDE_SUSPECT"
    if output < amount:
        return False, "ROUTE_OUTPUT_BELOW_INPUT"

    price_b = opp.get("price_b_usd")
    if price_b is not None and amount * price_b > max_trade_usd:
        return False, "TRADE_SIZE_TOO_LARGE"

    # The contract route must be exactly flashloan B, then B→A, then A→B.
    route = opp.get("route_tokens")
    expected = [opp["addr_b"], opp["addr_a"], opp["addr_b"]]
    if route is not None and [str(x).lower() for x in route] != [
        str(x).lower() for x in expected
    ]:
        return False, "ROUTE_TOKEN_SEQUENCE_INVALID"
    if opp.get("route_hops", 2) != 2:
        return False, "ROUTE_HOPS_INVALID"
    return True, "OK"
