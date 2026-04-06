import os
import re
import io
import json
import time
import uuid
import random
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import requests
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from openai import OpenAI

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


load_dotenv()

# -----------------------------------------------------------------------------
# ENV / CONFIG
# -----------------------------------------------------------------------------

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()

# Multi-chain support
ACTIVE_CHAINS_RAW = os.getenv("ACTIVE_CHAINS", "").strip()
LEGACY_CHAIN = os.getenv("POIDH_CHAIN", "").strip().lower()
LEGACY_RPC_URL = os.getenv("RPC_URL", "").strip()

RPC_URL_ARBITRUM = os.getenv("RPC_URL_ARBITRUM", "").strip()
RPC_URL_BASE = os.getenv("RPC_URL_BASE", "").strip()
RPC_URL_DEGEN = os.getenv("RPC_URL_DEGEN", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
NEYNAR_API_KEY = os.getenv("NEYNAR_API_KEY", "").strip()
NEYNAR_SIGNER_UUID = os.getenv("NEYNAR_SIGNER_UUID", "").strip()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))
AUTO_BOUNTY_PREFERRED_AMOUNT = Decimal(os.getenv("AUTO_BOUNTY_PREFERRED_AMOUNT", "1000"))
OPEN_BOUNTY_PROBABILITY = float(os.getenv("OPEN_BOUNTY_PROBABILITY", "0.65"))
MAX_EVAL_PER_RUN = int(os.getenv("MAX_EVAL_PER_RUN", "3"))
EVAL_DELAY_SECONDS = int(os.getenv("EVAL_DELAY_SECONDS", "120"))

BOT_PERSONA = os.getenv(
    "BOT_PERSONA",
    "You are a friendly autonomous bounty agent."
).strip()

AUTO_BOUNTY_THEME = os.getenv(
    "AUTO_BOUNTY_THEME",
    "Create simple real-life photo or video tasks people can complete quickly."
).strip()

ABI_FILE = "poidh_abi.json"
STATE_FILE = "state.json"
USER_AGENT = "poidh-autonomous-bot/10.0"
CALL_TIMEOUT = 20

if not PRIVATE_KEY:
    raise ValueError("Missing PRIVATE_KEY in .env")
if not OPENAI_API_KEY:
    raise ValueError("Missing OPENAI_API_KEY in .env")

CHAIN_CONFIG: Dict[str, Dict[str, Any]] = {
    "arbitrum": {
        "contract": "0x5555Fa783936C260f77385b4E153B9725feF1719",
        "base_url": "https://poidh.xyz/arbitrum",
        "v2_offset": 180,
        "symbol": "ETH",
        "explorer_tx": "https://arbiscan.io/tx/",
        "rpc_url": RPC_URL_ARBITRUM,
        "gas_buffer_native": Decimal("0.001"),
    },
    "base": {
        "contract": "0x5555Fa783936C260f77385b4E153B9725feF1719",
        "base_url": "https://poidh.xyz/base",
        "v2_offset": 986,
        "symbol": "ETH",
        "explorer_tx": "https://basescan.org/tx/",
        "rpc_url": RPC_URL_BASE,
        "gas_buffer_native": Decimal("0.001"),
    },
    "degen": {
        "contract": "0x18E5585ca7cE31b90Bc8BB7aAf84152857cE243f",
        "base_url": "https://poidh.xyz/degen",
        "v2_offset": 1197,
        "symbol": "DEGEN",
        "explorer_tx": "https://explorer.degen.tips/tx/",
        "rpc_url": RPC_URL_DEGEN,
        "gas_buffer_native": Decimal("2"),
    },
}

with open(ABI_FILE, "r", encoding="utf-8") as f:
    POIDH_ABI = json.load(f)

ERC721_TOKEN_URI_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
        "name": "tokenURI",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    }
]

openai_client = OpenAI(api_key=OPENAI_API_KEY)


# -----------------------------------------------------------------------------
# CHAIN SETUP
# -----------------------------------------------------------------------------

def resolve_active_chains() -> List[str]:
    chains: List[str] = []

    if ACTIVE_CHAINS_RAW:
        for c in [x.strip().lower() for x in ACTIVE_CHAINS_RAW.split(",") if x.strip()]:
            if c in CHAIN_CONFIG:
                chains.append(c)

    if not chains and LEGACY_CHAIN in CHAIN_CONFIG and LEGACY_RPC_URL:
        chains = [LEGACY_CHAIN]
        CHAIN_CONFIG[LEGACY_CHAIN]["rpc_url"] = LEGACY_RPC_URL

    if not chains:
        for c, cfg in CHAIN_CONFIG.items():
            if cfg["rpc_url"]:
                chains.append(c)

    if not chains:
        raise ValueError(
            "No active chain configured. Set ACTIVE_CHAINS and RPC_URL_<CHAIN>, "
            "or use legacy RPC_URL + POIDH_CHAIN."
        )

    return chains


ACTIVE_CHAINS = resolve_active_chains()

