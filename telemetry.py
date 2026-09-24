"""Structured opportunity telemetry for the Polygon arbitrage bot.

The bot deliberately writes append-only JSONL rather than relying only on
human-readable log lines.  This keeps the execution path observable and makes
paper/replay records easy to process with standard command-line tools.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _json_default(value: Any):
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def utc_now_iso(timestamp: Optional[float] = None) -> str:
    return datetime.fromtimestamp(
        timestamp if timestamp is not None else time.time(), tz=timezone.utc
    ).isoformat()


def new_opportunity_id() -> str:
    return "opp_" + uuid.uuid4().hex


class Telemetry:
    """Thread-safe JSONL event writer.

    A failed telemetry write is surfaced to the caller.  Silently dropping
    execution records would make a live bot impossible to audit.
    """

    def __init__(self, path: Optional[str] = None):
        default_path = Path(__file__).with_name("arb_telemetry.jsonl")
        self.path = Path(path or os.getenv("ARB_TELEMETRY_PATH", str(default_path)))
        self._lock = threading.Lock()

    def emit(
        self,
        event_type: str,
        *,
        opportunity_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        **fields: Any,
    ) -> Dict[str, Any]:
        now = timestamp if timestamp is not None else time.time()
        record: Dict[str, Any] = {
            "event_type": event_type,
            "timestamp": utc_now_iso(now),
            "timestamp_epoch": now,
        }
        if opportunity_id:
            record["opportunity_id"] = opportunity_id
        record.update(fields)

        encoded = json.dumps(record, default=_json_default, sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
        return record

    def candidate_detected(self, opp: Dict[str, Any]) -> str:
        candidate_id = opp.setdefault("opportunity_id", new_opportunity_id())
        self.emit(
            "candidate_detected",
            opportunity_id=candidate_id,
            timestamp=opp.get("timestamp_detected"),
            strategy_type=opp.get("strategy_type", "cross_dex"),
            block_number=opp.get("block_number"),
            token_pair=f"{opp.get('sym_a')}/{opp.get('sym_b')}",
            venue_1=opp.get("cheap_kind"),
            venue_2=opp.get("expensive_kind"),
            direction=opp.get("direction"),
            input_amount=opp.get("dya_human"),
            expected_output=opp.get("dyb_human"),
            gross_profit=opp.get("gross_profit_b_human"),
            dex_fees=opp.get("dex_fees_b_human", 0.0),
            flashloan_fee=opp.get("flashloan_fee_b_human"),
            estimated_gas=opp.get("estimated_gas"),
            gas_price=opp.get("gas_price_wei"),
            gas_cost=opp.get("gas_cost_usd"),
            net_profit=opp.get("net_profit_usd"),
            spread=opp.get("spread_bps"),
            liquidity=opp.get("liquidity"),
            price_source=opp.get("price_source"),
            detection_latency_ms=opp.get("detection_latency_ms"),
            route_hops=opp.get("route_hops", 2),
        )
        return candidate_id

    def rejection(self, opp: Dict[str, Any], reason: str, **fields: Any) -> None:
        self.emit(
            "candidate_rejected",
            opportunity_id=opp.get("opportunity_id"),
            rejection_reason=reason,
            strategy_type=opp.get("strategy_type", "cross_dex"),
            block_number=opp.get("block_number"),
            token_pair=f"{opp.get('sym_a')}/{opp.get('sym_b')}",
            **fields,
        )
