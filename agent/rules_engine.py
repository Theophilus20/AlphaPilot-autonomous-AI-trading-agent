"""
Rules engine — the guardrails the agent must obey before any trade.

Hackathon-scored (Autonomous execution and guardrails, 20 pts):
token allowlist, per-trade cap, daily limit, drawdown cap, slippage protection.

Every decision passes through `check()` BEFORE it can reach the executor.
A decision that fails any rule is rejected and never signed.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# The currency the agent holds and prices trades in. Buying it against
# itself is meaningless, so it's excluded from BUY candidates.
QUOTE_CCY = "USDT"


# Eligible BEP-20 tokens (BNB Hack). Trades outside this set do not count.
ELIGIBLE_TOKENS = {
    "ETH", "USDT", "USDC", "XRP", "TRX", "DOGE", "ZEC", "ADA", "LINK", "BCH", "DAI", "TON", "USD1", "USDe", "M",
    "LTC", "AVAX", "SHIB", "XAUt", "WLFI", "H", "DOT", "UNI", "ASTER", "DEXE", "USDD", "ETC", "AAVE", "ATOM", "U",
    "STABLE", "FIL", "INJ", "NIGHT", "FET", "TUSD", "BONK", "PENGU", "CAKE", "SIREN", "LUNC", "ZRO",
    "KITE", "FDUSD", "BEAT", "PIEVERSE", "BTT", "NFT", "EDGE", "FLOKI", "LDO", "B", "FF", "PENDLE", "NEX", "STG",
    "AXS", "TWT", "HOME", "RAY", "COMP", "GWEI", "XCN", "GENIUS", "XPL", "BAT", "SKYAI", "APE", "IP", "SFP", "TAG",
    "NXPC", "AB", "SAHARA", "1INCH", "CHEEMS", "BANANAS31", "RIVER", "MYX", "RAVE", "SNX", "FORM", "LAB", "HTX",
    "USDf", "CTM", "BDX", "SLX", "UB", "DUCKY", "FRAX", "BILL", "WFI", "KOGE", "ALE", "FRXUSD", "USDF", "GOMINING",
    "VCNT", "GUA", "DUSD", "SMILEK", "0G", "BEAM", "MY", "SOON", "REAL", "Q", "AIOZ", "ZIG", "YFI", "TAC", "lisUSD",
    "CYS", "ZAMA", "TRIA", "HUMA", "PLUME", "ZIL", "XPR", "ZETA", "BabyDoge", "NILA", "ROSE", "VELO", "UAI", "BRETT",
    "OPEN", "BSB", "TOSHI", "BAS", "ACH", "AXL", "LUR", "ELF", "KAVA", "APR", "IRYS", "EURI", "XUSD", "BARD", "DUSK",
    "SUSHI", "PEAQ", "COAI", "BDCA", "XAUM", "BNB",
}


@dataclass
class Rules:
    budget: float = 1000.0           # total capital the agent may deploy (quote ccy)
    max_per_trade: float = 200.0     # cap on a single trade
    max_positions: int = 3           # concurrent open positions
    max_daily_loss: float = 100.0    # stop trading for the day past this realized loss
    max_drawdown_pct: float = 5.0    # stop if portfolio draws down this % from peak
    max_slippage_pct: float = 1.0    # reject trades needing more slippage than this
    min_confidence: float = 0.70     # LLM/score confidence floor to act


@dataclass
class PortfolioState:
    deployed: float = 0.0
    open_positions: int = 0
    realized_pnl_today: float = 0.0
    peak_value: float = 0.0
    current_value: float = 0.0
    holdings: dict = field(default_factory=dict)  # {symbol: usd value held}
    day: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    def roll_day_if_needed(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.realized_pnl_today = 0.0


@dataclass
class Decision:
    action: str               # "BUY", "SELL", or "HOLD"
    symbol: str               # token symbol
    amount: float             # size in quote currency
    confidence: float         # 0..1
    slippage_pct: float = 1.0
    reason: str = ""


@dataclass
class RuleCheck:
    approved: bool
    reason: str


def check(decision: Decision, rules: Rules, state: PortfolioState) -> RuleCheck:
    """Return RuleCheck(approved, reason). Order matters: cheapest checks first."""
    state.roll_day_if_needed()

    if decision.action == "HOLD":
        return RuleCheck(False, "Decision is HOLD; no trade.")

    if decision.action not in ("BUY", "SELL"):
        return RuleCheck(False, f"Unknown action '{decision.action}'.")

    sym = (decision.symbol or "").strip()
    if sym not in ELIGIBLE_TOKENS:
        return RuleCheck(False, f"{sym or 'token'} is not on the eligible BEP-20 list.")

    if decision.action == "BUY" and sym == QUOTE_CCY:
        return RuleCheck(False, f"Cannot buy the quote currency {QUOTE_CCY} against itself.")

    # SELL must target a token we actually hold (prevents selling dust/native
    # balance we need for gas, and selling positions we don't own).
    if decision.action == "SELL":
        held = getattr(state, "holdings", None) or {}
        # BNB is the gas token and the agent's native balance — only sell it if
        # we explicitly bought it as a position (it's in holdings).
        if decision.symbol not in held:
            return RuleCheck(False,
                f"Won't SELL {decision.symbol}: not in current holdings "
                f"(only sell positions the agent opened).")

    if decision.confidence < rules.min_confidence:
        return RuleCheck(False,
            f"Confidence {decision.confidence:.0%} below floor {rules.min_confidence:.0%}.")

    if decision.slippage_pct > rules.max_slippage_pct:
        return RuleCheck(False,
            f"Slippage {decision.slippage_pct:.2f}% exceeds max {rules.max_slippage_pct:.2f}%.")

    if decision.amount <= 0:
        return RuleCheck(False, "Trade amount must be positive.")

    if decision.amount > rules.max_per_trade:
        return RuleCheck(False,
            f"Amount ${decision.amount:.2f} exceeds per-trade cap ${rules.max_per_trade:.2f}.")

    # Daily loss circuit-breaker
    if state.realized_pnl_today <= -abs(rules.max_daily_loss):
        return RuleCheck(False,
            f"Daily loss limit hit (${state.realized_pnl_today:.2f}). Trading paused for the day.")

    # Drawdown circuit-breaker
    if state.peak_value > 0:
        dd = (state.peak_value - state.current_value) / state.peak_value * 100
        if dd >= rules.max_drawdown_pct:
            return RuleCheck(False,
                f"Drawdown {dd:.1f}% exceeds cap {rules.max_drawdown_pct:.1f}%. Trading halted.")

    # Buys consume budget and a position slot
    if decision.action == "BUY":
        available = max(rules.budget - state.deployed, 0.0)
        if decision.amount > available:
            return RuleCheck(False,
                f"Amount ${decision.amount:.2f} exceeds available budget ${available:.2f}.")
        if state.open_positions >= rules.max_positions:
            return RuleCheck(False,
                f"Max concurrent positions reached ({rules.max_positions}).")

    return RuleCheck(True, "Passes all guardrails.")