CHAIN_CTX: Dict[str, Dict[str, Any]] = {}
for chain in ACTIVE_CHAINS:
    rpc_url = CHAIN_CONFIG[chain]["rpc_url"]
    if not rpc_url:
        raise ValueError(f"Missing RPC URL for active chain '{chain}'.")

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
    try:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    except Exception:
        pass

    account = w3.eth.account.from_key(PRIVATE_KEY)
    poidh = w3.eth.contract(
        address=Web3.to_checksum_address(CHAIN_CONFIG[chain]["contract"]),
        abi=POIDH_ABI,
    )

    CHAIN_CTX[chain] = {
        "chain": chain,
        "w3": w3,
        "account": account,
        "poidh": poidh,
        "rpc_url": rpc_url,
        "contract_address": CHAIN_CONFIG[chain]["contract"],
        "base_url": CHAIN_CONFIG[chain]["base_url"],
        "v2_offset": CHAIN_CONFIG[chain]["v2_offset"],
        "symbol": CHAIN_CONFIG[chain]["symbol"],
        "explorer_tx": CHAIN_CONFIG[chain]["explorer_tx"],
        "gas_buffer_native": CHAIN_CONFIG[chain]["gas_buffer_native"],
    }

# -----------------------------------------------------------------------------
# STATE
# -----------------------------------------------------------------------------

def default_state() -> Dict[str, Any]:
    return {
        "tracked_bounties": [],
        "seen_claim_ids": {},
        "processed_winners": {},
        "decision_casts": {},
        "reply_hashes_seen": [],
        "reasoning": {},
        "vote_submissions": {},
        "resolved_votes": {},
        "last_bounty_spec": None,
        "chain_rotation_index": 0,
        "last_reply_time": 0,
    }


def bounty_key(chain: str, bounty_id: int) -> str:
    return f"{chain}:{int(bounty_id)}"


def parse_bounty_key(key: str) -> Tuple[str, int]:
    if ":" not in key:
        chain = LEGACY_CHAIN if LEGACY_CHAIN in CHAIN_CONFIG else ACTIVE_CHAINS[0]
        return chain, int(key)
    chain, raw_id = key.split(":", 1)
    return chain, int(raw_id)


def normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    out = default_state()
    out.update(state or {})

    legacy_chain = LEGACY_CHAIN if LEGACY_CHAIN in CHAIN_CONFIG else ACTIVE_CHAINS[0]

    normalized_tracked = []
    for item in out.get("tracked_bounties", []):
        if isinstance(item, int):
            normalized_tracked.append(bounty_key(legacy_chain, item))
        elif isinstance(item, str):
            if ":" in item:
                normalized_tracked.append(item)
            elif item.isdigit():
                normalized_tracked.append(bounty_key(legacy_chain, int(item)))
    out["tracked_bounties"] = normalized_tracked

    for field in [
        "seen_claim_ids",
        "processed_winners",
        "decision_casts",
        "reasoning",
        "vote_submissions",
        "resolved_votes",
    ]:
        original = out.get(field, {}) or {}
        normalized = {}
        for k, v in original.items():
            nk = k
            if isinstance(k, int):
                nk = bounty_key(legacy_chain, k)
            elif isinstance(k, str) and ":" not in k and k.isdigit():
                nk = bounty_key(legacy_chain, int(k))
            normalized[nk] = v
        out[field] = normalized

    out["reply_hashes_seen"] = list(out.get("reply_hashes_seen", []))[-500:]
    return out


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        state = default_state()
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        return state

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    state = normalize_state(raw)
    save_state(state)
    return state


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# -----------------------------------------------------------------------------
# UTILS
# -----------------------------------------------------------------------------

def short_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr else "N/A"


def zero_address() -> str:
    return "0x0000000000000000000000000000000000000000"


def wei_to_native(ctx: Dict[str, Any], value: int) -> str:
    return str(ctx["w3"].from_wei(value, "ether"))


def ensure_http_url(uri: str) -> str:
    if not uri:
        return uri
    uri = uri.strip()
    if uri.startswith("ipfs://"):
        return "https://ipfs.io/ipfs/" + uri.replace("ipfs://", "", 1).lstrip("/")
    if uri.startswith("ar://"):
        return "https://arweave.net/" + uri.replace("ar://", "", 1).lstrip("/")
    return uri


def strip_html(html: str) -> str:
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def parse_json_object(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ValueError(f"Could not parse JSON from model output:\n{text}")
        return json.loads(match.group(0))


def safe_get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[requests.Response]:
    try:
        res = requests.get(url, headers=headers, params=params, timeout=30)
        if res.status_code == 429:
            print(f"Rate limited on GET {url}. Sleeping 60s...")
            time.sleep(60)
            return None
        res.raise_for_status()
        return res
    except Exception as e:
        print(f"GET error for {url}: {e}")
        return None


def safe_post(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Optional[requests.Response]:
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=30)
        if res.status_code == 429:
            print(f"Rate limited on POST {url}. Sleeping 60s...")
            time.sleep(60)
            return None
        res.raise_for_status()
        return res
    except Exception as e:
        print(f"POST error for {url}: {e}")
        return None


def guess_mime_type(url: str, content_type: str = "") -> str:
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return ct.split(";")[0]
    lower = url.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def call_with_timeout(func, timeout: int = CALL_TIMEOUT):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func)
        return future.result(timeout=timeout)


