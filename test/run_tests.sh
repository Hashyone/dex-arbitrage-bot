#!/usr/bin/env bash
# Run Foundry fork tests for MultiProtocolHybridBot
# Usage: ./run_tests.sh [extra forge args]
# Example: ./run_tests.sh -vvvv --match-test test_ContractState

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Build RPC URL from environment
if [[ -n "$FINALITY_API_KEY" ]]; then
    # Support full URL or bare key
    if [[ "$FINALITY_API_KEY" == http* ]]; then
        export POLYGON_RPC_URL="$FINALITY_API_KEY"
    else
        export POLYGON_RPC_URL="https://polygon.api.onfinality.io/rpc?apikey=$FINALITY_API_KEY"
    fi
elif [[ -n "$ALCHEMY_API_KEY" ]]; then
    export POLYGON_RPC_URL="https://polygon-mainnet.g.alchemy.com/v2/$ALCHEMY_API_KEY"
fi

if [[ -z "$POLYGON_RPC_URL" ]]; then
    echo "ERROR: Set FINALITY_API_KEY or ALCHEMY_API_KEY to run fork tests"
    exit 1
fi

echo "RPC: ${POLYGON_RPC_URL:0:60}..."
echo ""

# Locate forge binary
FORGE="$(which forge 2>/dev/null || echo "$HOME/.local/bin/forge")"
if [[ ! -x "$FORGE" ]]; then
    echo "ERROR: forge not found. Run: curl -L https://foundry.paradigm.xyz | bash && foundryup"
    exit 1
fi

echo "Forge: $FORGE"
"$FORGE" --version
echo ""

exec "$FORGE" test \
    --fork-url "$POLYGON_RPC_URL" \
    --match-contract HybridBotFork \
    -vvv \
    "$@"
