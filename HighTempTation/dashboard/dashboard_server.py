#!/usr/bin/env python3
"""
HighTempTation 天气看板独立服务器

独立于交易 Bot 运行，纯看板展示。
提供：
  - /              → dashboard.html (新天气面板)
  - /api/status    → 模拟天气交易数据 (无后端时自动生成)
  - /chart.umd.min.js → Chart.js 库

启动方式:
  python3 dashboard_server.py
  pm2 start ecosystem.config.cjs --only weather-dashboard

端口: 3002（可通过 Nginx/Caddy 反向代理对外暴露）
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dashboard-server")

# ── 路径配置 (自包含路径) ──
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)  # HighTempTation/
DASHBOARD_HTML = os.path.join(PROJECT_ROOT, "dashboard.html")
CHART_JS = os.path.join(HERE, "chart.umd.min.js")

# 确保路径存在，不存在则使用兜底
if not os.path.exists(DASHBOARD_HTML):
    DASHBOARD_HTML = os.path.join(HERE, "dashboard.html")
if not os.path.exists(CHART_JS):
    CHART_JS = os.path.join(HERE, "chart.umd.min.js")

# ── 服务器配置 ──
HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("DASHBOARD_PORT", "3002"))
REFRESH_SEC = int(os.environ.get("REFRESH_SEC", "30"))

# ── 模拟数据生成 ──
_MOCK_CITIES = [
    {"city": "Tokyo", "bucket": "37°C", "entry_no": 0.35, "curr_no": 0.42, "pnl": 7.00},
    {"city": "Hong Kong", "bucket": "33°C", "entry_no": 0.22, "curr_no": 0.28, "pnl": 6.00},
    {"city": "Singapore", "bucket": "34°C", "entry_no": 0.18, "curr_no": 0.31, "pnl": 13.00},
    {"city": "Bangkok", "bucket": "36°C", "entry_no": 0.28, "curr_no": 0.45, "pnl": 17.00},
    {"city": "Dubai", "bucket": "41°C", "entry_no": 0.55, "curr_no": 0.72, "pnl": 8.50},
]
_MOCK_CLOSED = [
    {"city": "New York", "bucket": "29°C", "pnl": 8.50, "exit_time": None, "exit_reason": "TP1"},
    {"city": "London", "bucket": "22°C", "pnl": -3.20, "exit_time": None, "exit_reason": "SL"},
    {"city": "Sydney", "bucket": "26°C", "pnl": 12.40, "exit_time": None, "exit_reason": "TP2"},
    {"city": "Paris", "bucket": "28°C", "pnl": 5.80, "exit_time": None, "exit_reason": "TP1"},
    {"city": "Berlin", "bucket": "25°C", "pnl": -1.50, "exit_time": None, "exit_reason": "expired"},
]

INITIAL_CAPITAL = 10000.0
_capital_history: list = []
_last_update = 0.0


def _generate_mock_status() -> dict:
    """生成带实时感的模拟天气交易数据"""
    global _capital_history, _last_update
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    # 每 30s 微调价格模拟波动
    if now_ts - _last_update > 25:
        import random
        open_positions = []
        total_pnl = 0
        for m in _MOCK_CITIES:
            # 模拟价格波动 ±5%
            delta = m["entry_no"] * random.uniform(-0.05, 0.08)
            curr = round(m["entry_no"] + delta, 4)
            curr = max(0.01, min(0.99, curr))
            pnl = round((curr - m["entry_no"]) * 100, 2)
            if m["bucket"].endswith("°C") and int(m["bucket"][:-2]) > 35:
                pnl = abs(pnl)  # 高温方向偏 YES
            open_positions.append({
                "city": m["city"],
                "bucket": m["bucket"],
                "entry_no": m["entry_no"],
                "curr_no": curr,
                "pnl": pnl,
                "entry_time": (now - timedelta(hours=random.randint(1, 8))).isoformat(),
            })
            total_pnl += pnl

        # 平仓记录 (带上时间)
        closed = []
        for c in _MOCK_CLOSED:
            closed.append({
                "city": c["city"],
                "bucket": c["bucket"],
                "pnl": c["pnl"],
                "realized": c["pnl"],
                "exit_time": (now - timedelta(hours=random.randint(1, 12))).isoformat(),
                "exit_reason": c["exit_reason"],
            })

        # 资金曲线
        current_capital = INITIAL_CAPITAL + total_pnl
        _capital_history.append([now.isoformat(), round(current_capital, 2)])
        if len(_capital_history) > 500:
            _capital_history = _capital_history[-500:]

        _last_update = now_ts

        return {
            "capital": round(current_capital, 2),
            "initial_capital": INITIAL_CAPITAL,
            "total_pnl": round(total_pnl, 2),
            "daily_pnl": round(total_pnl, 2),
            "open_count": len(open_positions),
            "open": open_positions,
            "recent_closed": closed,
            "capital_history": _capital_history,
            "wins": 8,
            "losses": 3,
            "total": 11,
            "win_rate": 72.7,
            "server_time": now.isoformat(),
            "mode": "standalone_dashboard",
            "status": "online",
        }

    # 缓存期内直接返回最后生成的数据
    return {
        "capital": 10043.70,
        "initial_capital": INITIAL_CAPITAL,
        "total_pnl": 43.70,
        "daily_pnl": 43.70,
        "open_count": 5,
        "open": [
            {"city": "Tokyo", "bucket": "37°C", "entry_no": 0.35, "curr_no": 0.42, "pnl": 7.00,
             "entry_time": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()},
            {"city": "Hong Kong", "bucket": "33°C", "entry_no": 0.22, "curr_no": 0.28, "pnl": 6.00,
             "entry_time": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
            {"city": "Singapore", "bucket": "34°C", "entry_no": 0.18, "curr_no": 0.31, "pnl": 13.00,
             "entry_time": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()},
            {"city": "Bangkok", "bucket": "36°C", "entry_no": 0.28, "curr_no": 0.45, "pnl": 17.00,
             "entry_time": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()},
            {"city": "Dubai", "bucket": "41°C", "entry_no": 0.55, "curr_no": 0.72, "pnl": 8.50,
             "entry_time": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()},
        ],
        "recent_closed": [
            {"city": "New York", "bucket": "29°C", "pnl": 8.50, "realized": 8.50,
             "exit_time": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(), "exit_reason": "TP1"},
            {"city": "London", "bucket": "22°C", "pnl": -3.20, "realized": -3.20,
             "exit_time": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(), "exit_reason": "SL"},
            {"city": "Sydney", "bucket": "26°C", "pnl": 12.40, "realized": 12.40,
             "exit_time": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "exit_reason": "TP2"},
            {"city": "Paris", "bucket": "28°C", "pnl": 5.80, "realized": 5.80,
             "exit_time": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), "exit_reason": "TP1"},
            {"city": "Berlin", "bucket": "25°C", "pnl": -1.50, "realized": -1.50,
             "exit_time": (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(), "exit_reason": "expired"},
        ],
        "capital_history": _capital_history or [
            [datetime.now(timezone.utc).isoformat(), INITIAL_CAPITAL],
            [(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), INITIAL_CAPITAL],
        ],
        "wins": 8, "losses": 3, "total": 11, "win_rate": 72.7,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "mode": "standalone_dashboard",
        "status": "online",
    }


def _generate_mock_trades(limit: int = 100) -> dict:
    """生成模拟历史交易数据"""
    import random
    now = datetime.now(timezone.utc)
    
    open_trades = [
        {"city": "Tokyo", "side": "37°C", "entry_price": 0.35, "size": 20,
         "entry_time": (now - timedelta(hours=2)).isoformat()},
        {"city": "Hong Kong", "side": "33°C", "entry_price": 0.22, "size": 20,
         "entry_time": (now - timedelta(hours=1)).isoformat()},
        {"city": "Singapore", "side": "34°C", "entry_price": 0.18, "size": 20,
         "entry_time": (now - timedelta(minutes=30)).isoformat()},
    ]
    
    trades = []
    city_data = [
        ("New York", "29°C", 0.42, 0.51, 8.50, "TP1"),
        ("London", "22°C", 0.38, 0.31, -3.20, "SL"),
        ("Sydney", "26°C", 0.25, 0.37, 12.40, "TP2"),
        ("Paris", "28°C", 0.55, 0.63, 5.80, "TP1"),
        ("Berlin", "25°C", 0.33, 0.31, -1.50, "expired"),
        ("Mumbai", "35°C", 0.28, 0.42, 14.00, "TP1"),
        ("Seoul", "31°C", 0.45, 0.52, 6.30, "TP2"),
        ("Osaka", "34°C", 0.19, 0.16, -2.80, "SL"),
        ("Beijing", "38°C", 0.62, 0.71, 8.20, "TP1"),
        ("Shanghai", "36°C", 0.48, 0.55, 7.50, "TP1"),
    ]
    for i, (city, side, entry, exit, pnl, reason) in enumerate(city_data):
        trades.append({
            "city": city,
            "side": side,
            "entry_price": entry,
            "exit_price": exit,
            "pnl": pnl,
            "size": random.randint(10, 30),
            "exit_reason": reason,
            "entry_time": (now - timedelta(hours=random.randint(6, 48))).isoformat(),
            "exit_time": (now - timedelta(hours=random.randint(1, 12))).isoformat(),
            "bucket_lower": int(side[:-2]) - 1,
            "bucket_upper": int(side[:-2]) + 1,
        })
    
    return {
        "trades": trades,
        "open_trades": open_trades,
        "total": len(trades),
        "limit": limit,
        "server_time": now.isoformat(),
    }


def _generate_mock_strategies() -> dict:
    """生成模拟策略归因数据"""
    return {
        "strategies": {
            "MODEL": {
                "name": "MODEL", "display_name": "模型预报",
                "total": 47, "wins": 32, "losses": 15,
                "win_rate": 68.1, "avg_pnl": 5.42, "total_pnl": 254.67,
                "is_open_pnl": 43.50, "open_count": 3,
                "enabled": True,
            },
            "BAYESIAN": {
                "name": "BAYESIAN", "display_name": "贝叶斯修正",
                "total": 23, "wins": 18, "losses": 5,
                "win_rate": 78.3, "avg_pnl": 8.15, "total_pnl": 187.45,
                "is_open_pnl": 12.30, "open_count": 1,
                "enabled": True,
            },
            "METAR": {
                "name": "METAR", "display_name": "极端扫描(METAR)",
                "total": 15, "wins": 11, "losses": 4,
                "win_rate": 73.3, "avg_pnl": 6.75, "total_pnl": 101.25,
                "is_open_pnl": 18.90, "open_count": 2,
                "enabled": True,
            },
            "HKO": {
                "name": "HKO", "display_name": "极端扫描(HKO)",
                "total": 8, "wins": 6, "losses": 2,
                "win_rate": 75.0, "avg_pnl": 4.38, "total_pnl": 35.00,
                "is_open_pnl": 0.0, "open_count": 0,
                "enabled": True,
            },
            "LADDER": {
                "name": "LADDER", "display_name": "温度阶梯",
                "total": 12, "wins": 8, "losses": 4,
                "win_rate": 66.7, "avg_pnl": 3.92, "total_pnl": 47.00,
                "is_open_pnl": 5.60, "open_count": 1,
                "enabled": True,
            },
            "_NEAR_SETTLEMENT": {
                "name": "临近结算", "display_name": "临近结算",
                "total": 18, "wins": 14, "losses": 4,
                "win_rate": 77.8, "avg_pnl": 3.45, "total_pnl": 62.10,
                "is_open_pnl": 0.0, "open_count": 0,
                "enabled": True,
            },
        },
        "toggles": {
            "MODEL": True,
            "BAYESIAN": True,
            "METAR": True,
            "HKO": True,
            "LADDER": True,
        },
        "total_strategies": 6,
        "enabled_count": 6,
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    """独立看板的 HTTP 请求处理器"""

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def _route(self, method: str):
        """统一路由：HEAD 和 GET 共用路由逻辑，HEAD 不写 body"""
        path = self.path.split("?")[0]  # 去掉查询参数
        if path == "/api/strategies":
            self._json(_generate_mock_strategies(), method)
        elif path == "/api/status":
            self._json(_generate_mock_status(), method)
        elif path in ("/", "/dashboard", "/index.html"):
            self._serve_html(method)
        elif path == "/api/trades":
            self._json(_generate_mock_trades(), method)
        elif path == "/api/strategy/toggle":
            self._json({"error": "单机模式下不支持策略开关"}, method)
        elif path == "/chart.umd.min.js":
            self._serve_chart_js(method)
        elif path == "/favicon.ico":
            self.send_error(204)
        else:
            self.send_error(404)

    def _json(self, data: dict, method: str = "GET"):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(body)

    def _serve_html(self, method: str = "GET"):
        """提供天气看板 HTML"""
        path = DASHBOARD_HTML
        if not os.path.exists(path):
            path = os.path.join(HERE, "dashboard.html")
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(body)
        except Exception as e:
            logger.warning(f"读取看板 HTML 失败: {e}")
            self.send_error(500, "Dashboard file not found")

    def _serve_chart_js(self, method: str = "GET"):
        """提供 Chart.js 库"""
        path = CHART_JS
        if not os.path.exists(path):
            path = os.path.join(HERE, "chart.umd.min.js")
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(data)
        except Exception as e:
            logger.warning(f"读取 chart.js 失败: {e}")
            self.send_error(404)

    def log_message(self, fmt, *args):
        logger.info(f"{self.client_address[0]} - {fmt % args}")


def main():
    logger.info("=" * 50)
    logger.info("🌤️  HighTempTation 天气看板独立服务器")
    logger.info(f"📄  HTML 文件: {DASHBOARD_HTML}")
    logger.info(f"📊  API 端口: {PORT}")
    logger.info(f"🔄  刷新间隔: {REFRESH_SEC}s")
    logger.info(f"🔗  http://{HOST}:{PORT}")
    logger.info("=" * 50)

    server = HTTPServer((HOST, PORT), DashboardHandler)
    logger.info(f"✅ 服务器已启动 → http://{HOST}:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("🛑 服务器关闭")
        server.shutdown()


if __name__ == "__main__":
    main()
