"""
x402 demo seller — a tiny local HTTP endpoint that requires x402 payment.

This is a LOCAL DEMO/TEST tool, not part of the live trading path. It lets you
exercise the full x402 handshake end-to-end against x402_client.py without
hitting a paid third-party API:

  GET /signal  with no payment   -> HTTP 402 + structured payment terms
  GET /signal  with X-PAYMENT    -> 200 + a sample trading signal

The payment terms advertise USDC on Base (the network CMC's x402 uses). In
production you'd verify the payment proof via a facilitator's /verify + /settle;
here we accept the presence of the payment header so the client flow can be
demonstrated locally. Nothing here touches real funds.

Run:  python x402_server.py        (serves on X402_PORT, default 8402)
"""

import http.server
import json
import os
import socketserver
from urllib.parse import urlparse

PORT = int(os.environ.get("X402_PORT", "8402"))

# Where payments should go (a test recipient by default).
PAY_TO = os.environ.get("X402_PAYTO", "0x000000000000000000000000000000000000dEaD")
PRICE = os.environ.get("X402_PRICE", "10000")    # USDC smallest unit: 10000 = $0.01
ASSET = os.environ.get("X402_ASSET", "USDC")
# CMC's x402 settles in USDC on Base (eip155:8453).
NETWORK = os.environ.get("X402_NETWORK", "base")


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