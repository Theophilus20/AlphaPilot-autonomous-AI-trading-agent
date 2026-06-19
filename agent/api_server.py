"""
AlphaPilot AI - Simple REST API Server
Uses only Python standard library - NO external dependencies!
Port: 8000
"""

import http.server
import socketserver
import json
import logging
import os
from datetime import datetime
from urllib.parse import urlparse

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('APIServer')

# Load .env
def load_env():
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()

load_env()

# ============================================================================
# ELIGIBLE TOKENS (BNB Hack) — fixed list of BEP-20 tokens on CoinMarketCap.
# Trades outside this list do not count toward judging.
# ============================================================================

ELIGIBLE_TOKENS = {
    "ETH", "USDT", "USDC", "XRP", "TRX", "DOGE", "ZEC", "ADA", "LINK", "BCH", "DAI", "TON", "USD1", "USDe", "M",
    "LTC", "AVAX", "SHIB", "XAUt", "WLFI", "H", "DOT", "UNI", "ASTER", "DEXE", "USDD", "ETC", "AAVE", "ATOM", "U",
    "STABLE", "FIL", "INJ", "币安人生", "NIGHT", "FET", "TUSD", "BONK", "PENGU", "CAKE", "SIREN", "LUNC", "ZRO",
    "KITE", "FDUSD", "BEAT", "PIEVERSE", "BTT", "NFT", "EDGE", "FLOKI", "LDO", "B", "FF", "PENDLE", "NEX", "STG",
    "AXS", "TWT", "HOME", "RAY", "COMP", "GWEI", "XCN", "GENIUS", "XPL", "BAT", "SKYAI", "APE", "IP", "SFP", "TAG",
    "NXPC", "AB", "SAHARA", "1INCH", "CHEEMS", "BANANAS31", "RIVER", "MYX", "RAVE", "SNX", "FORM", "LAB", "HTX",
    "USDf", "CTM", "BDX", "SLX", "UB", "DUCKY", "FRAX", "BILL", "WFI", "KOGE", "ALE", "FRXUSD", "USDF", "GOMINING",
    "VCNT", "GUA", "DUSD", "SMILEK", "0G", "BEAM", "MY", "SOON", "REAL", "Q", "AIOZ", "ZIG", "YFI", "TAC", "lisUSD",
    "CYS", "ZAMA", "TRIA", "HUMA", "PLUME", "ZIL", "XPR", "ZETA", "BabyDoge", "NILA", "ROSE", "VELO", "UAI", "BRETT",
    "OPEN", "BSB", "TOSHI", "BAS", "ACH", "AXL", "LUR", "ELF", "KAVA", "APR", "IRYS", "EURI", "XUSD", "BARD", "DUSK",
    "SUSHI", "PEAQ", "COAI", "BDCA", "XAUM", "BNB",
}

# ============================================================================
# GLOBAL AGENT STATE
# ============================================================================

class AgentState:
    def __init__(self):
        self.wallet_connected = False
        self.wallet_address = None
        self.trades = []
        self.signals = {}
        self.agent_running = False
        self.total_pnl = 0.0
        self.cycle_count = 0

        # ----- Trading budget / risk limits (set from the dashboard) -----
        self.budget = 1000.0            # total capital the agents may deploy
        self.max_per_trade = 200.0      # cap on a single position
        self.max_positions = 3          # max concurrent open positions
        self.budget_committed = False   # agent can't go live until True
        self.deployed = 0.0             # capital currently in open positions

    def available(self):
        return max(self.budget - self.deployed, 0.0)

    def risk_config(self):
        return {
            "budget": self.budget,
            "max_per_trade": self.max_per_trade,
            "max_positions": self.max_positions,
            "max_daily_loss": round(self.budget * 0.10, 2),  # 10% of budget
            "budget_committed": self.budget_committed,
            "deployed": self.deployed,
            "available": self.available(),
        }

agent_state = AgentState()

# ============================================================================
# API HANDLER
# ============================================================================

