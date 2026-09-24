#!/usr/bin/env python3
"""
Token approval setup for MultiProtocolHybridBot.

Calls setupAllApprovals() on the deployed contract (which makes the CONTRACT
approve all DEX routers and protocol addresses for all tokens).

Also calls batchApproveTokensForSpender() for any extra tokens not in
the contract's built-in list.

USDT and bridged USDC require reset-to-zero — handled inside the contract's
_safeApprove() function automatically.

Usage:
    python3 approve_tokens.py
    python3 approve_tokens.py --contract 0xNEW_ADDRESS
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

HERE      = Path(__file__).parent
ADDR_FILE = HERE / 'contract' / 'deployed_address.json'
ABI_FILE  = HERE / 'contract' / 'MultiProtocolHybridBot.abi.json'

# ─── RPC ──────────────────────────────────────────────────────────────
def _build_rpc_http():
    raw = (os.getenv('FINALITY_API_KEY') or '').strip()
    if raw:
        return raw if raw.startswith('http') else f'https://polygon.api.onfinality.io/rpc?apikey={raw}'
    alchemy = (os.getenv('ALCHEMY_API_KEY') or '').strip()
    if alchemy:
        return f'https://polygon-mainnet.g.alchemy.com/v2/{alchemy}'
    sys.exit('ERROR: No RPC key found.')

RPC_HTTP = _build_rpc_http()

PRIVATE_KEY = (os.getenv('PRIVATE_KEY') or '').strip()
if not PRIVATE_KEY:
    sys.exit('ERROR: PRIVATE_KEY not set.')
if not PRIVATE_KEY.startswith('0x'):
    PRIVATE_KEY = '0x' + PRIVATE_KEY

# ─── Extra tokens not in contract's built-in list ─────────────────────
# The contract already handles: WPOL, WETH, WBTC, USDC, USDC_N, USDT,
# DAI, LINK, AAVE, CRV, BAL.
# These are extra tokens to approve via batchApproveTokensForSpender().
EXTRA_TOKENS = {
    'WSTETH':  '0x03b54A6e9a984069379fae1a4fC4dBAE93B3bCCD',
    'MATICX':  '0xfa68FB4628DFF1028CFEc22b4162FCcd0d45efb6',
    'GHST':    '0x385Eeac5cB85A38A9a07A70c73e0a3271CfB54A7',
    'SUSHI':   '0x0b3F868E0BE5597D5DB7fEB59E1CADBb0fdDa50a',
    'DPI':     '0x85955046DF4668e1DD369D2DE9f3AEB98DD2A369',
}

EXTRA_SPENDERS = [
    '0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff',  # QuickSwap V2
    '0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506',  # SushiSwap
    '0xE592427A0AEce92De3Edee1F18E0157C05861564',  # Uniswap V3
    '0x111111125421cA6dc452d289314280a0f8842A65',  # 1inch
    '0x794a61358D6845594F94dc1DB02A252b5b4814aD',  # Aave V3
    '0x2032b9A8e9F7e76768CA9271003d3e43E1616B1F',  # Radiant
    '0xF25212E676D1F7F89Cd72fFEe66158f541246445',  # Compound V3
    '0xBA12222222228d8Ba445958a75a0704d566BF2C8',  # Balancer
]


def send_tx(w3, fn, account, gas=8_000_000):
    """Build, sign, send a transaction and wait for receipt."""
    gas_price = w3.eth.gas_price
    nonce     = w3.eth.get_transaction_count(account.address)
    tx = fn.build_transaction({
        'from':     account.address,
        'gas':      gas,
        'gasPrice': gas_price,
        'nonce':    nonce,
        'chainId':  137,
    })
    signed  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f'  TX sent: {tx_hash.hex()[:20]}...')
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    status  = 'SUCCESS' if receipt.status == 1 else 'FAILED'
    gas_used = receipt.gasUsed
    print(f'  Status: {status}  |  Gas used: {gas_used:,}')
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--contract', help='Contract address (overrides deployed_address.json)')
    args = parser.parse_args()

    # Load contract address and ABI
    if args.contract:
        contract_address = args.contract
    elif ADDR_FILE.exists():
        data = json.loads(ADDR_FILE.read_text())
        contract_address = data['address']
    else:
        sys.exit('ERROR: No contract address. Run deploy_contract.py first.')

    if not ABI_FILE.exists():
        sys.exit('ERROR: MultiProtocolHybridBot.abi.json not found. Run deploy_contract.py first.')

    abi = json.loads(ABI_FILE.read_text())

    print('=' * 60)
    print(f'MultiProtocolHybridBot — Token Approval Setup')
    print(f'Contract: {contract_address}')
    print('=' * 60)

    w3 = Web3(Web3.HTTPProvider(RPC_HTTP, request_kwargs={'timeout': 30}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    account = w3.eth.account.from_key(PRIVATE_KEY)

    print(f'Wallet : {account.address}')
    print(f'Block  : {w3.eth.block_number:,}')
    print(f'Balance: {w3.eth.get_balance(account.address) / 1e18:.4f} MATIC')

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(contract_address),
        abi=abi,
    )

    # Verify ownership
    owner = contract.functions.owner().call()
    if owner.lower() != account.address.lower():
        sys.exit(f'ERROR: Wallet {account.address} is not the owner ({owner})')
    print(f'Owner  : {owner} ✓\n')

    # ── Step 1: Approve built-in tokens per spender ───────────────────
    # We call batchApproveTokensForSpender once per spender (11 tokens each).
    # ~600k gas per call, well under the 1-ETH fee cap.
    BUILT_IN_TOKENS = [
        '0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270',  # WPOL
        '0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619',  # WETH
        '0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6',  # WBTC
        '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',  # USDC (bridged)
        '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359',  # USDC native
        '0xc2132D05D31c914a87C6611C10748AEb04B58e8F',  # USDT
        '0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063',  # DAI
        '0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39',  # LINK
        '0xD6DF932A45C0f255f85145f286eA0b292B21C90B',  # AAVE
        '0x172370d5Cd63279eFa6d502DAB29171933a610AF',  # CRV
        '0x9A71012b13ca4d3D0cDc72a177DF3eF03b0E76a7',  # BAL
    ]
    ALL_SPENDERS = [
        ('QuickSwap V2', '0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff'),
        ('SushiSwap',    '0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506'),
        ('Uniswap V3',   '0xE592427A0AEce92De3Edee1F18E0157C05861564'),
        ('1inch',        '0x111111125421cA6dc452d289314280a0f8842A65'),
        ('Aave V3',      '0x794a61358D6845594F94dc1DB02A252b5b4814aD'),
        ('Radiant',      '0x2032b9A8e9F7e76768CA9271003d3e43E1616B1F'),
        ('Morpho Blue',  '0x9dc1cf03C47513f64C3cA6b226F4b2B9da36e281'),
        ('Compound V3',  '0xF25212E676D1F7F89Cd72fFEe66158f541246445'),
        ('Balancer',     '0xBA12222222228d8Ba445958a75a0704d566BF2C8'),
    ]

    tokens_cs = [Web3.to_checksum_address(t) for t in BUILT_IN_TOKENS]

    print(f'[1/3] Approving {len(BUILT_IN_TOKENS)} tokens × {len(ALL_SPENDERS)} spenders '
          f'({len(BUILT_IN_TOKENS) * len(ALL_SPENDERS)} approvals total, 1 tx per spender)...')

    for label, spender in ALL_SPENDERS:
        spender_cs = Web3.to_checksum_address(spender)
        print(f'  → {label} ({spender_cs[:12]}...)')
        try:
            receipt = send_tx(
                w3,
                contract.functions.batchApproveTokensForSpender(tokens_cs, spender_cs),
                account,
                gas=800_000,
            )
            if receipt.status != 1:
                print(f'    [!] FAILED for {label}')
            time.sleep(1)
        except Exception as e:
            print(f'    [!] ERROR for {label}: {e}')

    print('[+] Built-in token approvals complete.\n')
    time.sleep(3)

    # ── Step 2: Extra tokens ───────────────────────────────────────────
    if EXTRA_TOKENS:
        print(f'[2/3] Approving {len(EXTRA_TOKENS)} extra tokens for all routers...')
        for sym, addr in EXTRA_TOKENS.items():
            print(f'  [{sym}] {addr}')
        for spender in EXTRA_SPENDERS:
            try:
                tokens_list = [Web3.to_checksum_address(a) for a in EXTRA_TOKENS.values()]
                print(f'  Approving {len(tokens_list)} extra tokens → {spender[:12]}...')
                receipt = send_tx(
                    w3,
                    contract.functions.batchApproveTokensForSpender(tokens_list, spender),
                    account,
                    gas=2_000_000,
                )
                time.sleep(1)
            except Exception as e:
                print(f'  [!] Extra token approval error for {spender[:12]}: {e}')
    else:
        print('[2/3] No extra tokens to approve.')

    time.sleep(3)

    # ── Step 3: Verify a few allowances ───────────────────────────────
    print('\n[3/3] Verifying allowances...')
    ERC20_ABI_MIN = [
        {'name': 'allowance', 'inputs': [{'name': 'owner', 'type': 'address'}, {'name': 'spender', 'type': 'address'}],
         'outputs': [{'name': '', 'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'},
    ]
    checks = [
        ('USDT',  '0xc2132D05D31c914a87C6611C10748AEb04B58e8F', '0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff', 'QuickSwap'),
        ('USDC',  '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174', '0x794a61358D6845594F94dc1DB02A252b5b4814aD', 'Aave V3'),
        ('WETH',  '0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619', '0xE592427A0AEce92De3Edee1F18E0157C05861564', 'UniswapV3'),
        ('WBTC',  '0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6', '0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506', 'SushiSwap'),
    ]
    MAX_UINT = 2**256 - 1
    all_ok = True
    for sym, token_addr, spender, label in checks:
        try:
            tok = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI_MIN)
            allowance = tok.functions.allowance(
                Web3.to_checksum_address(contract_address),
                Web3.to_checksum_address(spender),
            ).call()
            status = '✓ MAX' if allowance == MAX_UINT else f'⚠ {allowance}'
            if allowance != MAX_UINT:
                all_ok = False
            print(f'  {sym} → {label}: {status}')
        except Exception as e:
            print(f'  {sym} → {label}: ERROR — {e}')
            all_ok = False

    print()
    if all_ok:
        print('✅ All approvals verified. Contract is ready to trade.')
    else:
        print('⚠️  Some approvals may not be set correctly. Re-run this script.')

    print(f'\nContract: {contract_address}')
    print(f'Polygonscan: https://polygonscan.com/address/{contract_address}')


if __name__ == '__main__':
    main()
