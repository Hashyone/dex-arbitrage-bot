import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from oracle_edge import OracleEdgeMonitor


def test_oracle_transition_observation_is_research_only():
    monitor = OracleEdgeMonitor({"USDC": "0x0000000000000000000000000000000000000001"})
    first = monitor._observe(
        key="aave:usdc",
        oracle_address="0x0000000000000000000000000000000000000002",
        oracle_type="AAVE_V3_PRICE_ORACLE",
        asset="USDC",
        value=1.0,
        update_block=100,
        update_timestamp=1_000,
        consumer="Aave V3 Pool",
        market_price=1.0,
        now=1_001,
    )
    assert first is not None
    second = monitor._observe(
        key="aave:usdc",
        oracle_address="0x0000000000000000000000000000000000000002",
        oracle_type="AAVE_V3_PRICE_ORACLE",
        asset="USDC",
        value=1.01,
        update_block=101,
        update_timestamp=1_002,
        consumer="Aave V3 Pool",
        market_price=1.0,
        now=1_003,
    )
    assert second is not None
    assert second.research_only is True
    assert second.atomic_validation == "not_attempted"
    assert second.previous_value == 1.0
