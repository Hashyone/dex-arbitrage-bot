# Polygon arbitrage bot architecture assessment

## What currently works

- `pool_discovery.py` batches Polygon factory, reserve, `slot0`, liquidity, and
  Algebra `globalState()` reads through Multicall3.
- V2/V3 and Algebra candidate sizing uses the existing `calc.calc_dya()`
  optimizer. Its fee-adjusted route output is preserved.
- The deployed contract builds a Balancer flashloan route and performs the two
  swaps plus repayment atomically.
- A live `eth_call` simulation and a second gas-estimation gate run before any
  signed transaction. bloXroute submission is preserved when configured.
- V2 reserve and V3/Algebra TVL floors reject obvious thin or uninitialized
  pools.

## Incomplete or unsafe before this instrumentation

- Profitability used a fixed gas-limit cap and did not include the Balancer
  flashloan fee explicitly.
- Algebra fee handling from the restored snapshot can accept a missing/zero fee
  as free.
- Candidate lifecycle, rejection reasons, timestamps, block numbers, and
  execution outcomes were only present in free-form logs.
- The 20-second discovery loop is a fallback poller, not an event-driven state
  feed. Oracle transitions were not observed.
- Curve discovery and execution are not equivalent to an exact, reviewed
  round-trip route and must remain fail-closed until independently validated.

## False positives, missed opportunities, and decay

- False positives arise from stale concentrated-liquidity state, token-order or
  decimal mistakes, missing Algebra fee data, thin pools, and a theoretical
  spread that disappears before the exact transaction is simulated.
- Opportunities are missed because factory discovery and all pool reads are
  serialized through rate-limited RPCs, while no Swap/oracle update feed
  prioritizes recently changed state.
- Profitable candidates decay between discovery, simulation, gas estimation,
  signing, and inclusion; the existing simulation gate correctly rejects many
  of these rather than risking capital.

## Components intentionally unchanged

- `calc.py` remains unchanged because no mathematical defect has been
  demonstrated in this work.
- `pool_discovery.py` TVL floors and its RPC chunking remain unchanged.
- The deployed Solidity contract, MEV helper, and the separate liquidation
  bots are outside this arbitrage product boundary.

## Next validation boundary

The new JSONL telemetry, explicit accounting, paper mode, and research-only
oracle observer are the instrumentation boundary. No oracle transition is
promoted to an executable strategy until a verified atomic route survives exact
quoting, fork testing, and live `eth_call` simulation.