def contract_call(fn, timeout: int = CALL_TIMEOUT):
    try:
        return call_with_timeout(lambda: fn.call(), timeout=timeout)
    except FuturesTimeoutError:
        raise TimeoutError("Contract call timed out.")


def tx_common(ctx: Dict[str, Any]) -> Dict[str, Any]:
    w3 = ctx["w3"]
    account = ctx["account"]
    return {
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": w3.eth.chain_id,
    }


def send_contract_tx(ctx: Dict[str, Any], fn, value_wei: int = 0) -> str:
    w3 = ctx["w3"]
    account = ctx["account"]

    estimate_args = {"from": account.address}
    if value_wei:
        estimate_args["value"] = value_wei

    gas_estimate = call_with_timeout(lambda: fn.estimate_gas(estimate_args), timeout=CALL_TIMEOUT)
    tx = fn.build_transaction(
        {
            **tx_common(ctx),
            "value": value_wei,
            "gas": int(gas_estimate * 1.2),
            "gasPrice": w3.eth.gas_price,
        }
    )

    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def get_tx_link(chain: str, tx_hash: str) -> str:
    return f"{CHAIN_CONFIG[chain]['explorer_tx']}{tx_hash}"


def format_chain_label(chain: str) -> str:
    return chain.upper()

# -----------------------------------------------------------------------------
# CHAIN + CONTRACT HELPERS
# -----------------------------------------------------------------------------

def get_minimums(ctx: Dict[str, Any]) -> Tuple[int, int]:
    poidh = ctx["poidh"]
    min_bounty = int(contract_call(poidh.functions.MIN_BOUNTY_AMOUNT()))
    min_contribution = int(contract_call(poidh.functions.MIN_CONTRIBUTION()))
    return min_bounty, min_contribution


def bounty_to_dict(bounty: Any) -> Dict[str, Any]:
    return {
        "id": int(bounty[0]),
        "issuer": bounty[1],
        "name": bounty[2],
        "description": bounty[3],
        "amount": int(bounty[4]),
        "claimer": bounty[5],
        "createdAt": int(bounty[6]),
        "claimId": int(bounty[7]),
    }


def claim_to_dict(claim: Any) -> Dict[str, Any]:
    return {
        "id": int(claim[0]),
        "issuer": claim[1],
        "bountyId": int(claim[2]),
        "bountyIssuer": claim[3],
        "name": claim[4],
        "description": claim[5],
        "createdAt": int(claim[6]),
        "accepted": bool(claim[7]),
    }


def get_bounty(ctx: Dict[str, Any], bounty_id: int) -> Dict[str, Any]:
    raw = contract_call(ctx["poidh"].functions.bounties(bounty_id))
    return bounty_to_dict(raw)


def bounty_is_active(bounty: Dict[str, Any]) -> bool:
    return bounty["claimer"].lower() == zero_address().lower()


def fetch_claims(ctx: Dict[str, Any], bounty_id: int) -> List[Dict[str, Any]]:
    print(f"[{ctx['chain']}] Fetching claims for bounty #{bounty_id}...")
    batch = contract_call(ctx["poidh"].functions.getClaimsByBountyId(bounty_id, 0))
    claims = [claim_to_dict(c) for c in batch if c[0] != 0]
    print(f"[{ctx['chain']}] Returned {len(claims)} claims for bounty #{bounty_id}: {[c['id'] for c in claims]}")
    return claims


def get_claim_uri(ctx: Dict[str, Any], claim_id: int) -> Optional[str]:
    try:
        nft_address = Web3.to_checksum_address(contract_call(ctx["poidh"].functions.poidhNft()))
        nft = ctx["w3"].eth.contract(address=nft_address, abi=ERC721_TOKEN_URI_ABI)
        return contract_call(nft.functions.tokenURI(claim_id))
    except Exception as e:
        print(f"[{ctx['chain']}] Could not fetch tokenURI for claim #{claim_id}: {e}")
        return None


def accept_claim(ctx: Dict[str, Any], bounty_id: int, claim_id: int) -> str:
    return send_contract_tx(ctx, ctx["poidh"].functions.acceptClaim(bounty_id, claim_id))


def submit_claim_for_vote(ctx: Dict[str, Any], bounty_id: int, claim_id: int) -> str:
    return send_contract_tx(ctx, ctx["poidh"].functions.submitClaimForVote(bounty_id, claim_id))


def resolve_vote(ctx: Dict[str, Any], bounty_id: int) -> str:
    return send_contract_tx(ctx, ctx["poidh"].functions.resolveVote(bounty_id))


# -----------------------------------------------------------------------------
# CONTENT RESOLUTION
# -----------------------------------------------------------------------------

def read_pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    if PdfReader is None:
        return "PDF found, but pypdf is not installed."
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for page in reader.pages[:10]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(pages).strip()[:12000]
    except Exception as e:
        return f"Failed to read PDF: {e}"


