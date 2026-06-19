"""
x402 payment client — CMC Agent Hub spec (USDC on Base, EIP-3009).

Real handshake (per CMC docs):
  1. GET resource -> HTTP 402 with base64 'Payment-Required' (or JSON body)
     containing accepts[]: {scheme, network eip155:8453, asset USDC, amount, payTo}.
  2. Sign an EIP-3009 transferWithAuthorization (OFF-CHAIN signature) for USDC
     on Base. Attach as PAYMENT-SIGNATURE header. Pay-on-success; transfer is
     only settled by the facilitator when data is delivered.
  3. Retry with the header -> 200 + data.

HONEST BOUNDARY ON SIGNING:
  EIP-3009 signing needs the wallet's private key. The agent itself never holds
  the key — TWAK does. Two supported signing backends:
    A) x402 helper CLI/sidecar (X402_SIGN_CMD) that returns the signature.
    B) A local signer via env X402_PRIVATE_KEY ONLY for a low-value Base USDC
       "data wallet" (NOT your trading key). Clearly opt-in and separate.
  If neither is configured, we DO NOT fake a payment: we return paid=False and
  the caller falls back to API-key mode. Nothing is presented as paid when it isn't.
"""

import base64
import json
import os
import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PaymentRecord:
    paid: bool
    amount: Optional[str] = None
    asset: Optional[str] = None
    network: Optional[str] = None
    recipient: Optional[str] = None
    signature: Optional[str] = None
    note: str = ""


@dataclass
class X402Response:
    ok: bool
    status: int
    data: Optional[dict]
    payment: Optional[PaymentRecord] = None
    error: Optional[str] = None


class X402Client:
    def __init__(self, executor=None, network: str = "base", max_price_usdc: float = 0.05):
        self.executor = executor                 # TWAK (not used for Base EIP-3009 yet)
        self.network = network
        self.max_price_usdc = max_price_usdc      # refuse charges above this
        self.sign_cmd = os.environ.get("X402_SIGN_CMD")   # external signer command
        self.priv_key = os.environ.get("X402_PRIVATE_KEY")  # opt-in Base data wallet key
        self.total_spent = 0.0
        self.payments = []

    def _request(self, url, headers=None):
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode()
                try:
                    data = json.loads(body)
                except (json.JSONDecodeError, ValueError):
                    data = {"text": body}
                return resp.status, dict(resp.headers), data
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                data = {"text": body}
            return e.code, dict(e.headers), data
        except (urllib.error.URLError, TimeoutError) as e:
            return 0, {}, {"error": str(e)}

    def _parse_terms(self, headers, data):
        """Read payment terms from the 402 (header base64 or JSON body 'accepts')."""
        # Header form
        raw = headers.get("Payment-Required") or headers.get("payment-required")
        if raw:
            try:
                data = json.loads(base64.b64decode(raw).decode())
            except (ValueError, json.JSONDecodeError):
                pass
        accepts = (data or {}).get("accepts")
        if isinstance(accepts, list) and accepts:
            a = accepts[0]
            # amount is in smallest unit (USDC 6 decimals): 10000 = $0.01
            try:
                usd = float(a.get("amount", 0)) / 1_000_000.0
            except (TypeError, ValueError):
                usd = 0.0
            return {"amount_raw": a.get("amount"), "amount_usd": usd,
                    "asset": a.get("asset"), "network": a.get("network"),
                    "payTo": a.get("payTo"), "extra": a.get("extra", {}), "raw": a}
        return None

    def _sign_payment(self, terms) -> PaymentRecord:
        usd = terms["amount_usd"]
        if usd > self.max_price_usdc:
            return PaymentRecord(False, note=f"x402 price ${usd} exceeds cap ${self.max_price_usdc}")

        # Backend A: external signer command (returns the PAYMENT-SIGNATURE string).
        if self.sign_cmd:
            try:
                payload = json.dumps(terms["raw"])
                out = subprocess.run(self.sign_cmd, input=payload, shell=True,
                                     capture_output=True, text=True, timeout=30)
                sig = (out.stdout or "").strip()
                if out.returncode == 0 and sig:
                    self.total_spent += usd
                    rec = PaymentRecord(True, str(usd), terms["asset"], terms["network"],
                                        terms["payTo"], sig, "signed via X402_SIGN_CMD")
                    self.payments.append(rec)
                    return rec
                return PaymentRecord(False, note=f"signer failed: {out.stderr.strip()}")
            except Exception as e:  # noqa: BLE001
                return PaymentRecord(False, note=f"signer error: {e}")

        # Backend B: local EIP-3009 sign with an opt-in Base data-wallet key.
        if self.priv_key:
            sig = _eip3009_sign(self.priv_key, terms)
            if sig:
                self.total_spent += usd
                rec = PaymentRecord(True, str(usd), terms["asset"], terms["network"],
                                    terms["payTo"], sig, "signed locally (Base data wallet)")
                self.payments.append(rec)
                return rec
            return PaymentRecord(False, note="local EIP-3009 sign unavailable (need 'eth-account')")

        # No signer configured -> do NOT fake. Honest unpaid.
        return PaymentRecord(False, note="no x402 signer configured; falling back to API key")

    def get(self, url, headers=None) -> X402Response:
        status, hdrs, data = self._request(url, headers)
        if status != 402:
            ok = 200 <= status < 300
            return X402Response(ok, status, data,
                                PaymentRecord(False, note="endpoint did not require x402"),
                                None if ok else f"HTTP {status}")
        terms = self._parse_terms(hdrs, data)
        if not terms:
            return X402Response(False, 402, data, error="402 but no payment terms")
        rec = self._sign_payment(terms)
        if not rec.paid:
            return X402Response(False, 402, data, rec, rec.note)
        pay_headers = dict(headers or {})
        pay_headers["PAYMENT-SIGNATURE"] = rec.signature
        status2, _, data2 = self._request(url, pay_headers)
        ok = 200 <= status2 < 300
        return X402Response(ok, status2, data2, rec, None if ok else f"retry HTTP {status2}")


def _eip3009_sign(priv_key: str, terms) -> Optional[str]:
    """
    Sign USDC EIP-3009 transferWithAuthorization on Base. Needs 'eth-account'.
    Returns a base64 PAYMENT-SIGNATURE payload, or None if lib missing.
    This uses a SEPARATE low-value Base data wallet key — never the trading key.
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        import secrets, time
    except ImportError:
        return None
    try:
        acct = Account.from_key(priv_key)
        now = int(time.time())
        nonce = "0x" + secrets.token_hex(32)
        domain = {"name": (terms.get("extra") or {}).get("name", "USD Coin"),
                  "version": (terms.get("extra") or {}).get("version", "2"),
                  "chainId": 8453,
                  "verifyingContract": terms["asset"]}
        types = {"TransferWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"}]}
        message = {"from": acct.address, "to": terms["payTo"],
                   "value": int(terms["amount_raw"]),
                   "validAfter": 0, "validBefore": now + 60, "nonce": nonce}
        signed = Account.sign_typed_data(priv_key, domain, types, message)
        proof = {"signature": signed.signature.hex(), "authorization": {
            "from": acct.address, "to": terms["payTo"], "value": str(terms["amount_raw"]),
            "validAfter": "0", "validBefore": str(now + 60), "nonce": nonce}}
        return base64.b64encode(json.dumps(proof).encode()).decode()
    except Exception:  # noqa: BLE001
        return None