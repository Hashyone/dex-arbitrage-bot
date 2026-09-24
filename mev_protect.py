#!/usr/bin/env python3
"""
MEV / front-running protection for Polygon transaction submission.

Research summary (verified against bloXroute's official docs at the time of
writing — see replit.md for the full comparison table):

  - Flashbots Protect is Ethereum-mainnet only; it has no Polygon equivalent
    (Polygon has no MEV-Boost/PBS relay infrastructure for it to plug into).
  - Blocknative's "Protect" private-tx product is likewise Ethereum-only;
    Blocknative's Polygon support was limited to mempool/gas data, and the
    company has been winding the standalone platform down.
  - bloXroute is the one provider with a real, documented, Polygon-native
    private-transaction endpoint: the `polygon_private_tx` JSON-RPC method
    on their Cloud API (https://api.blxrbdn.com). It routes the raw signed
    tx directly to BDN-connected validators, bypassing the public mempool
    entirely, which is exactly what's needed to stop this bot's arbitrage
    transactions from being seen and front-run/sandwiched before they land.
    It requires a free bloXroute account (Authorization header), but no
    payment for basic private-tx submission.
  - Polygon FastLane/Atlas is a protocol-level, more involved SDK/auction
    integration (UserOperation/SolverOperation flow) — heavier to integrate
    than is warranted here; bloXroute's private tx is the pragmatic choice.

This module submits via bloXroute when BLOXROUTE_AUTH_HEADER is configured,
and transparently falls back to normal public broadcast (with a loud warning
that MEV protection is OFF) when it isn't. It never blocks execution on the
absence of a third-party credential we don't control.
"""
import os
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger('arb_bot.mev')

BLOXROUTE_HTTP_URL = 'https://api.blxrbdn.com'


def is_bloxroute_configured() -> bool:
    return bool((os.getenv('BLOXROUTE_AUTH_HEADER') or '').strip())


def send_private_tx(raw_tx_bytes: bytes) -> dict:
    """
    Submit a raw signed transaction privately via bloXroute's Polygon
    private-transaction endpoint (`polygon_private_tx`). Raises on failure;
    caller decides how to handle (e.g. fall back to public broadcast).
    """
    auth = (os.getenv('BLOXROUTE_AUTH_HEADER') or '').strip()
    if not auth:
        raise RuntimeError('BLOXROUTE_AUTH_HEADER not configured')

    raw_hex = raw_tx_bytes.hex()
    if raw_hex.startswith('0x'):
        raw_hex = raw_hex[2:]

    payload = json.dumps({
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'polygon_private_tx',
        'params': {
            'transaction': raw_hex,
            'mev_builders': ['all'],
        },
    }).encode()

    req = urllib.request.Request(
        BLOXROUTE_HTTP_URL,
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': auth},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'bloXroute private tx submission failed: HTTP {e.code}') from e
    except Exception as e:
        raise RuntimeError(f'bloXroute private tx submission failed: {e}') from e

    if 'error' in body:
        raise RuntimeError(f'bloXroute rejected private tx: {body["error"]}')

    logger.info('Submitted via bloXroute polygon_private_tx (mempool-hidden, front-run protected)')
    return body


def submit_transaction(w3, signed_tx, *, allow_public_fallback: bool = True) -> str:
    """
    Submit a signed transaction using the best available protection:
      1. bloXroute private tx, if BLOXROUTE_AUTH_HEADER is configured.
      2. Public broadcast via the normal RPC (unprotected — logs a warning).

    Returns the tx hash as a hex string either way.
    """
    raw = signed_tx.raw_transaction
    tx_hash_hex = '0x' + signed_tx.hash.hex().lstrip('0x')

    if is_bloxroute_configured():
        try:
            send_private_tx(raw)
            return tx_hash_hex
        except Exception as e:
            logger.error(f'Private submission failed: {e}')
            if not allow_public_fallback:
                raise

    if not allow_public_fallback and not is_bloxroute_configured():
        raise RuntimeError('private submission required but bloXroute is not configured')

    logger.warning(
        'MEV PROTECTION INACTIVE — broadcasting via public mempool. '
        'Set BLOXROUTE_AUTH_HEADER (free account at portal.bloxroute.com) '
        'to enable front-running-protected private submission.'
    )
    sent_hash = w3.eth.send_raw_transaction(raw)
    return sent_hash.hex()