def resolve_claim_content(uri: Optional[str]) -> Dict[str, Any]:
    result = {
        "original_uri": uri,
        "resolved_uri": None,
        "kind": "unknown",
        "content_url": None,
        "text": None,
        "metadata": None,
        "preview_image": None,
        "notes": [],
    }

    if not uri:
        result["notes"].append("No URI found.")
        return result

    url = ensure_http_url(uri)
    result["resolved_uri"] = url
    result["content_url"] = url

    if not (url.startswith("http://") or url.startswith("https://")):
        result["notes"].append("URI is not fetchable over HTTP/HTTPS.")
        return result

    resp = safe_get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    if not resp:
        result["notes"].append("Could not fetch content.")
        return result

    try:
        content_type = (resp.headers.get("Content-Type") or "").lower()

        if "application/json" in content_type or url.endswith(".json"):
            data = resp.json()
            result["metadata"] = data
            result["kind"] = "metadata"

            candidate = data.get("animation_url") or data.get("image") or data.get("external_url")
            if candidate:
                candidate = ensure_http_url(candidate)
                result["content_url"] = candidate

                if candidate.lower().endswith(".pdf"):
                    result["kind"] = "pdf"
                    pdf_resp = safe_get(candidate, headers={"User-Agent": USER_AGENT})
                    if pdf_resp:
                        result["text"] = read_pdf_text_from_bytes(pdf_resp.content)
                elif re.search(r"\.(png|jpg|jpeg|webp|gif)$", candidate, re.I):
                    result["kind"] = "image"
                    result["preview_image"] = candidate
                elif re.search(r"\.(mp4|mov|webm|m4v)$", candidate, re.I):
                    result["kind"] = "video"

            preview = data.get("image")
            if preview:
                result["preview_image"] = ensure_http_url(preview)

            return result

        if "image/" in content_type:
            result["kind"] = "image"
            result["preview_image"] = url
            return result

        if "video/" in content_type:
            result["kind"] = "video"
            return result

        if "pdf" in content_type or url.lower().endswith(".pdf"):
            result["kind"] = "pdf"
            result["text"] = read_pdf_text_from_bytes(resp.content)
            return result

        if "text/html" in content_type:
            result["kind"] = "webpage"
            result["text"] = strip_html(resp.text)[:12000]
            return result

        if "text/" in content_type:
            result["kind"] = "text"
            result["text"] = resp.text[:12000]
            return result

        result["notes"].append(f"Unhandled content type: {content_type}")
        return result

    except Exception as e:
        result["notes"].append(f"Content resolution error: {e}")
        return result

# -----------------------------------------------------------------------------
# OPENAI (Evaluation + Replies)
# -----------------------------------------------------------------------------

def get_response_text(response) -> str:
    try:
        return response.output[0].content[0].text.strip()
    except Exception:
        return str(response)


def call_openai(prompt: str) -> str:
    try:
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
        )
        return get_response_text(response)
    except Exception as e:
        print(f"OpenAI error: {e}")
        return ""


def evaluate_claim_with_openai(
    bounty: Dict[str, Any],
    claim: Dict[str, Any],
    content: Dict[str, Any],
) -> Dict[str, Any]:

    prompt = f"""
You are evaluating a bounty submission.

Bounty:
{bounty['name']}
{bounty['description']}

Claim:
{claim['name']}
{claim['description']}

Content type: {content['kind']}
Text: {content.get('text', '')}

Score based on:
- relevance (0-10)
- quality (0-10)
- authenticity (0-10)

Return JSON:
{{
  "relevance": int,
  "quality": int,
  "authenticity": int,
  "reason": "short explanation"
}}
"""

    result = call_openai(prompt)

    try:
        parsed = parse_json_object(result)
        parsed["total"] = parsed["relevance"] + parsed["quality"] + parsed["authenticity"]
        return parsed
    except Exception:
        return {
            "relevance": 5,
            "quality": 5,
            "authenticity": 5,
            "reason": "Fallback scoring",
            "total": 15,
        }


def generate_reply_with_openai(user_text: str, context_json: str) -> str:
    text = user_text.lower()

    try:
        context = json.loads(context_json) if isinstance(context_json, str) else context_json
    except Exception:
        context = {}

    link_keywords = [
        "link",
        "bounty link",
        "share a link",
        "send a link",
        "i want the link",
        "can i get the link",
        "can you share the link",
        "share the bounty",
        "where is the bounty",
        "where's the bounty",
        "show me the bounty",
    ]

    if any(keyword in text for keyword in link_keywords):
        chain = context.get("chain")
        bounty_id = context.get("bounty_id")

        if chain and bounty_id is not None:
            offset = CHAIN_CONFIG[chain]["v2_offset"]
            link = f"{CHAIN_CONFIG[chain]['base_url']}/bounty/{bounty_id + offset}"
            return f"Here’s the bounty link:\n{link}"

    prompt = f"""
You are an autonomous bounty bot.

Someone asked:
"{user_text}"

Context:
{json.dumps(context, ensure_ascii=False)}

Reply clearly, naturally, and briefly.
"""

    response = call_openai(prompt)
    return response or "I selected the best claim based on relevance, quality, and authenticity."



# -----------------------------------------------------------------------------
# BOUNTY CREATION / TYPE / CHAIN CHOICE
# -----------------------------------------------------------------------------

def choose_bounty_type() -> str:
    return "open" if random.random() < OPEN_BOUNTY_PROBABILITY else "solo"


def get_chain_balance_wei(ctx: Dict[str, Any]) -> int:
    return int(ctx["w3"].eth.get_balance(ctx["account"].address))


