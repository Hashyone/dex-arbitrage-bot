import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from accounting import calculate_route_economics


def test_flashloan_and_gas_are_included_without_dex_double_count():
    result = calculate_route_economics(
        route_profit_after_dex_fees_token=10.0,
        flashloan_amount_token=1_000.0,
        flashloan_fee_bps=9.0,
        gas_units=100_000,
        gas_price_wei=30_000_000_000,
        native_usd=0.70,
        token_usd=1.0,
        dex_fees_token=4.0,
    )
    assert math.isclose(result.flashloan_fee_token, 0.9)
    assert math.isclose(result.gas_cost_usd, 0.0021, rel_tol=1e-9, abs_tol=1e-9)
    assert math.isclose(result.net_profit_usd, 9.0979, rel_tol=1e-9, abs_tol=1e-9)
    assert result.dex_fees_token == 4.0


def test_invalid_negative_economic_inputs_fail_closed():
    try:
        calculate_route_economics(
            route_profit_after_dex_fees_token=-1.0,
            flashloan_amount_token=10.0,
            flashloan_fee_bps=9.0,
            gas_units=1,
            gas_price_wei=1,
            native_usd=1.0,
            token_usd=1.0,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("negative route profit must be rejected")
