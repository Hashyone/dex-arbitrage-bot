#!/usr/bin/env python3
"""
One-shot approval fix: calls approveTokenForAllSpenders() for every key token
on the deployed MultiProtocolHybridBot contract.

Balancer Vault and Compound Comet are included in the contract's spender list —
this fixes the zero-allowance that causes every flash loan to revert.

Run once: python3 fix_approvals.py
"""

import json, os, sys, time
from pathlib import Path
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

HERE      = Path(__file__).parent
ADDR_FILE = HERE / 'contract' / 'deployed_address.json'
ABI_FILE  = HERE / 'contract' / 'MultiProtocolHybridBot.abi.json'

def build_rpc():
    # Prefer OnFinality so we don't rate-limit the running bot's Alchemy quota
    finality = (os.getenv('FINALITY_API_KEY') or '').strip()
    if finality:
        # FINALITY_API_KEY may be stored as a full URL or as a bare key
        if finality.startswith('http'):
            return finality
        return f'https://polygon.api.onfinality.io/rpc?apikey={finality}'
    alchemy = (os.getenv('ALCHEMY_API_KEY') or '').strip()
    if alchemy:
        return f'https://polygon-mainnet.g.alchemy.com/v2/{alchemy}'
    sys.exit('Set ALCHEMY_API_KEY or FINALITY_API_KEY')

RPC = build_rpc()
PK  = (os.getenv('PRIVATE_KEY') or '').strip()
if not PK:
    sys.exit('Set PRIVATE_KEY')
if not PK.startswith('0x'):
    PK = '0x' + PK

w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={'timeout': 30}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
print(f'Connected: block={w3.eth.block_number:,}')

info    = json.loads(ADDR_FILE.read_text())
contract_addr = Web3.to_checksum_address(info['address'])
abi     = json.loads(ABI_FILE.read_text())
contract= w3.eth.contract(address=contract_addr, abi=abi)
account = w3.eth.account.from_key(PK)

print(f'Contract : {contract_addr}')
print(f'Wallet   : {account.address}')

owner = contract.functions.owner().call()
if owner.lower() != account.address.lower():
    sys.exit(f'Wallet is not owner (owner={owner})')

# All tokens the contract may need to approve
TOKENS = {
    'USDC (bridged)': '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',
    'USDC (native)':  '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359',
    'USDT':           '0xc2132D05D31c914a87C6611C10748AEb04B58e8F',
    'WETH':           '0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619',
    'WBTC':           '0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6',
    'WPOL':           '0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270',
    'DAI':            '0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063',
    'LINK':           '0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39',
    'AAVE':           '0xD6DF932A45C0f255f85145f286eA0b292B21C90B',
    'CRV':            '0x172370d5Cd63279eFa6d502DAB29171933a610AF',
    'BAL':            '0x9A71012b13ca4d3D0cDc72a177DF3eF03b0E76a7',
    'wstETH':         '0x03b54A6e9a984069379fae1a4fC4dBAE93B3bCCD',
}

def send(fn, gas=500_000):
    gp    = w3.eth.gas_price
    nonce = w3.eth.get_transaction_count(account.address, 'latest')
    tx    = fn.build_transaction({
        'from': account.address, 'nonce': nonce,
        'gas': gas, 'gasPrice': gp, 'chainId': 137,
    })
    signed  = w3.eth.account.sign_transaction(tx, PK)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f'  TX: {tx_hash.hex()}')
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
    if receipt['status'] == 1:
        print(f'  ✅ gas_used={receipt["gasUsed"]:,}')
    else:
        print(f'  ❌ REVERTED')
    return receipt['status'] == 1

ERC20_ABI = [{'name':'allowance','inputs':[{'name':'o','type':'address'},{'name':'s','type':'address'}],'outputs':[{'name':'','type':'uint256'}],'stateMutability':'view','type':'function'}]
BALANCER  = Web3.to_checksum_address('0xBA12222222228d8Ba445958a75a0704d566BF2C8')
COMET     = Web3.to_checksum_address('0xF25212E676D1F7F89Cd72fFEe66158f541246445')

print()
succeeded = 0
def safe_allowance(tok, owner, spender):
    """Return allowance or 0 on any failure (some tokens have non-standard ABI)."""
    try:
        return tok.functions.allowance(owner, spender).call()
    except Exception:
        return 0   # treat as zero — better to re-approve than to skip

for sym, addr in TOKENS.items():
    tok_cs = Web3.to_checksum_address(addr)
    tok = w3.eth.contract(address=tok_cs, abi=ERC20_ABI)
    bal_allowance   = safe_allowance(tok, contract_addr, BALANCER)
    comet_allowance = safe_allowance(tok, contract_addr, COMET)
    if bal_allowance > 2**200 and comet_allowance > 2**200:
        print(f'[SKIP] {sym}: Balancer + Comet already MAX')
        continue
    print(f'[APPROVING] {sym} ({addr[:12]}...)  Balancer={bal_allowance} Comet={comet_allowance}')
    ok = send(contract.functions.approveTokenForAllSpenders(tok_cs))
    if ok:
        succeeded += 1
    time.sleep(1)   # avoid nonce collision

print()
print(f'Done: {succeeded} tokens approved')