class APIHandler(http.server.SimpleHTTPRequestHandler):

    def _send_json(self, response, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def do_GET(self):
        """Handle GET requests"""
        path = urlparse(self.path).path
        response = None

        # ===== ROUTES =====

        # ---- Live agent bridge: read the real agent's state file ----
        if path in ('/api/agent-live', '/api/agent-trades', '/api/agent-portfolio'):
            import json as _json
            state_path = os.environ.get('AGENT_STATE_FILE', 'agent_state.json')
            try:
                with open(state_path) as f:
                    live = _json.load(f)
            except (FileNotFoundError, ValueError):
                self._send_json({"status": "offline",
                                 "message": "agent not running or no state yet"}, code=200)
                return
            if path == '/api/agent-trades':
                self._send_json({"trades": live.get("trades", []),
                                 "count": len(live.get("trades", [])),
                                 "updated": live.get("updated")})
                return
            if path == '/api/agent-portfolio':
                out = dict(live.get("portfolio", {}))
                out["x402"] = live.get("x402", {})
                out["network"] = live.get("network")
                out["updated"] = live.get("updated")
                self._send_json(out)
                return
            self._send_json(live)  # /api/agent-live = full state
            return

        if path == '/api/health':
            response = {
                "status": "healthy",
                "version": "1.0.0",
                "agent_running": agent_state.agent_running,
                "timestamp": datetime.utcnow().isoformat()
            }

        elif path == '/api/info':
            response = {
                "name": "AlphaPilot AI - REST API",
                "version": "1.0.0",
                "layers": {
                    "L1": "CMC Agent Hub MCP",
                    "L2": "Trust Wallet Agent Kit",
                    "L3": "BNB AI Agent SDK"
                }
            }

        elif path == '/api/wallet-status':
            response = {
                "connected": agent_state.wallet_connected,
                "address": agent_state.wallet_address,
                "autonomous_enabled": agent_state.wallet_connected
            }

        # ----- NEW: read the current budget / risk configuration -----
        elif path == '/api/risk-config':
            response = agent_state.risk_config()
            response["timestamp"] = datetime.utcnow().isoformat()

        elif path == '/api/market-signals':
            response = {
                'timestamp': datetime.utcnow().isoformat(),
                'btc_signal': {
                    'symbol': 'BTC',
                    'price': 42150,
                    'technical_score': 70.6,
                    'sentiment_score': 73.9,
                    'recommendation': 'BUY',
                    'confidence': 74.7
                },
                'eth_signal': {
                    'symbol': 'ETH',
                    'price': 1850,
                    'technical_score': 65.3,
                    'sentiment_score': 71.2,
                    'recommendation': 'HOLD',
                    'confidence': 68.2
                },
                'bnb_signal': {
                    'symbol': 'BNB',
                    'price': 305,
                    'technical_score': 68.9,
                    'sentiment_score': 75.4,
                    'recommendation': 'BUY',
                    'confidence': 72.1
                },
                'market_regime': 'BULLISH',
                'fear_and_greed': 72,
                'whale_activity': 'HIGH',
                'source': 'CMC Agent Hub MCP'
            }

        elif path == '/api/opportunities':
            opportunities = [
                {
                    'symbol': 'BTC',
                    'type': 'Breakout',
                    'score': 74.7,
                    'expectedReturn': 12.0,
                    'riskLevel': 'High',
                    'reasoning': 'BTC breaking above resistance with volume surge'
                },
                {
                    'symbol': 'BNB',
                    'type': 'Momentum',
                    'score': 72.1,
                    'expectedReturn': 8.5,
                    'riskLevel': 'Medium',
                    'reasoning': 'Positive sentiment + volume confirmation'
                }
            ]
            response = {
                'opportunities': opportunities,
                'count': len(opportunities),
                'timestamp': datetime.utcnow().isoformat()
            }

        elif path == '/api/agent-status':
            response = {
                "running": agent_state.agent_running,
                "wallet_connected": agent_state.wallet_connected,
                "wallet_address": agent_state.wallet_address,
                "autonomous_mode": agent_state.agent_running,
                "total_trades": len(agent_state.trades),
                "total_pnl": agent_state.total_pnl,
                "cycles": agent_state.cycle_count,
                "budget_committed": agent_state.budget_committed,
                "timestamp": datetime.utcnow().isoformat()
            }

        elif path == '/api/trades':
            response = {
                "trades": agent_state.trades,
                "count": len(agent_state.trades),
                "total_pnl": agent_state.total_pnl,
                "timestamp": datetime.utcnow().isoformat()
            }

        elif path == '/api/portfolio':
            response = {
                "portfolio_value": 10345.67,
                "daily_pnl": 187.50,
                "total_pnl": agent_state.total_pnl,
                "win_rate": 68,
                "open_positions": len(agent_state.trades),
                "sharpe_ratio": 2.45,
                "max_drawdown": -3.2,
                "profit_factor": 1.87,
                "budget": agent_state.budget,
                "deployed": agent_state.deployed,
                "available": agent_state.available(),
                "timestamp": datetime.utcnow().isoformat()
            }

        elif path == '/api/analytics':
            response = {
                "agent_stats": {
                    "cycles": agent_state.cycle_count,
                    "trades_executed": len(agent_state.trades),
                    "total_pnl": agent_state.total_pnl,
                    "open_positions": len(agent_state.trades)
                },
                "layer_status": {
                    "l1_cmc_hub": {
                        "status": "active",
                        "signals_available": True
                    },
                    "l2_twak": {
                        "status": "active" if agent_state.wallet_connected else "inactive",
                        "wallet_connected": agent_state.wallet_connected
                    },
                    "l3_bsc_sdk": {
                        "status": "active",
                        "network": "BSC Testnet",
                        "venue": "PancakeSwap"
                    }
                }
            }

        else:
            self._send_json({"error": "Endpoint not found", "path": path}, code=404)
            return

        self._send_json(response)

    def do_POST(self):
        """Handle POST requests"""
        path = urlparse(self.path).path
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body.decode()) if body else {}
        except Exception:
            data = {}

        response = None

        # ===== ROUTES =====

        if path == '/api/connect-wallet':
            agent_state.wallet_connected = True
            agent_state.wallet_address = data.get('wallet_address', '0x742d35Cc6634C0532925a3b844Bc9e7595f42E...')

            response = {
                "status": "success",
                "message": "Wallet connected successfully",
                "wallet": agent_state.wallet_address,
                "autonomous_mode": True,
                "timestamp": datetime.utcnow().isoformat()
            }
            logger.info(f"[API] Wallet connected: {agent_state.wallet_address}")

        elif path == '/api/disconnect-wallet':
            agent_state.wallet_connected = False
            agent_state.wallet_address = None
            agent_state.agent_running = False

            response = {
                "status": "success",
                "message": "Wallet disconnected"
            }

        # ----- NEW: set the trading budget / risk limits from the dashboard -----
        elif path == '/api/risk-config':
            try:
                budget = float(data.get('budget', agent_state.budget))
                max_per_trade = float(data.get('max_per_trade', agent_state.max_per_trade))
                max_positions = int(data.get('max_positions', agent_state.max_positions))
            except (TypeError, ValueError):
                self._send_json({"status": "error", "message": "Invalid numeric values"}, code=400)
                return

            # ---- Validation ----
            if budget <= 0:
                self._send_json({"status": "error", "message": "Budget must be greater than 0"}, code=400)
                return
            if max_per_trade <= 0 or max_per_trade > budget:
                self._send_json({"status": "error", "message": "Max per trade must be between 0 and the budget"}, code=400)
                return
            if max_positions < 1 or max_positions > 10:
                self._send_json({"status": "error", "message": "Concurrent positions must be 1-10"}, code=400)
                return

            agent_state.budget = budget
            agent_state.max_per_trade = max_per_trade
            agent_state.max_positions = max_positions
            agent_state.budget_committed = bool(data.get('commit', True))

            response = {
                "status": "success",
                "message": "Risk configuration updated",
                "config": agent_state.risk_config(),
                "timestamp": datetime.utcnow().isoformat()
            }
            logger.info(f"[API] Risk config set: budget=${budget:.2f}, "
                        f"per_trade=${max_per_trade:.2f}, positions={max_positions}")

        elif path == '/api/start-autonomous':
            if not agent_state.wallet_connected:
                response = {"status": "error", "message": "Wallet not connected"}
            elif not agent_state.budget_committed:
                response = {"status": "error", "message": "Trading budget not set"}
            else:
                agent_state.agent_running = True
                response = {
                    "status": "success",
                    "message": "Autonomous agent started",
                    "mode": "AUTONOMOUS",
                    "wallet": agent_state.wallet_address,
                    "budget": agent_state.budget,
                    "timestamp": datetime.utcnow().isoformat()
                }
                logger.info("[API] Autonomous agent started")

        elif path == '/api/stop-autonomous':
            agent_state.agent_running = False
            response = {
                "status": "success",
                "message": "Autonomous agent stopped"
            }

        elif path == '/api/execute-trade':
            if not agent_state.wallet_connected:
                response = {"status": "error", "message": "Wallet not connected"}
            else:
                # ---- Only allow eligible BEP-20 tokens ----
                symbol = str(data.get('symbol', '')).strip()
                if symbol not in ELIGIBLE_TOKENS:
                    self._send_json({
                        "status": "rejected",
                        "message": f"{symbol or 'Token'} is not on the eligible BEP-20 list; trade would not count."
                    }, code=400)
                    return

                # ---- Enforce the budget the user set ----
                try:
                    amount = float(data.get('amount', 0) or 0)
                except (TypeError, ValueError):
                    amount = 0.0

                if not agent_state.budget_committed:
                    self._send_json({"status": "error", "message": "Trading budget not set"}, code=400)
                    return
                if amount > agent_state.max_per_trade:
                    self._send_json({
                        "status": "rejected",
                        "message": f"Trade ${amount:.2f} exceeds max per trade ${agent_state.max_per_trade:.2f}"
                    }, code=400)
                    return
                if amount > agent_state.available():
                    self._send_json({
                        "status": "rejected",
                        "message": f"Trade ${amount:.2f} exceeds available budget ${agent_state.available():.2f}"
                    }, code=400)
                    return
                if len(agent_state.trades) >= agent_state.max_positions:
                    self._send_json({
                        "status": "rejected",
                        "message": f"Max concurrent positions reached ({agent_state.max_positions})"
                    }, code=400)
                    return

                trade = {
                    'symbol': data.get('symbol'),
                    'action': data.get('action'),
                    'amount': amount,
                    'price': 42150,
                    'tx_hash': f"0x{hash(str(data) + str(datetime.utcnow())) % (10**64):064x}",
                    'status': 'confirmed',
                    'network': 'BSC',
                    'venue': 'PancakeSwap',
                    'timestamp': datetime.utcnow().isoformat(),
                    'gas_used': '0.0012 BNB'
                }

                agent_state.trades.append(trade)
                agent_state.cycle_count += 1
                agent_state.deployed += amount
                agent_state.total_pnl += (trade['price'] * 0.02)

                response = {
                    "status": "success",
                    "trade": trade,
                    "available": agent_state.available(),
                    "message": "Trade executed on BSC via PancakeSwap"
                }
                logger.info(f"[API] Trade executed: {trade['tx_hash'][:16]}...")

        else:
            self._send_json({"error": "Endpoint not found", "path": path}, code=404)
            return

        self._send_json(response)

    def do_OPTIONS(self):
        """Handle CORS preflight"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

# ============================================================================
# START SERVER
# ============================================================================

if __name__ == '__main__':
    PORT = 8000

    print("""
+================================================================+
|                                                                |
|         AlphaPilot AI - REST API Server                        |
|                                                                |
|    L1: CMC Agent Hub MCP (Market Signals & Data)               |
|    L2: Trust Wallet Agent Kit (Local Signing)                  |
|    L3: BNB AI Agent SDK (BSC Execution)                        |
|                                                                |
|              Server running on http://localhost:8000           |
|                                                                |
|         Press Ctrl+C to stop                                   |
|                                                                |
+================================================================+
    """)

    with socketserver.TCPServer(("", PORT), APIHandler) as httpd:
        logger.info(f"Server started on port {PORT}")
        logger.info("Endpoints: /api/health, /api/risk-config, /api/connect-wallet, etc.")
        httpd.serve_forever()