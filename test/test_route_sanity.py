import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from route_sanity import validate_opportunity


UNIVERSE = {
    "A": "0x0000000000000000000000000000000000000001",
    "B": "0x0000000000000000000000000000000000000002",
}


def _opp(**overrides):
    value = {
        "sym_a": "A",
        "sym_b": "B",
        "addr_a": UNIVERSE["A"],
        "addr_b": UNIVERSE["B"],
        "dec_a": 6,
        "dec_b": 18,
        "dya_human": 100.0,
        "dyb_human": 100.2,
        "gross_profit_b_human": 0.2,
        "route_hops": 2,
        "route_tokens": [UNIVERSE["B"], UNIVERSE["A"], UNIVERSE["B"]],
        "_token_universe": UNIVERSE,
    }
    value.update(overrides)
    return value


def test_valid_round_trip_is_accepted():
    assert validate_opportunity(_opp()) == (True, "OK")


def test_wrong_hop_sequence_is_rejected():
    valid, reason = validate_opportunity(
        _opp(route_tokens=[UNIVERSE["B"], UNIVERSE["A"], UNIVERSE["A"]])
    )
    assert not valid
    assert reason == "ROUTE_TOKEN_SEQUENCE_INVALID"


def test_astronomical_profit_is_rejected():
    valid, reason = validate_opportunity(_opp(gross_profit_b_human=50_000.0))
    assert not valid
    assert reason == "PROFIT_MAGNITUDE_SUSPECT"
