import os
import json
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

CHAIN = os.getenv("POIDH_CHAIN", "base").strip().lower()
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()

CHAIN_CONFIG = {
    "base": {
        "rpc_url": "https://mainnet.base.org",
        "contract": "0x5555Fa783936C260f77385b4E153B9725feF1719",
    },
    "arbitrum": {
        "rpc_url": "https://arb1.arbitrum.io/rpc",
        "contract": "0x5555Fa783936C260f77385b4E153B9725feF1719",
    },
    "degen": {
        "rpc_url": "https://rpc.degen.tips",
        "contract": "0x18E5585ca7cE31b90Bc8BB7aAf84152857cE243f",
    },
}

if CHAIN not in CHAIN_CONFIG:
    raise ValueError(f"Unsupported chain: {CHAIN}")

if not PRIVATE_KEY:
    raise ValueError("Missing PRIVATE_KEY in .env")

w3 = Web3(Web3.HTTPProvider(CHAIN_CONFIG[CHAIN]["rpc_url"]))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

with open("poidh_abi.json", "r", encoding="utf-8") as f:
    abi = json.load(f)

contract = w3.eth.contract(
    address=Web3.to_checksum_address(CHAIN_CONFIG[CHAIN]["contract"]),
    abi=abi,
)

account = w3.eth.account.from_key(PRIVATE_KEY)

bounty_id = int(input("Enter bounty ID to cancel: ").strip())
bounty = contract.functions.bounties(bounty_id).call()

issuer = bounty[1]
claimer = bounty[5]

print(f"Wallet: {account.address}")
print(f"Bounty issuer: {issuer}")
print(f"Claimer: {claimer}")

if issuer.lower() != account.address.lower():
    raise ValueError("This wallet is not the issuer of the bounty.")

zero = "0x0000000000000000000000000000000000000000"
if str(claimer).lower() != zero.lower():
    raise ValueError("This bounty is no longer active, so it cannot be cancelled.")

kind = input("Type bounty kind ('solo' or 'open'): ").strip().lower()

if kind == "solo":
    fn = contract.functions.cancelSoloBounty(bounty_id)
elif kind == "open":
    fn = contract.functions.cancelOpenBounty(bounty_id)
else:
    raise ValueError("Invalid kind. Use 'solo' or 'open'.")

gas_estimate = fn.estimate_gas({"from": account.address})
tx = fn.build_transaction(
    {
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": w3.eth.chain_id,
        "gas": int(gas_estimate * 1.2),
        "gasPrice": w3.eth.gas_price,
    }
)

signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

print("Cancel transaction sent:")
print(tx_hash.hex())