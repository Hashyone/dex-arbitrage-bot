#!/usr/bin/env python3
"""
Deploy MultiProtocolHybridBot to Polygon mainnet.

Usage:
    python3 deploy_contract.py

Saves deployed address to bot/contract/deployed_address.json
"""

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
import solcx

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────
HERE         = Path(__file__).parent
CONTRACT_DIR = HERE / 'contract'
SOL_FILE     = CONTRACT_DIR / 'MultiProtocolHybridBot.sol'
ADDR_FILE    = CONTRACT_DIR / 'deployed_address.json'

SOLC_VERSION = '0.8.20'

# ─── RPC ──────────────────────────────────────────────────────────────
def _build_rpc_http():
    raw = (os.getenv('FINALITY_API_KEY') or '').strip()
    if raw:
        if raw.startswith('http'):
            # Already a full URL — use directly
            return raw
        # Raw UUID key
        return f'https://polygon.api.onfinality.io/rpc?apikey={raw}'
    alchemy = (os.getenv('ALCHEMY_API_KEY') or '').strip()
    if alchemy:
        return f'https://polygon-mainnet.g.alchemy.com/v2/{alchemy}'
    sys.exit('ERROR: No RPC key found. Set FINALITY_API_KEY or ALCHEMY_API_KEY.')

RPC_HTTP = _build_rpc_http()

# ─── Wallet ───────────────────────────────────────────────────────────
PRIVATE_KEY = (os.getenv('PRIVATE_KEY') or '').strip()
if not PRIVATE_KEY:
    sys.exit('ERROR: PRIVATE_KEY not set.')
if not PRIVATE_KEY.startswith('0x'):
    PRIVATE_KEY = '0x' + PRIVATE_KEY


def compile_contract():
    """Compile the Solidity contract using py-solc-x."""
    print(f'[+] Installing solc {SOLC_VERSION} if needed...')
    solcx.install_solc(SOLC_VERSION, show_progress=False)
    solcx.set_solc_version(SOLC_VERSION)

    print(f'[+] Compiling {SOL_FILE.name}...')
    source = SOL_FILE.read_text()

    result = solcx.compile_source(
        source,
        output_values=['abi', 'bin'],
        solc_version=SOLC_VERSION,
        optimize=True,
        optimize_runs=200,
    )

    # solcx returns keys like '<stdin>:ContractName'
    for key, data in result.items():
        if 'MultiProtocolHybridBot' in key:
            print(f'[+] Compiled: {key}')
            return data['abi'], data['bin']

    raise RuntimeError('Contract MultiProtocolHybridBot not found in compiled output.')


def deploy(w3: Web3, abi: list, bytecode: str, account) -> str:
    """Deploy contract and return its address."""
    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)

    print('[+] Estimating gas...')
    gas_estimate = Contract.constructor().estimate_gas({'from': account.address})
    gas_limit    = int(gas_estimate * 1.2)

    gas_price    = w3.eth.gas_price
    gas_price_gw = gas_price / 1e9
    print(f'[+] Gas price: {gas_price_gw:.2f} gwei | Gas limit: {gas_limit:,}')
    cost_matic   = (gas_price * gas_limit) / 1e18
    print(f'[+] Estimated deployment cost: {cost_matic:.4f} MATIC')

    balance = w3.eth.get_balance(account.address)
    print(f'[+] Wallet balance: {balance / 1e18:.4f} MATIC')
    if balance < gas_price * gas_limit:
        sys.exit('ERROR: Insufficient MATIC for deployment.')

    nonce = w3.eth.get_transaction_count(account.address)
    tx    = Contract.constructor().build_transaction({
        'from':     account.address,
        'gas':      gas_limit,
        'gasPrice': gas_price,
        'nonce':    nonce,
        'chainId':  137,
    })

    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    print('[+] Sending deployment transaction...')
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f'[+] TX hash: {tx_hash.hex()}')

    print('[+] Waiting for receipt (up to 120s)...')
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status != 1:
        sys.exit(f'ERROR: Transaction failed. Receipt: {receipt}')

    return receipt.contractAddress


def setup_approvals(w3: Web3, contract_address: str, abi: list, account):
    """Call setupAllApprovals() on the freshly deployed contract."""
    contract = w3.eth.contract(address=contract_address, abi=abi)

    print('[+] Calling setupAllApprovals()...')
    nonce     = w3.eth.get_transaction_count(account.address)
    gas_price = w3.eth.gas_price

    tx = contract.functions.setupAllApprovals().build_transaction({
        'from':     account.address,
        'gas':      2_000_000,
        'gasPrice': gas_price,
        'nonce':    nonce,
        'chainId':  137,
    })
    signed  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f'[+] setupAllApprovals TX: {tx_hash.hex()}')
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status == 1:
        print('[+] setupAllApprovals: SUCCESS')
    else:
        print('[!] setupAllApprovals: FAILED — run approve_tokens.py manually')


def main():
    print('=' * 60)
    print('MultiProtocolHybridBot — Deployment Script')
    print('=' * 60)

    # Connect
    w3 = Web3(Web3.HTTPProvider(RPC_HTTP, request_kwargs={'timeout': 30}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    block = w3.eth.block_number
    print(f'[+] Connected to Polygon (block {block:,})')

    account = w3.eth.account.from_key(PRIVATE_KEY)
    print(f'[+] Deploying from: {account.address}')

    # Compile
    abi, bytecode = compile_contract()

    # Deploy
    address = deploy(w3, abi, bytecode, account)
    print(f'\n✅ CONTRACT DEPLOYED: {address}\n')

    # Save ABI and address
    abi_path = CONTRACT_DIR / 'MultiProtocolHybridBot.abi.json'
    abi_path.write_text(json.dumps(abi, indent=2))
    print(f'[+] ABI saved to {abi_path}')

    addr_data = {
        'address':    address,
        'chain_id':   137,
        'chain':      'polygon',
        'deployed_at': int(time.time()),
        'deployer':   account.address,
    }
    ADDR_FILE.write_text(json.dumps(addr_data, indent=2))
    print(f'[+] Address saved to {ADDR_FILE}')

    print('\n' + '=' * 60)
    print(f'Contract address: {address}')
    print(f'Polygonscan: https://polygonscan.com/address/{address}')
    print('=' * 60)
    print('\nNext step: run   python3 approve_tokens.py   to set all token approvals')


if __name__ == '__main__':
    main()