def chain_is_funded_for_new_bounty(ctx: Dict[str, Any]) -> bool:
    try:
        min_bounty, _ = get_minimums(ctx)
        preferred = int(ctx["w3"].to_wei(AUTO_BOUNTY_PREFERRED_AMOUNT, "ether"))
        target = max(min_bounty, preferred)
        gas_buffer = int(ctx["w3"].to_wei(ctx["gas_buffer_native"], "ether"))
        balance = get_chain_balance_wei(ctx)
        return balance >= (target + gas_buffer)
    except Exception as e:
        print(f"[{ctx['chain']}] Funding check failed: {e}")
        return False


def pick_creation_chain(state: Dict[str, Any]) -> Optional[str]:
    funded = [chain for chain in ACTIVE_CHAINS if chain_is_funded_for_new_bounty(CHAIN_CTX[chain])]
    if not funded:
        return None

    idx = int(state.get("chain_rotation_index", 0)) % len(funded)
    chosen = funded[idx]
    state["chain_rotation_index"] = idx + 1
    save_state(state)
    return chosen


def generate_bounty_spec() -> Dict[str, str]:
    prompt = f"""
You are creating a new bounty for Pics Or It Didn't Happen.

Theme:
{AUTO_BOUNTY_THEME}

Rules:
- Make it a simple real-world task.
- It must be doable with a phone.
- Keep it safe, short, and clear.
- It can involve a photo or short video.
- Avoid anything dangerous or private.

Return JSON:
{{
  "title": "short bounty title",
  "description": "clear bounty description"
}}
"""
    result = call_openai(prompt)

    try:
        return parse_json_object(result)
    except Exception:
        return {
            "title": "Show a real-world moment",
            "description": "Share a clear real-world proof of the task using a photo or short video."
        }


def create_bounty(ctx: Dict[str, Any], title: str, description: str) -> Tuple[str, Optional[int], Optional[str], str]:
    min_bounty, _ = get_minimums(ctx)
    preferred = int(ctx["w3"].to_wei(AUTO_BOUNTY_PREFERRED_AMOUNT, "ether"))
    amount_wei = max(min_bounty, preferred)

    bounty_type = choose_bounty_type()

    if bounty_type == "solo":
        fn = ctx["poidh"].functions.createSoloBounty(title, description)
    else:
        fn = ctx["poidh"].functions.createOpenBounty(title, description)

    tx_hash = send_contract_tx(ctx, fn, value_wei=amount_wei)
    print(f"[{ctx['chain']}] Bounty tx sent: {tx_hash}")

    try:
        receipt = ctx["w3"].eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        logs = ctx["poidh"].events.BountyCreated().process_receipt(receipt)
        if logs:
            bounty_id = int(logs[0]["args"]["id"])
            frontend_id = bounty_id + ctx["v2_offset"]
            link = f"{ctx['base_url']}/bounty/{frontend_id}"
            return tx_hash, bounty_id, link, bounty_type
    except Exception as e:
        print(f"[{ctx['chain']}] Could not decode bounty ID: {e}")

    return tx_hash, None, None, bounty_type


# -----------------------------------------------------------------------------
# FORMATTING
# -----------------------------------------------------------------------------

def format_voting_post(chain: str, bounty_id: int, claim_id: int, title: str, score: Dict[str, Any]) -> str:
    return f"""Claim Selected for Voting

Chain: {format_chain_label(chain)}
Bounty #{bounty_id}
Claim #{claim_id} — {title}

Relevance: {score['relevance']}/10
Quality: {score['quality']}/10
Authenticity: {score['authenticity']}/10

Total Score: {score['total']}/30

{score['reason']}"""


def format_winner_post(chain: str, bounty_id: int, claim_id: int, title: str, score: Dict[str, Any]) -> str:
    return f"""Winner Finalized

Chain: {format_chain_label(chain)}
Bounty #{bounty_id}
Claim #{claim_id} — {title}

Relevance: {score['relevance']}/10
Quality: {score['quality']}/10
Authenticity: {score['authenticity']}/10

Total Score: {score['total']}/30

{score['reason']}"""


# -----------------------------------------------------------------------------
# FARCASTER
# -----------------------------------------------------------------------------

def post_to_farcaster(text: str, parent_hash: Optional[str] = None, parent_fid: Optional[int] = None):
    if not NEYNAR_API_KEY or not NEYNAR_SIGNER_UUID:
        print("Farcaster not configured, skipping post.")
        return None

    url = "https://api.neynar.com/v2/farcaster/cast"
    headers = {
        "Content-Type": "application/json",
        "api_key": NEYNAR_API_KEY,
        "x-api-key": NEYNAR_API_KEY,
    }

    payload: Dict[str, Any] = {
        "signer_uuid": NEYNAR_SIGNER_UUID,
        "text": text[:320],
    }

    if parent_hash:
        payload["parent"] = parent_hash
    if parent_fid is not None:
        payload["parent_author_fid"] = int(parent_fid)

    res = safe_post(url, headers, payload)
    if res:
        print("Posted to Farcaster.")
        try:
            return res.json()
        except Exception:
            return None
    return None


