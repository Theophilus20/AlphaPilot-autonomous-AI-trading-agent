"""
LLM decision engine — the "AI" in AI trading agent (OpenAI / ChatGPT).

ChatGPT reads the CMC market snapshot plus the rules/portfolio and returns a
STRUCTURED decision (JSON). The model decides direction, token, size, confidence;
the rules engine then INDEPENDENTLY vetoes anything unsafe. The model is never
trusted to self-police — code is the backstop.

No API key / no network -> transparent heuristic fallback (llm=False).
Never presented as a real model call when it isn't.
"""

import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional

from rules_engine import Decision, Rules, PortfolioState


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# OpenRouter model slugs are namespaced. Override via OPENROUTER_MODEL.
# Good cheap default; for a free option try "meta-llama/llama-3.1-8b-instruct:free".
MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")


SYSTEM = """You are the decision engine of an autonomous crypto trading agent on BSC.
Your goal: maximize total return over a one-week window WITHOUT breaching a max
drawdown cap. You are scored on profit, penalized for drawdown and for overtrading
(each trade has a cost). Trade with conviction, not noise.

STRATEGY — contrarian mean-reversion on market sentiment:
- Fear & Greed is the primary regime signal (0=extreme fear, 100=extreme greed).
- FEAR (F&G <= 40): market is fearful, tends to be oversold/cheap. BUY quality
  tokens (majors like BNB, ETH, CAKE). Lower F&G = stronger conviction, deploy more.
- GREED (F&G >= 60): market is greedy, tends to be overbought. SELL positions you
  HOLD to take profit. Higher F&G = trim more. Do NOT sell what you don't own.
- TRANSITION (40 < F&G < 60): narrow neutral band. Act only on a token with a
  clear specific signal (strong 24h move + supportive technicals); else HOLD.
- Confirm direction with the token's own technicals/momentum; sentiment leads.

You should typically find at least one good trade per day. Trade with conviction
when you have an edge; don't force trades with no rationale.

You are given `top_candidates`: a pre-researched shortlist of the strongest
oversold setups (already ranked by oversold depth, recent dip, and liquidity),
plus `all_signals` for full context. Evaluate the shortlist and pick the BEST
opportunity — consider momentum, how oversold it is, and liquidity. You may pick
from all_signals if you see a clearly better setup, but the shortlist is your
fast path to a smart choice.

BE DECISIVE, NOT PASSIVE:
- In FEAR (F&G <= 40), defaulting to HOLD wastes the core edge of this strategy.
  Extreme fear IS the opportunity for mean-reversion — usually pick the most
  oversold quality token and BUY. "No clear opportunity" is rarely right in fear.
- Vary token choice by the signals — consider ETH, CAKE, ADA, LINK, BNB and
  others, not only BNB. Pick the best setup (most oversold + supportive technicals).

DISCIPLINE RULES (hard):
- Only trade tokens on the eligible list.
- Only SELL a token that appears in `current_holdings`. Never sell what you don't hold.
- Respect max_per_trade and available_budget when sizing (amounts are in USDT/$).
- Prefer HOLD when the edge is unclear. Overtrading loses to fees.
- confidence in [0,1] = your genuine conviction. Below the user's min_confidence,
  the trade is rejected, so only commit a number you mean.

Output ONLY this JSON (no prose, no markdown):
{"action":"BUY|SELL|HOLD","symbol":"<TICKER>","amount":<usdt_number>,
 "confidence":<0..1>,"slippage_pct":<number>,"reason":"<one sentence>"}"""


@dataclass
class DecisionResult:
    decision: Decision
    llm: bool
    raw: str = ""


