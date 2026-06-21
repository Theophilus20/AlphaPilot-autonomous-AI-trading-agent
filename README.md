# AlphaPilot — Autonomous AI Trading Agent

**BNB Hack: AI Trading Agent Edition · Track 1 (Autonomous Trading Agents)**

AlphaPilot reads live market signals from **CoinMarketCap**, decides with an
**LLM**, enforces hard **risk guardrails**, and signs + executes self-custody
swaps on **BSC** through the **Trust Wallet Agent Kit (TWAK)** — fully
autonomous, keys never leave the machine.

```
CMC signals  →  LLM decision  →  risk guardrails  →  TWAK sign + swap on BSC
 (real data)     (OpenRouter)     (your limits)        (self-custody, local key)
      ↑                                                        ↓
      └──────────────── loop, every N minutes  ←────── on-chain tx hash
```

---

## Three-layer sponsor stack (all live)

- **L1 · CoinMarketCap** — live quotes, 24h momentum, and the **Fear & Greed
  index** drive every decision. Optional **x402** pay-per-request path included.
- **L2 · Trust Wallet Agent Kit** — the *sole* execution layer. TWAK signs every
  swap locally; the agent process never holds a private key. Also used for
  **ERC-8004 agent identity** registration.
- **BNB Chain** — trades execute on BSC mainnet; the agent holds an on-chain
  **ERC-8004 identity** (a BNB-ecosystem primitive).

---

## On-chain proof (BSC mainnet)

All verifiable on https://bscscan.com for the agent wallet.

- **Agent wallet:** `0xA385Dd8D50dd8FD694a94Ddc1618d688Fef19610`
- **Competition registration:** registered via `twak compete register` (status: registered)
- **ERC-8004 agent identity:** agentId `138228` —
  tx `0x6eb2d8cd9d00581e95d65631eb948302c81572706d23482c761d5417552eaf52`
- **Autonomous trades (signed via TWAK, executed on BSC):**
  - BUY ADA — `0xef1bbebe1c6c63ff2b12946911e59d46760945a8fc4bb63a7bdace0b35fa40f7`
  - SELL BNB (dollar-sized) — `0x1d47424ef1b76a027ddff0915b1d41e431f0c1c45b85f39df31f714d2628ed85`
  - BUY BNB — `0x5bb27df035795ee48238cb195c2eed941ae1d840f82e2717b10c9a9007363dfa`
  - BUY BNB (multi-hop via TWAK aggregator, routed through **PancakeSwap Infinity**) — `0xaf06c7bbfcb4c7103e9e798620feb868b8d764d46e9cd19600413e0b335cf1ae`

Every swap is signed locally by Trust Wallet (self-custody); the agent process
never holds a private key. TWAK's aggregator routes across BSC liquidity —
verified routing through **PancakeSwap** on-chain (see the tx above on BscScan).

---

## Strategy — contrarian mean-reversion with risk gates

The agent capitalizes on sentiment extremes while maintaining disciplined risk management and cost-aware execution.

- **Fear (F&G ≤ 40):** market oversold → **BUY** the best setup. The agent scans
  ~56 eligible tokens, ranks by how oversold each is (with a quality-major
  bias), skips what it already holds, and buys the strongest opportunity.
- **Greed (F&G ≥ 60):** market overbought → **SELL** held positions to take profit.
- **Transition (40–60):** mostly HOLD; act only on a clear single-token signal.
- **Minimum daily activity:** to stay actively engaged with the market, if a full
  day passes with no signal-driven trade, the agent takes one small measured
  position in the best oversold major — a real trade, within all risk caps.

## Risk guardrails (enforced in code before every trade)

- Eligible-token allowlist (trades outside it are rejected)
- Per-trade cap, total budget cap, max concurrent positions
- Daily-loss circuit breaker
- **Max-drawdown gate** with per-cycle mark-to-market (halts trading on breach)
- Slippage ceiling; scam-token protection (exact-symbol + price filter on resolve)
- Dollar-aware sizing: the agent converts the USD cap into the correct token
  amount using the live price, so a trade can never oversize
- SELL only what the agent actually holds
- Auto-sizing: if the model proposes a trade above the per-trade cap, the agent
  sizes it down to the limit rather than rejecting — always within your caps

Every LLM decision is independently vetoed by `rules_engine.check()` — the model
is never trusted to self-police; code is the backstop.

---

## Files

| File | Role |
| --- | --- |
| `agent.py` | Orchestrator loop (read → decide → guard → execute → publish) |
| `cmc_reader.py` | L1 — CMC signals (API key or x402; sim fallback) |
| `decision_engine.py` | LLM decision via OpenRouter (heuristic fallback) |
| `rules_engine.py` | Guardrails + eligible-token list |
| `twak_executor.py` | L2 — TWAK CLI wrapper (sole signer) + token resolver |
| `x402_client.py` | x402 pay-per-request buyer (CMC, USDC-on-Base, EIP-3009) |
| `x402_server.py` | Local x402 seller for end-to-end demo |
| `api_server.py` | Dashboard backend (serves live agent state) |

---

## Setup (reproducible)

**Prerequisites:** Python 3.10+, Node.js, and the TWAK CLI
(`npm install -g @trustwallet/cli`). A funded BSC wallet created via
`twak wallet create` and credentials set via `twak init`.

```bash
# 1. configure
cp .env.example .env          # then fill in the keys below

# 2. run one cycle (watch it)
python agent.py

# 3. run continuously
python agent.py --loop --interval 1800
```

### `.env`

```dotenv
CMC_API_KEY=...                 # coinmarketcap.com/api
OPENROUTER_API_KEY=...          # openrouter.ai/keys
OPENROUTER_MODEL=openai/gpt-4o-mini
TWAK_WALLET_PASSWORD=...        # your TWAK wallet password
NETWORK=mainnet
I_UNDERSTAND_MAINNET=yes
BUDGET=20
MAX_PER_TRADE=1
MAX_POSITIONS=3
MAX_DAILY_LOSS=6
MAX_DRAWDOWN_PCT=20
MAX_SLIPPAGE_PCT=2
MIN_CONFIDENCE=0.55
REBALANCE_INTERVAL_HOURS=12
```

The banner prints what's live, e.g.:
`network=mainnet · twak=live · CMC=apikey · LLM=live`

---

## Safety & honesty notes

- **Self-custody:** keys are generated and stored locally by Trust Wallet Core
  (AES-256-GCM); the agent shells out to the `twak` CLI and never sees a key.
- **Mainnet lock:** the agent refuses mainnet unless both `NETWORK=mainnet` and
  `I_UNDERSTAND_MAINNET=yes` are set.
- **No fakery:** if CMC/LLM/TWAK credentials are absent, the agent runs in
  clearly-labelled simulation/heuristic/dry-run mode — never presented as real.
- **Spot only:** perps are intentionally out of scope (leverage/liquidation risk;
  TWAK does not expose a perps command).
- **x402 status:** the x402 buyer is fully implemented (real 402 handshake,
  USDC-on-Base, EIP-3009) and demoable via the included local server. It is
  off by default; the agent uses the standard CMC API key path unless x402 is
  explicitly enabled. Nothing is simulated as a real payment.