def fetch_replies_to_cast(parent_fid: int, parent_hash: str) -> List[Dict[str, Any]]:
    if not NEYNAR_API_KEY:
        return []

    url = "https://snapchain-api.neynar.com/v1/castsByParent"
    headers = {"x-api-key": NEYNAR_API_KEY}
    params = {"fid": int(parent_fid), "hash": parent_hash}

    res = safe_get(url, headers=headers, params=params)
    if not res:
        return []

    try:
        data = res.json()
        return data.get("messages", [])
    except Exception as e:
        print(f"Reply fetch parse error: {e}")
        return []


def parse_reply_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    data = msg.get("data", {})
    body = data.get("castAddBody", {})
    return {
        "hash": msg.get("hash"),
        "fid": data.get("fid"),
        "text": body.get("text", ""),
    }


def should_answer_reply(text: str) -> bool:
    t = (text or "").lower().strip()
    if len(t) < 5:
        return False
    keywords = [
        "why", "how", "reason", "picked", "winner",
        "authentic", "quality", "relevance", "explain", "logic"
    ]
    return any(k in t for k in keywords)


# -----------------------------------------------------------------------------
# PROCESSING
# -----------------------------------------------------------------------------

def bounty_needs_ai_review(state: Dict[str, Any], key: str, current_claim_ids: List[int]) -> bool:
    last_ids = state.get("seen_claim_ids", {}).get(key, [])

    if current_claim_ids != last_ids:
        state.setdefault("seen_claim_ids", {})[key] = current_claim_ids
        save_state(state)
        return True

    return False


def evaluate_all_claims(state: Dict[str, Any], ctx: Dict[str, Any], bounty: Dict[str, Any], claims: List[Dict[str, Any]]):
    results = []
    count = 0

    for claim in claims:
        if count >= MAX_EVAL_PER_RUN:
            print(f"[{ctx['chain']}] Max evaluations reached for this run.")
            break

        print(f"[{ctx['chain']}] Evaluating claim #{claim['id']}...")
        uri = get_claim_uri(ctx, claim["id"])
        content = resolve_claim_content(uri)
        score = evaluate_claim_with_openai(bounty, claim, content)

        if score:
            results.append({"claim": claim, "score": score, "content": content, "uri": uri})
        
        try:
            print("checking farcaster replies...")
            process_farcaster_replies(state)
        except Exception as e:
            print(f"Reply failed, skipping: {e}")

        count += 1
        print(f"Sleeping {EVAL_DELAY_SECONDS}s to avoid rate limits...")
        time.sleep(EVAL_DELAY_SECONDS)

    return results

def get_latest_bounty_id(ctx: Dict[str, Any], max_lookback: int = 5000) -> Optional[int]:
    """
    Find the latest existing bounty ID by scanning backwards.
    """
    for bounty_id in range(max_lookback, -1, -1):
        try:
            _ = get_bounty(ctx, bounty_id)
            return bounty_id
        except Exception:
            continue
    return None


def discover_active_bounties_across_chains(scan_depth: int = 20) -> List[str]:
    """
    Check recent bounty IDs on every active chain and return active ones.
    """
    discovered: List[str] = []

    for chain in ACTIVE_CHAINS:
        ctx = CHAIN_CTX[chain]
        latest_id = get_latest_bounty_id(ctx)

        if latest_id is None:
            print(f"[{chain}] Could not discover latest bounty ID.")
            continue

        start_id = max(0, latest_id - scan_depth + 1)

        for bounty_id in range(latest_id, start_id - 1, -1):
            try:
                bounty = get_bounty(ctx, bounty_id)
                if bounty_is_active(bounty):
                    discovered.append(bounty_key(chain, bounty_id))
            except Exception:
                continue

    return sorted(set(discovered))

def maybe_create_new_bounty(state: Dict[str, Any]) -> None:
    cleaned: List[str] = []

    # First, keep any tracked bounties that are still active
    for key in list(state.get("tracked_bounties", [])):
        try:
            chain, bounty_id = parse_bounty_key(key)
            if chain not in CHAIN_CTX:
                continue
            ctx = CHAIN_CTX[chain]
            bounty = get_bounty(ctx, bounty_id)
            if bounty_is_active(bounty):
                cleaned.append(key)
        except Exception as e:
            print(f"Could not check tracked bounty {key}: {e}")

    # If nothing valid remains, discover active bounties across all chains
    if not cleaned:
        print("No valid tracked bounties found. Scanning all chains for active bounties...")
        cleaned = discover_active_bounties_across_chains(scan_depth=20)

    state["tracked_bounties"] = sorted(set(cleaned))
    save_state(state)

    if state["tracked_bounties"]:
        print(f"Active tracked bounties: {state['tracked_bounties']}")
        print("Bot will monitor existing bounty/bounties instead of creating a new one.")
        return

    # Only create a new bounty if no active bounty exists anywhere
    chosen_chain = pick_creation_chain(state)
    if not chosen_chain:
        print("No funded chains available for new bounty creation.")
        return

    ctx = CHAIN_CTX[chosen_chain]
    print(f"[{chosen_chain}] No active bounties found on any chain. Generating a new bounty...")

    spec = generate_bounty_spec()
    title = spec["title"].strip()[:120]
    description = spec["description"].strip()[:1500]

    tx_hash, bounty_id, link, bounty_type = create_bounty(ctx, title, description)
    if bounty_id is not None:
        key = bounty_key(chosen_chain, bounty_id)
        state["tracked_bounties"] = [key]
        state["last_bounty_spec"] = spec
        save_state(state)

        print(f"[{chosen_chain}] New bounty ID: {bounty_id}")
        if link:
            print(f"[{chosen_chain}] View at: {link}")
            post_to_farcaster(
                f"New {bounty_type} bounty live\n\nChain: {format_chain_label(chosen_chain)}\n{title}\n\n{link}"
            )
    else:
        print(f"[{chosen_chain}] Bounty tx sent but ID could not be decoded: {tx_hash}")