class Decider:
    def __init__(self, api_key: Optional[str] = None):
        # OpenRouter key (preferred). Falls back to OPENAI_API_KEY for convenience.
        self.api_key = (api_key or os.environ.get("OPENROUTER_API_KEY")
                        or os.environ.get("OPENAI_API_KEY"))

    def decide(self, snapshot, rules: Rules, state: PortfolioState,
               eligible: list, holdings: Optional[dict] = None,
               recent_buys: Optional[list] = None) -> DecisionResult:
        available = max(rules.budget - state.deployed, 0.0)
        holdings = holdings or {}
        recent_buys = recent_buys or []
        sigs = []
        for sym, s in snapshot.signals.items():
            sigs.append({"symbol": s.symbol, "price": s.price, "chg24h": s.pct_change_24h,
                         "technical": s.technical_score, "sentiment": s.sentiment_score,
                         "volume24h": getattr(s, "volume_24h", 0), "hint": s.recommendation})
        # Pre-rank a shortlist so the model "researches" the strongest setups fast
        # instead of scanning 30 raw rows. Ranking mirrors the strategy: most
        # oversold + recent dip + liquidity, excluding held tokens, and lightly
        # penalizing tokens bought very recently so positions rotate (diversify).
        def _cand_score(x):
            oversold = 100 - x["technical"]
            dip = min(max(-x["chg24h"], 0), 25)
            liq = 8 if (x.get("volume24h") or 0) > 5_000_000 else 0
            recency_penalty = 15 if x["symbol"] in recent_buys else 0
            return oversold + dip * 1.5 + liq - recency_penalty
        shortlist = sorted(
            [x for x in sigs if x["symbol"] != "USDT" and x["symbol"] not in holdings],
            key=_cand_score, reverse=True)[:6]
        payload = {
            "fear_greed": snapshot.fear_greed,
            "top_candidates": shortlist,        # pre-researched best setups
            "all_signals": sigs,                # full universe for context
            "recently_bought": recent_buys,     # prefer to diversify away from these
            "rules": {"max_per_trade": rules.max_per_trade, "available_budget": available,
                      "max_slippage_pct": rules.max_slippage_pct,
                      "min_confidence": rules.min_confidence},
            "portfolio": {"deployed": state.deployed, "open_positions": state.open_positions,
                          "max_positions": rules.max_positions},
            "current_holdings": holdings,
        }

        if self.api_key:
            parsed, raw = self._call_openai(payload)
            if parsed is not None:
                return DecisionResult(self._to_decision(parsed), llm=True, raw=raw)

        return DecisionResult(self._heuristic(snapshot, rules, state, holdings), llm=False)

    # ---- OpenAI / ChatGPT call -----------------------------------------

    def _call_openai(self, payload):
        body = json.dumps({
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(payload)},
            ],
            "temperature": 0.2,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},  # force JSON (supported models)
        }).encode()
        req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            # OpenRouter likes these for attribution (optional but recommended).
            "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "https://alphapilot.local"),
            "X-Title": "AlphaPilot Agent",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            text = data["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.strip("`")
                text = text[text.find("{"):]
            return json.loads(text), text
        except (urllib.error.URLError, urllib.error.HTTPError,
                ValueError, KeyError, TimeoutError, IndexError):
            return None, ""

    def _to_decision(self, p) -> Decision:
        return Decision(
            action=str(p.get("action", "HOLD")).upper(),
            symbol=str(p.get("symbol", "")).strip(),
            amount=float(p.get("amount", 0) or 0),
            confidence=float(p.get("confidence", 0) or 0),
            slippage_pct=float(p.get("slippage_pct", 1.0) or 1.0),
            reason=str(p.get("reason", ""))[:200],
        )

    # ---- heuristic fallback --------------------------------------------

    def _heuristic(self, snapshot, rules: Rules, state: PortfolioState, holdings=None) -> Decision:
        """Deterministic mean-reversion backstop (used when no LLM key).
        Buy quality in fear, sell holdings in greed, else hold."""
        holdings = holdings or {}
        fg = snapshot.fear_greed
        available = max(rules.budget - state.deployed, 0.0)
        majors = ("BNB", "ETH", "CAKE", "BTCB", "LINK", "ADA")

        # GREED (>=60) -> take profit on something we actually hold.
        if fg >= 60 and holdings:
            sym = max(holdings, key=lambda k: holdings[k])  # largest position
            amount = min(rules.max_per_trade, float(holdings[sym]))
            if amount > 0:
                return Decision("SELL", sym, round(amount, 2), 0.80,
                                slippage_pct=min(1.0, rules.max_slippage_pct),
                                reason=f"Greed (F&G={fg}): taking profit on {sym}.")

        # FEAR (<=40) -> research ALL eligible tokens and buy the best opportunity.
        # Multi-factor score: oversold depth + recent dip (mean-reversion setup) +
        # liquidity/quality bias. Skips held tokens to diversify over time.
        if fg <= 40 and available > 0 and state.open_positions < rules.max_positions:
            scored = []
            for sym, s in snapshot.signals.items():
                if s.symbol == "USDT" or s.symbol in holdings:
                    continue
                oversold = 100 - s.technical_score          # cheaper = higher
                # a recent negative move strengthens a mean-reversion bounce setup
                dip = min(max(-s.pct_change_24h, 0), 25)    # 0..25 from a 24h drop
                volume = getattr(s, "volume_24h", 0) or 0
                liquidity = 8 if volume > 5_000_000 else 0  # tradeable depth
                quality = 8 if s.symbol in majors else 0
                score = oversold + dip * 1.5 + liquidity + quality
                scored.append((s, round(score, 1)))
            if scored:
                scored.sort(key=lambda x: x[1], reverse=True)
                s, sc = scored[0]
                runners = ", ".join(f"{c.symbol}({v:.0f})" for c, v in scored[1:3])
                amount = min(rules.max_per_trade, available)
                return Decision("BUY", s.symbol, round(amount, 2), 0.78,
                                slippage_pct=min(1.0, rules.max_slippage_pct),
                                reason=f"Fear (F&G={fg}): best setup {s.symbol} (score {sc}); "
                                       f"next: {runners}.")

        # TRANSITION ZONE (40-60) -> hold (avoid overtrading / fees).
        return Decision("HOLD", "", 0.0, 0.50,
                        reason=f"Transition zone (F&G={fg}): holding, edge unclear.")

        # TRANSITION ZONE (40-60) -> hold (avoid overtrading / fees).
        return Decision("HOLD", "", 0.0, 0.50,
                        reason=f"Transition zone (F&G={fg}): holding, edge unclear.")