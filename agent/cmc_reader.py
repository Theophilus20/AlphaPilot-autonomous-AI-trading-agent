"""
CMC signal reader — L1 data layer, with THREE real modes:

  1. x402  (agent-native, pay-per-request, no API key) — CMC Agent Hub x402.
            USDC-on-Base micro-payments via the x402 handshake.
  2. apikey (standard CMC Pro REST with X-CMC_PRO_API_KEY).
  3. sim    (labelled simulation when neither is configured).

Mode is chosen automatically: if CMC_X402=on and an x402 client is wired -> x402;
elif CMC_API_KEY set -> apikey; else -> sim. Never presents sim as real.

x402 endpoints (from CMC docs):
  GET /x402/v3/cryptocurrency/quotes/latest?symbol=...
  Payment: USDC on Base (eip155:8453), $0.01/request, EIP-3009 signed header.
"""

import json
import os
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


CMC_BASE = "https://pro-api.coinmarketcap.com"


@dataclass
class TokenSignal:
    symbol: str
    price: float
    pct_change_24h: float
    volume_24h: float
    technical_score: float
    sentiment_score: float
    recommendation: str
    address: Optional[str] = None   # BSC contract address if known
    sim: bool = False


@dataclass
class MarketSnapshot:
    timestamp: str
    fear_greed: int
    signals: dict = field(default_factory=dict)
    sim: bool = False
    mode: str = "sim"               # 'x402' | 'apikey' | 'sim'


class CMCReader:
    def __init__(self, api_key: Optional[str] = None, x402_client=None):
        self.api_key = api_key or os.environ.get("CMC_API_KEY")
        self.x402 = x402_client
        self.use_x402 = (os.environ.get("CMC_X402", "").lower() in ("1", "on", "true")
                         and x402_client is not None)
        # x402 quotes endpoint (per CMC docs).
        self.x402_quotes = (os.environ.get("CMC_X402_QUOTES_URL")
                            or CMC_BASE + "/x402/v3/cryptocurrency/quotes/latest")

    # ---- standard Pro API (key) ----------------------------------------

    def _get(self, path: str, params: dict) -> Optional[dict]:
        if not self.api_key:
            return None
        qs = urllib.parse.urlencode(params)
        url = f"{CMC_BASE}{path}?{qs}"
        req = urllib.request.Request(url, headers={
            "X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
            return None

    def _fetch_quotes_apikey(self, symbols):
        data = self._get("/v1/cryptocurrency/quotes/latest",
                         {"symbol": ",".join(symbols), "convert": "USD"})
        if not data or "data" not in data:
            return None
        return data["data"]

    def _fetch_fear_greed(self):
        data = self._get("/v3/fear-and-greed/latest", {})
        try:
            return int(data["data"]["value"])
        except (TypeError, KeyError, ValueError):
            return None

    # ---- x402 (pay-per-request, no key) --------------------------------

    def _fetch_quotes_x402(self, symbols):
        """Fetch quotes via x402 pay-per-request. Returns CMC 'data' dict or None."""
        url = self.x402_quotes + "?" + urllib.parse.urlencode(
            {"symbol": ",".join(symbols), "convert": "USD"})
        resp = self.x402.get(url)   # x402 client does 402 -> pay -> retry
        if not resp.ok or not resp.data:
            return None
        body = resp.data
        return body.get("data") if isinstance(body, dict) else None

    # ---- public --------------------------------------------------------

    def snapshot(self, symbols) -> MarketSnapshot:
        ts = datetime.now(timezone.utc).isoformat()

        quotes, mode = None, "sim"
        if self.use_x402:
            quotes = self._fetch_quotes_x402(symbols)
            if quotes is not None:
                mode = "x402"
        if quotes is None and self.api_key:
            quotes = self._fetch_quotes_apikey(symbols)
            if quotes is not None:
                mode = "apikey"

        fg = self._fetch_fear_greed() if self.api_key else None

        if quotes is None:
            import random
            sigs = {}
            for s in symbols:
                chg = round(random.uniform(-8, 10), 2)
                tech = round(max(0, min(100, 50 + chg * 4)), 1)
                senti = round(random.uniform(35, 85), 1)
                sigs[s] = TokenSignal(s, round(random.uniform(0.5, 600), 4), chg,
                                      random.uniform(1e6, 5e7), tech, senti,
                                      _heuristic(tech, senti), sim=True)
            return MarketSnapshot(ts, fg if fg is not None else 60, sigs, sim=True, mode="sim")

        fear = fg if fg is not None else 50
        sigs = {}
        for s in symbols:
            q = quotes.get(s)
            if not q:
                continue
            usd = q["quote"]["USD"]
            chg = float(usd.get("percent_change_24h") or 0.0)
            price = float(usd.get("price") or 0.0)
            vol = float(usd.get("volume_24h") or 0.0)
            tech = max(0.0, min(100.0, 50 + chg * 4))
            # BSC contract address if CMC lists it under platform.
            addr = None
            plat = q.get("platform")
            if isinstance(plat, dict) and str(plat.get("name", "")).lower() in (
                    "bnb smart chain", "binance smart chain", "bnb", "bsc"):
                addr = plat.get("token_address")
            sigs[s] = TokenSignal(s, price, chg, vol, round(tech, 1), round(float(fear), 1),
                                  _heuristic(tech, fear), address=addr, sim=False)
        return MarketSnapshot(ts, fear, sigs, sim=False, mode=mode)


def _heuristic(tech: float, senti: float) -> str:
    score = tech * 0.6 + senti * 0.4
    if score >= 65:
        return "BUY"
    if score >= 50:
        return "HOLD"
    return "SELL"