def process_bounty(state: Dict[str, Any], key: str) -> None:
    chain, bounty_id = parse_bounty_key(key)
    if chain not in CHAIN_CTX:
        print(f"Skipping {key}: chain '{chain}' is not active.")
        return

    ctx = CHAIN_CTX[chain]
    print(f"[{chain}] Starting process_bounty for #{bounty_id}")

    try:
        bounty = get_bounty(ctx, bounty_id)
    except Exception as e:
        print(f"[{chain}] Skipping bounty #{bounty_id}: {e}")
        return

    if not bounty_is_active(bounty):
        print(f"[{chain}] Bounty #{bounty_id} finalized. Removing from tracking.")
        state["tracked_bounties"] = [b for b in state["tracked_bounties"] if b != key]
        save_state(state)
        return

    claims = fetch_claims(ctx, bounty_id)
    claim_ids = [c["id"] for c in claims]

    if not claim_ids:
        print(f"[{chain}] No claims yet for bounty #{bounty_id}.")
        return

    if not bounty_needs_ai_review(state, key, claim_ids):
        print(f"[{chain}] No new claim changes for bounty #{bounty_id}. Skipping OpenAI.")
        return

    scored_claims = evaluate_all_claims(state, ctx, bounty, claims)
    if not scored_claims:
        print(f"[{chain}] No valid scored claims for bounty #{bounty_id}.")
        return

    best = max(scored_claims, key=lambda x: x["score"]["total"])
    claim = best["claim"]
    score = best["score"]
    content = best["content"]
    claim_uri = best["uri"]

    if state.get("processed_winners", {}).get(key) == claim["id"]:
        print(f"[{chain}] Claim #{claim['id']} already processed for bounty #{bounty_id}.")
        return

    reasoning_context = {
        "chain": chain,
        "bounty_id": bounty_id,
        "claim_id": claim["id"],
        "claim_name": claim["name"],
        "claim_description": claim["description"],
        "claim_uri": claim_uri,
        "content_url": content.get("content_url"),
        "content_kind": content.get("kind"),
        "score": score,
    }
    state.setdefault("reasoning", {})[key] = reasoning_context
    save_state(state)

    had_external = bool(contract_call(ctx["poidh"].functions.everHadExternalContributor(bounty_id)))

    if had_external:
        tx_hash = submit_claim_for_vote(ctx, bounty_id, claim["id"])
        print(f"[{chain}] Vote submitted for bounty #{bounty_id}: {tx_hash}")

        state.setdefault("processed_winners", {})[key] = claim["id"]
        state.setdefault("vote_submissions", {})[key] = {
            "claim_id": claim["id"],
            "claim_title": claim["name"],
            "score": score,
            "tx_hash": tx_hash,
            "submitted_at": int(time.time()),
        }
        save_state(state)

        post_text = format_voting_post(chain, bounty_id, claim["id"], claim["name"], score)
        cast = post_to_farcaster(post_text)

        if cast and cast.get("cast"):
            state.setdefault("decision_casts", {})[key] = {
                "hash": cast["cast"].get("hash"),
                "fid": cast["cast"].get("author", {}).get("fid"),
                "reason": score["reason"],
                "context": reasoning_context,
            }
            save_state(state)

    else:
        tx_hash = accept_claim(ctx, bounty_id, claim["id"])
        print(f"[{chain}] Claim accepted for bounty #{bounty_id}: {tx_hash}")

        state.setdefault("processed_winners", {})[key] = claim["id"]
        save_state(state)

        post_text = format_winner_post(chain, bounty_id, claim["id"], claim["name"], score)
        post_text += f"\nTransaction:\n{get_tx_link(chain, tx_hash)}"
        cast = post_to_farcaster(post_text)

        if cast and cast.get("cast"):
            state.setdefault("decision_casts", {})[key] = {
                "hash": cast["cast"].get("hash"),
                "fid": cast["cast"].get("author", {}).get("fid"),
                "reason": score["reason"],
                "context": reasoning_context,
            }
            save_state(state)


