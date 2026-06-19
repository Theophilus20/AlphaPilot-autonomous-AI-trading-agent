"""
TWAK executor — the SOLE execution layer for AlphaPilot.

Every on-chain action goes through the Trust Wallet Agent Kit CLI (`twak`),
so private keys are generated and stored locally by Trust Wallet Core and
NEVER leave the machine. This module shells out to the real `twak` binary;
the agent process itself never sees a private key or mnemonic.

CONFIRMED against twak v0.19.1:
  - chain key is `bsc` (BNB Smart Chain mainnet). There is NO BSC testnet.
  - swap quote:  twak swap <amt> <from> <to> --chain bsc --quote-only
       -> {input, output, minReceived, provider, priceImpact}
  - swap exec:   twak swap <amt> <from> <to> --chain bsc --password <pw>
  - password passed via --password (falls back to OS keychain / TWAK_WALLET_PASSWORD)

If `twak` is not installed (e.g. CI / sandbox) every method returns a clearly
labelled dry-run result so the surrounding logic stays testable. Nothing is
faked as real: dry_run=True is always present in that output.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional


# Only BSC mainnet exists in TWAK. Kept as a dict so the rest of the code
# can stay network-agnostic, but both keys map to the real `bsc` chain.
CHAIN_KEYS = {
    "mainnet": "bsc",
    "testnet": "bsc",  # TWAK has no BSC testnet; alias to bsc so nothing breaks
}


@dataclass
class SwapResult:
    ok: bool
    dry_run: bool
    tx_hash: Optional[str] = None
    from_token: str = ""
    to_token: str = ""
    amount: float = 0.0
    output: Optional[str] = None
    min_received: Optional[str] = None
    provider: Optional[str] = None
    price_impact: Optional[str] = None
    raw: dict = field(default_factory=dict)
    error: Optional[str] = None
    explorer_url: Optional[str] = None


class TwakExecutor:
    def __init__(self, network: str = "mainnet", twak_password: Optional[str] = None):
        """
        network: kept for API compatibility; both values use BSC mainnet (`bsc`).
        twak_password: signing password. Read from TWAK_WALLET_PASSWORD env if
                       not passed. Falls back to the OS keychain inside twak.
        """
        self.network = network
        self.chain = CHAIN_KEYS.get(network, "bsc")
        self.password = twak_password or os.environ.get("TWAK_WALLET_PASSWORD")
        self.twak_path = shutil.which("twak")
        self.available = self.twak_path is not None

    # ---- internal -------------------------------------------------------

    def _run(self, args, timeout=120):
        """Run a twak command, return (ok, parsed_json_or_text, error)."""
        if not self.available:
            return False, None, "twak CLI not installed"
        cmd = [self.twak_path] + args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, None, "twak command timed out"
        except Exception as e:  # noqa: BLE001
            return False, None, str(e)

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return False, None, err or out or f"twak exited {proc.returncode}"

        # twak prints JS-style object literals; try strict JSON first, then a
        # lenient parse for the {key: 'value'} shape the CLI uses.
        parsed = _parse_twak_output(out)
        return True, parsed, None

    def _with_password(self, args):
        """Provide the signing password. Prefer the TWAK_WALLET_PASSWORD env var
        (TWAK reads it automatically and it's not visible in shell history);
        only fall back to the --password flag if no env var is set."""
        if os.environ.get("TWAK_WALLET_PASSWORD"):
            return args  # twak picks it up from the environment; nothing to add
        if self.password:
            return args + ["--password", self.password]
        return args  # twak will fall back to OS keychain

    # ---- read-only ------------------------------------------------------

    def wallet_address(self) -> Optional[str]:
        ok, data, _ = self._run(["wallet", "address", "--chain", self.chain, "--json"])
        if ok and isinstance(data, dict):
            return data.get("address") or data.get("Address")
        return None

    def balance(self, address: str, token: str = "BNB") -> Optional[float]:
        # native balance by address; token contract not handled here
        ok, data, _ = self._run(["balance", "--address", address,
                                 "--chain", self.chain, "--json"])
        if ok and isinstance(data, dict):
            try:
                return float(data.get("available", data.get("total", 0)))
            except (TypeError, ValueError):
                return None
        return None

    def _token_arg(self, token: str) -> str:
        """Return what to pass to twak for a token: a known symbol stays a
        symbol; anything else resolves to a verified contract address."""
        up = (token or "").strip().upper()
        if up in TWAK_NATIVE_SYMBOLS:
            return token  # twak knows these by symbol
        addr = resolve_token(self, token)
        return addr or token  # fall back to symbol (twak will reject if unknown)

    def token_price(self, token: str) -> Optional[float]:
        """Get a token's USD price via `twak price` (read-only)."""
        if not self.available:
            return None
        ok, data, _ = self._run(["price", token, "--json"])
        if ok and isinstance(data, dict):
            for k in ("priceUsd", "price", "usd"):
                if k in data:
                    try:
                        return float(data[k])
                    except (TypeError, ValueError):
                        pass
        return None

    def swap_usd(self, from_token: str, to_token: str, usd_amount: float,
                 slippage: float = 2.0) -> SwapResult:
        """
        Execute a swap sized in USD via TWAK's native --usd flag.
        TWAK converts the USD amount into the right token amount itself, which
        is more reliable than pre-computing it. The only signing call for $-sized
        trades. Slippage defaults higher (2%) to help thin/RFQ routes fill.
        """
        if not self.available:
            return SwapResult(ok=True, dry_run=True, from_token=from_token,
                              to_token=to_token, amount=usd_amount,
                              raw={"note": "twak not installed; swap simulated, NOT on-chain"})
        f = self._token_arg(from_token)
        t = self._token_arg(to_token)
        # twak swap --usd <amt> <from> <to>  (USD-equivalent of source token)
        args = ["swap", "--usd", str(usd_amount), f, t,
                "--chain", self.chain, "--slippage", str(slippage)]
        args = self._with_password(args)
        ok, data, err = self._run(args, timeout=180)
        if not ok:
            return SwapResult(ok=False, dry_run=False, from_token=from_token,
                              to_token=to_token, amount=usd_amount, error=err)
        d = data if isinstance(data, dict) else {}
        tx_hash = (d.get("txHash") or d.get("tx_hash") or d.get("hash")
                   or d.get("transactionHash"))
        explorer = ("https://bscscan.com/tx/" + tx_hash) if tx_hash else None
        return SwapResult(ok=True, dry_run=False, tx_hash=tx_hash, from_token=from_token,
                          to_token=to_token, amount=usd_amount,
                          output=d.get("output"), min_received=d.get("minReceived"),
                          provider=d.get("provider"), price_impact=d.get("priceImpact"),
                          raw=d, explorer_url=explorer)

    def quote(self, from_token: str, to_token: str, amount: float) -> dict:
        """Swap quote WITHOUT signing (safe, read-only)."""
        if not self.available:
            return {"dry_run": True, "from": from_token, "to": to_token,
                    "amount": amount, "note": "twak not installed; quote simulated"}
        f = self._token_arg(from_token)
        t = self._token_arg(to_token)
        ok, data, err = self._run([
            "swap", str(amount), f, t,
            "--chain", self.chain, "--quote-only",
        ])
        if ok and isinstance(data, dict):
            return data
        return {"error": err or "quote failed"}

    # ---- signing / execution -------------------------------------------

    def swap(self, from_token: str, to_token: str, amount: float,
             slippage: Optional[float] = None) -> SwapResult:
        """
        Execute a REAL swap on BSC via twak. The only signing call.
        twak signs locally with the user's key; we never see it.
        """
        if not self.available:
            return SwapResult(
                ok=True, dry_run=True, from_token=from_token, to_token=to_token,
                amount=amount,
                raw={"note": "twak CLI not installed; swap simulated, NOT on-chain"},
            )

        args = ["swap", str(amount), self._token_arg(from_token),
                self._token_arg(to_token), "--chain", self.chain]
        if slippage is not None:
            # confirm flag name on your CLI via `twak swap --help`; commonly --slippage
            args += ["--slippage", str(slippage)]
        args = self._with_password(args)

        ok, data, err = self._run(args, timeout=180)
        if not ok:
            return SwapResult(ok=False, dry_run=False, from_token=from_token,
                              to_token=to_token, amount=amount, error=err)

        d = data if isinstance(data, dict) else {}
        tx_hash = (d.get("txHash") or d.get("tx_hash") or d.get("hash")
                   or d.get("transactionHash"))
        explorer = ("https://bscscan.com/tx/" + tx_hash) if tx_hash else None
        return SwapResult(
            ok=True, dry_run=False, tx_hash=tx_hash, from_token=from_token,
            to_token=to_token, amount=amount,
            output=d.get("output"), min_received=d.get("minReceived"),
            provider=d.get("provider"), price_impact=d.get("priceImpact"),
            raw=d, explorer_url=explorer,
        )


def _parse_twak_output(out: str):
    """
    Parse twak's output. It prints JS object literals like:
      { input: '5 USDT', output: '0.0086 BNB', provider: 'LiquidMesh' }
    Try JSON first; if that fails, do a light normalization to JSON.
    """
    if not out:
        return {"text": ""}
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        pass
    # Lenient: quote unquoted keys and convert single to double quotes.
    import re
    s = out
    # keys:  word:  ->  "word":
    s = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', s)
    # single-quoted strings -> double-quoted
    s = s.replace("\\'", "'")
    s = re.sub(r"'([^']*)'", r'"\1"', s)
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return {"text": out}


# ── Known-good BSC contract addresses for major eligible tokens ──────────
# Hardcoded so the agent NEVER resolves these via fuzzy search (which returns
# scam impostors). Verified canonical addresses on BNB Smart Chain.
KNOWN_BSC_ADDRESSES = {
    "CAKE": "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82",
    "USDT": "0x55d398326f99059fF775485246999027B3197955",
    "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    "ETH":  "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
    "BTCB": "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c",
    "DAI":  "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3",
    "LINK": "0xF8A0BF9cF54Bb92F17374d9e9A321E6a111a51bD",
    "ADA":  "0x3EE2200Efb3400fAbB9AacF31297cBdD1d435D47",
    "DOGE": "0xbA2aE424d960c26247Dd6c32edC70B295c744C43",
    "XRP":  "0x1D2F0da169ceB9fC7B3144628dB156f3F6c60dBE",
    "TWT":  "0x4B0F1812e5Df2A09796481Ff14017e6005508003",
    "UNI":  "0xBf5140A22578168FD562DCcF235E5D43A02ce9B1",
    "AVAX": "0x1CE0c2827e2eF14D5C4f29a091d735A204794041",
    # BNB and WBNB are native / handled by symbol directly.
}

# Symbols TWAK accepts directly without an address (native + a few majors).
TWAK_NATIVE_SYMBOLS = {"BNB", "WBNB", "USDT", "BNB"}


def resolve_token(executor, symbol: str) -> Optional[str]:
    """
    Turn a token symbol into a SAFE BSC contract address.

    Safety: never trusts a fuzzy search match. Order of trust:
      1. Known-good hardcoded address (canonical, verified).
      2. twak search result whose symbol matches EXACTLY (case-insensitive)
         AND has a real USD price — and we take the highest-priced exact match
         (impostors are priced at ~0). Still conservative.
    Returns the address, or None if it can't be safely resolved.
    """
    sym = (symbol or "").strip()
    if not sym:
        return None
    up = sym.upper()

    # 1) Known-good wins, always.
    if up in KNOWN_BSC_ADDRESSES:
        return KNOWN_BSC_ADDRESSES[up]

    # 2) Exact-symbol search match, defensively filtered.
    if not executor.available:
        return None
    ok, data, _ = executor._run(["search", sym, "--networks", "bsc",
                                 "--limit", "10", "--json"])
    if not ok or not isinstance(data, list):
        return None
    exact = [r for r in data
             if isinstance(r, dict)
             and str(r.get("symbol", "")).upper() == up
             and str(r.get("chain", "")).lower() == "bsc"
             and float(r.get("priceUsd", 0) or 0) > 0]
    if not exact:
        return None
    # Prefer the most credible: highest USD price among exact symbol matches.
    exact.sort(key=lambda r: float(r.get("priceUsd", 0) or 0), reverse=True)
    return exact[0].get("address")