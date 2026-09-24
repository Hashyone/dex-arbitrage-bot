# Polygon DEX Arbitrage Bot

A research and execution architecture for atomic, same-chain arbitrage across Polygon DEX pools. The project combines on-chain pool discovery, route sizing, cost checks, transaction simulation, and a Balancer flash-loan executor. It is a portfolio demonstration of DeFi market microstructure and execution engineering. **No profitable trading record or reliable economic edge is claimed.**

## How this differs from the liquidation bot

| | This repository | Liquidation repository |
| --- | --- | --- |
| Opportunity | Price differences between DEX pools for the same assets | An Aave V3 borrower's health factor falls below the liquidation threshold |
| Discovery | Periodic factory and pool-state reads | Aave event and new-block subscriptions, tracked positions, oracle-price refreshes |
| Decision | Size a two-leg swap and estimate net proceeds after costs | Choose debt and collateral, liquidation bonus, swap path, and estimated net proceeds |
| On-chain action | Balancer flash loan → DEX swap → reverse DEX swap → repayment | Balancer flash loan → Aave liquidation → collateral sale → repayment |
| Main files | `arb_bot.py`, `pool_discovery.py`, `calc.py`, `CorrectedSlippageArbitrageContract.sol` | `liquidation_bot.py`, `contract/AaveLiquidationContract.sol` |

The two repositories share a historical code snapshot and therefore contain many of the same files. `liquidation_bot.py`, `hybrid_bot.py`, `contract/AaveLiquidationContract.sol`, and `contract/MultiProtocolHybridBot.sol` are adjacent liquidation experiments; they are **not** the arbitrage runner. The corrected-slippage Solidity source at repository root represents the arbitrage contract in this repo. The executable ABI is loaded from `contract/CorrectedSlippageArbitrageContract.abi.json`; verify that it matches the deployed contract before use.

## Implemented path

1. `pool_discovery.py` batches Polygon factory and pool reads through Multicall3, checking QuickSwap V2 reserves, Uniswap V3 state, and QuickSwap V3/Algebra state. It excludes obvious uninitialized or thin pools and works over a configured token universe.
2. `arb_bot.py` builds cross-venue candidates. `calc.py` sizes fee-adjusted round trips; the bot checks route structure, state age, slippage and estimated gas. `accounting.py` records an estimated net result including swap costs, gas and a configured flash-loan fee assumption.
3. The bot estimates gas and calls the **exact contract transaction** through `eth_call` against current RPC state. A passing simulation is a point-in-time check, not proof of future inclusion or realized profit.
4. `CorrectedSlippageArbitrageContract.sol` has an owner-gated entry point, Balancer callback validation, venue-specific swap branches and repayment/profit checks inside one transaction. The runner builds routes for QuickSwap V2, Uniswap V3 and Algebra.
5. Submission requires a wallet, `PAPER_MODE=false`, and `EXECUTION_ENABLED=true`. `mev_protect.py` provides a private submission path when configured; public fallback depends on configuration. `telemetry.py` writes candidate and rejection events.

Curve candidates are scanned, but `run_once()` explicitly rejects Curve routes before execution because the round-trip economics have not been validated. The Solidity contract's additional router branches do not imply the Python strategy can safely execute those venues. `oracle_edge.py` records research observations only.

## Repository map

- `arb_bot.py`: polling, candidate selection, simulation and optional submission.
- `pool_discovery.py`, `calc.py`, `curve.py`: pool state, sizing and exploratory Curve discovery.
- `accounting.py`, `route_sanity.py`, `price_math.py`, `telemetry.py`: cost estimates, safeguards and instrumentation.
- `CorrectedSlippageArbitrageContract.sol`: corrected-slippage atomic executor source; `contract/` includes its ABI and deployment metadata.
- `test/test_accounting.py`, `test/test_route_sanity.py`, `test/test_price_math.py`: focused Python tests. The Foundry fork suite under `test/test/` targets the **hybrid liquidation contract**, not this arbitrage executor.

## Inspect locally

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s test -p 'test_*.py'
```

Running `python arb_bot.py` also requires a working Polygon RPC, matching deployed contract and ABI, and relevant environment configuration. With default `PAPER_MODE=true` and execution disabled, it does not submit trades. Never put a private key in the repository. Review the hard-coded addresses and RPC configuration before attempting any chain interaction.

## Open work and limits

- No verified profitable trades, historical P&L, systematic backtest, or demonstrated persistent alpha is provided. Estimated net profit and a successful `eth_call` are not realized results.
- Discovery is a polling loop (default 20 seconds), with RPC latency and stale-state exposure. An event-driven feed and replay against historical state are still needed.
- Concentrated-liquidity approximation, pool fee data, token decimals, flash-loan fees, price conversion and transaction inclusion must be independently checked against exact on-chain outcomes; the configured fee assumption is not a live quote.
- Add focused fork tests for `CorrectedSlippageArbitrageContract.sol`, including token ordering, slippage bounds, callbacks, repayment and adverse pool-state changes. The current fork suite exercises a different contract.
- Reconcile the root contract source, ABI, configured `ARB_CONTRACT_ADDRESS` and deployment metadata; this snapshot contains differing address records. A claim that the root source is the verified deployed bytecode would require separate verification.
- Measure candidate decay, rejected simulations, submission latency, inclusion, failed transactions and realized net results before considering live operation.

This repository illustrates an implemented route from market observation to a guarded atomic transaction; it is not a production performance claim or an audited contract release.
