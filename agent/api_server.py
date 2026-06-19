"""
AlphaPilot — dashboard API server (read-only bridge to the live agent).

Serves the REAL agent state that agent.py publishes to agent_state.json each
cycle. This process does NOT trade, does NOT sign, and does NOT invent data —
it only reads the agent's published state file and exposes it over HTTP so a
dashboard can display live status, trades, and portfolio.

Zero external dependencies (Python standard library only).

Endpoints (all GET, all read-only):
  /api/health          - server liveness + whether an agent state file exists
  /api/agent-live      - full published agent state (network, live flags, trades)
  /api/agent-trades    - the agent's recorded trades (real on-chain tx hashes)
  /api/agent-portfolio - budget / deployed / available / open positions
  /api/rules           - the risk guardrails the agent is enforcing

If the agent isn't running yet (no state file), endpoints return a clear
{"status":"offline"} response — never fabricated data.

Run:  python api_server.py        (serves on PORT, default 8000)
"""

import http.server
import json
import os
import socketserver
from datetime import datetime, timezone
from urllib.parse import urlparse

PORT = int(os.environ.get("API_PORT", "8000"))
STATE_FILE = os.environ.get("AGENT_STATE_FILE", "agent_state.json")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read_state():
    """Read the agent's published state file. Returns dict or None if absent."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


class APIHandler(http.server.BaseHTTPRequestHandler):

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/health":
            self._send({
                "status": "healthy",
                "agent_state_present": _read_state() is not None,
                "timestamp": _now(),
            })
            return

        # Everything else needs the live state file.
        state = _read_state()
        if state is None:
            self._send({"status": "offline",
                        "message": "agent not running or no state published yet"})
            return

        if path == "/api/agent-live":
            self._send(state)
        elif path == "/api/agent-trades":
            trades = state.get("trades", [])
            self._send({"trades": trades, "count": len(trades),
                        "updated": state.get("updated")})
        elif path == "/api/agent-portfolio":
            out = dict(state.get("portfolio", {}))
            out["network"] = state.get("network")
            out["x402"] = state.get("x402", {})
            out["updated"] = state.get("updated")
            self._send(out)
        elif path == "/api/rules":
            self._send({"rules": state.get("rules", {}),
                        "updated": state.get("updated")})
        else:
            self._send({"error": "endpoint not found", "path": path}, code=404)

    def log_message(self, *args):
        pass  # quiet


if __name__ == "__main__":
    print(f"AlphaPilot dashboard API (read-only) on http://localhost:{PORT}")
    print(f"  reading live agent state from: {STATE_FILE}")
    print("  endpoints: /api/health /api/agent-live /api/agent-trades "
          "/api/agent-portfolio /api/rules")
    with socketserver.TCPServer(("", PORT), APIHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")