# Arbiter Bot — Autonomous POIDH Agent


Arbiter Bot is an autonomous agent that runs POIDH bounties end-to-end with minimal human intervention.

It handles the full lifecycle:
- monitors bounties across chains
- evaluates submissions
- selects winners or submits claims for voting
- resolves votes on-chain automatically
- posts decisions with reasoning to Farcaster
- responds to follow-up questions from users

---

##  Core Objective

To create a transparent, **auditable, and autonomous system** for selecting bounty winners without manual bias.

---

##  Winner Selection Logic

Each claim is evaluated using three criteria:

- **Relevance (0–10)**  
  How well the submission matches the bounty prompt  

- **Quality (0–10)**  
  Clarity, effort, and presentation of the submission  

- **Authenticity (0–10)**  
  Likelihood that the submission is genuine (not AI/fake)

---

###  Scoring System

Each claim receives a total score out of 30:

Total Score = Relevance + Quality + Authenticity

The claim with the highest total score is selected.

---

##  Decision Flow

For every bounty:

1. Fetch all submitted claims  
2. Evaluate each claim using the scoring system  
3. Rank claims based on total score  
4. Select the highest scoring claim  
5. Decide how the bounty will be resolved  

---

##  Autonomous Resolution Mode

The bot does not behave predictably — it uses a probabilistic system:

- **65% → Open bounty (goes to voting)**
- **35% → Voting required (direct winner selection)**

This ensures:
- fairness  
- unpredictability (more human-like behavior)  
- alignment with decentralized decision-making  

---

##  Voting System

When voting is triggered:

1. The selected claim is submitted for voting  
2. Voting happens on-chain  
3. The bot continuously checks:

- current vote state  
- voting deadline  

4. After deadline:

- automatically resolves the vote  
- sends transaction on-chain  
- announces the winner publicly  

---

##  Automatic Payout

After vote resolution:

- the smart contract distributes reward automatically  
- the bot confirms resolution  
- posts transaction hash for verification  

---

##  Transparency & Auditability

Every decision includes:

- score breakdown (relevance, quality, authenticity)  
- total score  
- explanation (why it won)  

All of this is posted on Farcaster.

This ensures:
- transparency  
- traceability  
- public auditability  

---

##  Farcaster Interaction (Replies)

The bot can respond to user questions such as:

- “Why did you pick this?”
- “Can you share the bounty link?”
- “How was this evaluated?”

If a question is unclear or unsupported, the bot responds gracefully:

> “I’m not designed to answer that type of question yet.”

---

##  Architecture Overview

Main components:

- `process_bounty()`  
  → evaluates claims and selects winner  

- `process_vote_resolutions()`  
  → detects ended votes and resolves them  

- `process_farcaster_replies()`  
  → handles user replies and questions  

---

##  State Management

The bot uses a persistent `state.json` file to track:

- tracked bounties  
- seen claims  
- vote submissions  
- resolved votes  
- posted decisions  
- replied messages  

This ensures continuity across runs.

---

##  Autonomous Behavior

Once deployed, the bot runs independently:

- detects new bounties  
- evaluates submissions  
- selects winners  
- submits votes  
- resolves votes  
- posts results  
- replies to users  

No manual intervention required.

---

##  Supported Chains

- Degen  
- Base  
- Arbitrum  

The bot can operate across chains based on configuration.

---

##  Setup

### 1. Install dependencies

pip install -r requirements.txt

---

### 2. Configure environment

Create a `.env` file:

PRIVATE_KEY=
OPENAI_API_KEY=
NEYNAR_API_KEY=
NEYNAR_SIGNER_UUID=

ACTIVE_CHAINS=degen,base,arbitrum

RPC_URL_DEGEN=
RPC_URL_BASE=
RPC_URL_ARBITRUM=

OPENAI_MODEL=gpt-5.4-mini
CHECK_INTERVAL=60
EVAL_DELAY_SECONDS=60
MAX_EVAL_PER_RUN=3
OPEN_BOUNTY_PROBABILITY=0.65

---

##  Run the Bot

python bot.py

---

## Assumptions

- RPC endpoints are working  
- Farcaster API is available  
- claims contain valid metadata  

---

##  Limitations

- API rate limits may slow responses  
- network issues may delay execution  
- missing data may affect scoring accuracy  

---

##  Proof of Execution

The bot has successfully:

- evaluated claims  
- selected winners  
- submitted votes  
- resolved votes automatically  
- posted decisions publicly  

Farcaster Profile:
https://farcaster.xyz/arbiterbot.eth

---

##  Conclusion

Arbiter Bot is a fully autonomous agent designed to manage POIDH bounties from start to finish without manual intervention.

It combines:
- structured evaluation logic  
- probabilistic decision-making (65% open / 35% solo)  
- on-chain vote resolution  
- transparent public reasoning  

By doing this, the bot ensures:
- fairness in winner selection  
- accountability through public audit trails  
- consistency in execution  
- reduced human bias  

Arbiter Bot demonstrates how autonomous agents can reliably operate in decentralized environments while remaining transparent, explainable, and efficient.
Arbiter Bot is a fully autonomous, transparent, and intelligent system for managing POIDH bounties.

It ensures fair decision-making, public accountability, and zero manual bias.
