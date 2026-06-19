"""
x402 demo resource server (stdlib only).

A minimal SELLER that speaks x402, so you can demonstrate the agent's
pay-per-request loop end-to-end on testnet without depending on an external
provider's x402 endpoint being live.

Flow:
  GET /signal           -> 402 Payment Required + payment terms (price, token, payTo)
  GET /signal + X-PAYMENT header -> 200 + the data

Run:  python x402_server.py        (listens on :8402)
Point the agent at it with:  X402_SIGNAL_URL=http://localhost:8402/signal

This is a DEMO facilitator-less server: it accepts any well-formed X-PAYMENT
header (in production the server verifies the proof with a facilitator's
/verify + /settle). Clearly labelled so it's not mistaken for production.
"""

import http.server
import json
import os
import socketserver
from urllib.parse import urlparse

PORT = int(os.environ.get("X402_PORT", "8402"))

# Where payments should go (your agent wallet or a test recipient).
PAY_TO = os.environ.get("X402_PAYTO", "0x000000000000000000000000000000000000dEaD")
PRICE = os.environ.get("X402_PRICE", "0.01")     # USDC per request
ASSET = os.environ.get("X402_ASSET", "USDC")
NETWORK = os.environ.get("X402_NETWORK", "bsc-testnet")


SAMPLE_SIGNAL = {
    "source": "x402 demo seller",
    "fear_greed": 71,
    "signals": [
        {"symbol": "BNB", "technical": 78, "sentiment": 75, "hint": "BUY"},
        {"symbol": "CAKE", "technical": 72, "sentiment": 70, "hint": "BUY"},
        {"symbol": "ETH", "technical": 64, "sentiment": 68, "hint": "HOLD"},
    ],
}


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path != "/signal":
            self._send(404, {"error": "not found"})
            return

        payment = self.headers.get("X-PAYMENT")
        if not payment:
            # No payment yet -> 402 with structured terms (x402 spec shape).
            terms = {
                "x402Version": 1,
                "error": "Payment Required",
                "accepts": [{
                    "scheme": "exact",
                    "network": NETWORK,
                    "asset": ASSET,
                    "amount": PRICE,
                    "payTo": PAY_TO,
                    "resource": "/signal",
                    "description": "Live trading signal (per-request)",
                }],
            }
            self._send(402, terms)
            return

        # Payment header present -> (demo) accept and serve the data.
        # Production: verify proof via facilitator /verify + /settle here.
        self._send(200, {"paid": True, "data": SAMPLE_SIGNAL})

    def log_message(self, *args):
        pass  # quiet


if __name__ == "__main__":
    print(f"x402 demo seller on http://localhost:{PORT}/signal")
    print(f"  price={PRICE} {ASSET} on {NETWORK} -> {PAY_TO}")
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")