"""Explicit, testable economics for a flashloan route.

The concentrated-liquidity/CPMM model returns a route profit after swap fees
and price impact.  We retain an explicit DEX-fee field for observability but
do not subtract it twice when that model output is used.
"""

from dataclasses import dataclass, asdict
from typing import Dict


@dataclass(frozen=True)
class RouteEconomics:
    route_profit_after_dex_fees_token: float
    dex_fees_token: float
    flashloan_amount_token: float
    flashloan_fee_token: float
    expected_slippage_token: float
    gas_units: int
    gas_price_wei: int
    gas_cost_usd: float
    token_usd: float
    net_profit_token: float
    net_profit_usd: float

    def as_dict(self) -> Dict:
        return asdict(self)


def calculate_route_economics(
    *,
    route_profit_after_dex_fees_token: float,
    flashloan_amount_token: float,
    flashloan_fee_bps: float,
    gas_units: int,
    gas_price_wei: int,
    native_usd: float,
    token_usd: float,
    dex_fees_token: float = 0.0,
    expected_slippage_token: float = 0.0,
) -> RouteEconomics:
    """Calculate net economics using the borrowed token as the unit of account.

    `route_profit_after_dex_fees_token` is already fee-adjusted by calc.py.
    `dex_fees_token` is therefore informational unless a caller passes a
    pre-fee route profit and handles that conversion before calling this
    function.  This convention prevents the common double-counting bug.
    """
    if min(
        route_profit_after_dex_fees_token,
        flashloan_amount_token,
        gas_units,
        gas_price_wei,
        native_usd,
        token_usd,
    ) < 0:
        raise ValueError("economic inputs cannot be negative")
    if flashloan_fee_bps < 0 or expected_slippage_token < 0 or dex_fees_token < 0:
        raise ValueError("fees and slippage cannot be negative")

    flashloan_fee_token = flashloan_amount_token * flashloan_fee_bps / 10_000.0
    gas_cost_usd = gas_units * gas_price_wei / 1e18 * native_usd
    gas_cost_token = gas_cost_usd / token_usd if token_usd else float("inf")
    net_profit_token = (
        route_profit_after_dex_fees_token
        - flashloan_fee_token
        - expected_slippage_token
        - gas_cost_token
    )
    return RouteEconomics(
        route_profit_after_dex_fees_token=route_profit_after_dex_fees_token,
        dex_fees_token=dex_fees_token,
        flashloan_amount_token=flashloan_amount_token,
        flashloan_fee_token=flashloan_fee_token,
        expected_slippage_token=expected_slippage_token,
        gas_units=int(gas_units),
        gas_price_wei=int(gas_price_wei),
        gas_cost_usd=gas_cost_usd,
        token_usd=token_usd,
        net_profit_token=net_profit_token,
        net_profit_usd=net_profit_token * token_usd,
    )