def process_vote_resolutions(state: Dict[str, Any]) -> None:
    for key in list(state.get("tracked_bounties", [])):
        chain, bounty_id = parse_bounty_key(key)
        if chain not in CHAIN_CTX:
            continue
        ctx = CHAIN_CTX[chain]

        if key not in state.get("vote_submissions", {}):
            continue

        if state.get("resolved_votes", {}).get(key):
            continue

        try:
            current_vote_claim = int(contract_call(ctx["poidh"].functions.bountyCurrentVotingClaim(int(bounty_id))))
            if current_vote_claim == 0:
                continue

            _yes_w, _no_w, deadline = contract_call(ctx["poidh"].functions.bountyVotingTracker(int(bounty_id)))

            if int(deadline) and int(time.time()) >= int(deadline):
                tx_hash = resolve_vote(ctx, int(bounty_id))
                print(f"[{chain}] Resolved vote for bounty #{bounty_id}: {tx_hash}")

                vote_info = state["vote_submissions"][key]
                score = vote_info["score"]

                post_text = format_winner_post(
                    chain,
                    int(bounty_id),
                    int(vote_info["claim_id"]),
                    vote_info["claim_title"],
                    score,
                )
                post_text += f"\nTransaction:\n{get_tx_link(chain, tx_hash)}"
                cast = post_to_farcaster(post_text)

                if cast and cast.get("cast"):
                    state.setdefault("decision_casts", {})[key] = {
                        "hash": cast["cast"].get("hash"),
                        "fid": cast["cast"].get("author", {}).get("fid"),
                        "reason": score["reason"],
                        "context": state.get("reasoning", {}).get(key, {}),
                    }

                state.setdefault("resolved_votes", {})[key] = {
                    "tx_hash": tx_hash,
                    "resolved_at": int(time.time()),
                }
                save_state(state)

        except Exception as e:
            print(f"[{chain}] Vote resolution check failed for bounty #{bounty_id}: {e}")


def process_farcaster_replies(state: Dict[str, Any]) -> None:
    seen = set(state.get("reply_hashes_seen", []))

    for key, cast_info in state.get("decision_casts", {}).items():
        parent_hash = cast_info.get("hash")
        parent_fid = cast_info.get("fid")
        context = cast_info.get("context") or {
            "reason": cast_info.get("reason", "The decision was based on relevance, quality, and authenticity.")
        }

        if not parent_hash or not parent_fid:
            continue

        replies = fetch_replies_to_cast(int(parent_fid), parent_hash)

def process_farcaster_replies(state: Dict[str, Any]) -> None:
    seen = set(state.get("reply_hashes_seen", []))

    for key, cast_info in state.get("decision_casts", {}).items():
        parent_hash = cast_info.get("hash")
        parent_fid = cast_info.get("fid")
        context = cast_info.get("context") or {
            "reason": cast_info.get("reason", "The decision was based on relevance, quality, and authenticity.")
        }

        if not parent_hash or not parent_fid:
            continue

        replies = fetch_replies_to_cast(int(parent_fid), parent_hash)

        for raw in replies:
            reply = parse_reply_message(raw)
            reply_hash = reply.get("hash")
            reply_text = reply.get("text", "")

            if not reply_hash or reply_hash in seen:
                continue

            if not should_answer_reply(reply_text):
                seen.add(reply_hash)
                continue

            now = time.time()

            # prevent replying too often
            if now - state.get("last_reply_time", 0) < 120:
                print("Reply cooldown active. Skipping reply for now.")
                continue

            answer = generate_reply_with_openai(reply_text, context)
            post_to_farcaster(
                answer,
                parent_hash=reply_hash,
                parent_fid=int(reply["fid"])
            )

            state["last_reply_time"] = now
            seen.add(reply_hash)
            save_state(state)

            print(f"Replied to follow-up on cast for {key}.")

    state["reply_hashes_seen"] = list(seen)[-500:]
    save_state(state)



# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def print_chain_status() -> None:
    print("=" * 88)
    print("POIDH AUTONOMOUS BOT")
    print("=" * 88)
    print(f"OpenAI model: {OPENAI_MODEL}")
    print(f"Active chains: {', '.join([c.upper() for c in ACTIVE_CHAINS])}")
    for chain in ACTIVE_CHAINS:
        ctx = CHAIN_CTX[chain]
        print("-" * 88)
        print(f"Chain: {chain.upper()}")
        print(f"Wallet: {ctx['account'].address} ({short_addr(ctx['account'].address)})")
        print(f"RPC: {ctx['rpc_url']}")
        print(f"Contract: {ctx['contract_address']}")
        try:
            min_bounty, min_contribution = get_minimums(ctx)
            print(f"On-chain minimum bounty: {wei_to_native(ctx, min_bounty)} {ctx['symbol']}")
            print(f"On-chain minimum contribution: {wei_to_native(ctx, min_contribution)} {ctx['symbol']}")
        except Exception as e:
            print(f"Minimums read failed: {e}")


def main() -> None:
    state = load_state()
    print_chain_status()

    while True:
        try:
            print("About to maybe_create_new_bounty()...")
            maybe_create_new_bounty(state)

            print("About to process tracked bounties...")
            for key in list(state.get("tracked_bounties", [])):
                print(f"About to process bounty {key}...")
                process_bounty(state, key)

            print("About to process vote resolutions...")
            process_vote_resolutions(state)

            print("About to process Farcaster replies...")
            process_farcaster_replies(state)

        except Exception as e:
            print(f"Top-level loop error: {e}")

        print("-" * 88)
        print(f"Sleeping for {CHECK_INTERVAL} seconds...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()