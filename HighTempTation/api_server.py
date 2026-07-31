#!/usr/bin/env python3
"""
HighTempTation — FastAPI 仪表板/API 服务器 (Day1 关键项 #4)

替代手写 http.server (旧 DashboardHandler)，提供:
  - 自动交互式文档 /docs (FastAPI + Swagger UI)
  - 保留全部旧端点: /api/status /api/signals /api/analyses /api/positions
    /api/trades /api/bayesian /api/strategies /api/capital /api/reload
    /api/strategy/toggle (POST)
  - 新增生产级端点:
      GET /api/metrics       → 校准指标 (ECE/Brier/可靠性曲线) + 成本模型统计
                                + 组合风控状态 + 信号/持仓统计
      GET /api/calibration   → 可靠性曲线数据 (JSON)
      GET /api/calibration/diagram → Reliability Diagram PNG
      GET /api/health        → 进程健康检查
  - 静态页面: / → dashboard.html, /chart.umd.min.js

架构: bot.py 主循环 (asyncio) 保持单进程, 本模块在 daemon 线程内
      跑独立 uvicorn 事件循环, 通过 bot 模块级全局变量共享状态
      (_engine / _latest_signals / _latest_analyses / _calibrator / ...)。

用法 (由 bot.py 调用, 无需单独启动):
  from api_server import start_api_server
  start_api_server(cfg.DASHBOARD_HOST, cfg.DASHBOARD_PORT)
"""
import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger("api_server")

_APP = None
_SERVER = None
_THREAD: Optional[threading.Thread] = None


def _bot():
    """延迟获取 bot 模块, 避免循环依赖 (bot.py → api_server.py → bot.py)。

    关键: bot 以 `python3 bot.py` 运行时模块名是 __main__,
    `import bot` 会重新执行一份 bot.py 导致全局变量分裂 (_engine=None)。
    因此优先返回 __main__; 仅当 __main__ 不是 bot 时 (独立启动本模块) 才 import bot。
    """
    import sys
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and hasattr(main_mod, "_engine"):
        return main_mod
    import bot
    return bot


def _engine():
    return _bot()._engine


def _json_ok(data: dict) -> dict:
    return data


# ════════════════════════════════════════════════════════════════════
# FastAPI 应用
# ════════════════════════════════════════════════════════════════════

def _create_app():
    """构建 FastAPI 应用 (含全部旧端点 + 新生产级端点)"""
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse

    app = FastAPI(
        title="HighTempTation 天气交易 Bot API",
        description=(
            "生产级架构: 概率校准 (Isotonic) + 成本模型 + 组合风控 + FastAPI。\n\n"
            "- `/api/metrics`: 校准 ECE/Brier、成本模型统计、风控熔断状态\n"
            "- `/api/calibration`: 可靠性曲线数据 + Reliability Diagram\n"
            "- `/docs`: 本交互式文档 (自动生成)"
        ),
        version="2.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _dashboard_html = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   '../tools/hightemptation_live/dashboard.html')
    _chart_js = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chart.umd.min.js')
    if not os.path.exists(_chart_js):
        _chart_js = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '../../services/webhook/chart.umd.min.js')

    # ── 健康检查 ──
    @app.get("/api/health")
    def health():
        b = _bot()
        return {
            "status": "ok",
            "time": b.datetime.now(b.timezone.utc).isoformat(),
            "engine": _engine() is not None,
            "scan_count": b._last_scan,
        }

    # ── 生产级: 指标汇总 (校准 + 成本 + 风控 + 信号统计) ──
    @app.get("/api/metrics")
    def metrics():
        b = _bot()
        out = {
            "time": b.datetime.now(b.timezone.utc).isoformat(),
            "calibrator": b._calibrator.get_metrics() if b._calibrator else {"enabled": False},
            "cost_model": b._cost_model.get_stats() if b._cost_model else {"enabled": False},
            "portfolio_risk": b._portfolio_risk.get_stats() if b._portfolio_risk else {"enabled": False},
            "signals": {
                "total_latest": len(b._latest_signals or []),
                "last_scan": b._last_scan,
            },
        }
        e = _engine()
        if e:
            s = e.summary()
            out["engine"] = {
                "capital": s.get("capital"),
                "daily_pnl": s.get("daily_pnl"),
                "total_pnl": s.get("total_pnl"),
                "open_count": s.get("open_count"),
                "total": s.get("total"),
                "win_rate": s.get("win_rate"),
            }
        return out

    # ── 生产级: 校准可靠性数据 ──
    @app.get("/api/calibration")
    def calibration():
        b = _bot()
        if not b._calibrator:
            return {"enabled": False}
        return {
            "enabled": True,
            "curve": b._calibrator.get_reliability_curve(),
            "metrics": b._calibrator.get_metrics(with_cv=False),
            "diagram": "/api/calibration/diagram",
        }

    # ── 生产级: Reliability Diagram PNG ──
    @app.get("/api/calibration/diagram")
    def calibration_diagram():
        b = _bot()
        if not b._calibrator:
            return JSONResponse({"error": "校准器未初始化"}, status_code=404)
        path = os.path.join(b.os.path.dirname(b.os.path.abspath(b.__file__)),
                            "dashboard", "reliability_diagram.png")
        saved = b._calibrator.plot_reliability_diagram(path)
        if not saved or not os.path.exists(saved):
            return JSONResponse({"error": "Reliability Diagram 暂不可用 (校准样本不足或 matplotlib 缺失), 积累已结算信号后自动生成"},
                                status_code=404)
        return FileResponse(saved, media_type="image/png")

    # ── 旧端点: 状态 ──
    @app.get("/api/status")
    def status():
        return _engine().summary() if _engine() else {}

    @app.get("/api/signals")
    def signals():
        b = _bot()
        return {"signals": b._latest_signals, "scan_time": b._last_scan}

    @app.get("/api/analyses")
    def analyses():
        b = _bot()
        return {"analyses": b._latest_analyses[-100:]}

    @app.get("/api/positions")
    def positions():
        s = _engine().summary() if _engine() else {}
        return {"open": s.get("open", []), "closed": s.get("recent_closed", [])}

    @app.get("/api/capital")
    def capital():
        s = _engine().summary() if _engine() else {}
        return {
            "initial": s.get("initial_capital", 0),
            "current": s.get("capital", 0),
            "total_pnl": s.get("total_pnl", 0),
            "daily_pnl": s.get("daily_pnl", 0),
            "history": s.get("capital_history", []),
        }

    @app.get("/api/reload")
    def reload_cfg():
        b = _bot()
        b.reload_config()
        return {"status": "ok", "message": "配置已熱更新"}

    @app.get("/api/trades")
    def trades(limit: int = 100, request: Request = None):
        limit = max(1, min(limit, 500))
        trades_data = {"trades": [], "open_trades": [], "total_closed": 0}
        e = _engine()
        if e and e.db:
            try:
                closed_trades = e.db.get_recent_trades(limit=limit)
                open_trades = e.db.get_open_trades()
                for t in closed_trades:
                    for k in ("entry_time", "exit_time", "created_at"):
                        if isinstance(t.get(k), str):
                            t[k] = t[k][:19]
                for t in open_trades:
                    for k in ("entry_time", "created_at"):
                        if isinstance(t.get(k), str):
                            t[k] = t[k][:19]
                trades_data["trades"] = closed_trades
                trades_data["open_trades"] = open_trades
                trades_data["total_closed"] = len(closed_trades)
            except Exception as ex:
                logger.warning(f"读取 TradeDB 失败: {ex}")
                trades_data["error"] = str(ex)
        else:
            trades_data["error"] = "TradeDB 未初始化"
        return trades_data

    @app.get("/api/bayesian")
    def bayesian(limit: int = 50, city: str = "", days: int = 7):
        limit = max(1, min(limit, 200))
        result = {"decisions": [], "summary": {}}
        e = _engine()
        if e and e.db:
            try:
                decisions = e.db.get_bayesian_decisions(
                    limit=limit, city=city or None)
                for d in decisions:
                    if isinstance(d.get("created_at"), str):
                        d["created_at"] = d["created_at"][:19]
                result["decisions"] = decisions
                result["summary"] = e.db.get_bayesian_summary(days=days)
            except Exception as ex:
                result["error"] = str(ex)
        return result

    @app.get("/api/strategies")
    def strategies():
        b = _bot()
        return b.get_strategies_data() if hasattr(b, "get_strategies_data") else {}

    @app.post("/api/strategy/toggle")
    async def strategy_toggle(request: Request):
        b = _bot()
        try:
            data = await request.json()
        except Exception:
            data = {}
        strategy_name = data.get("strategy", "")
        enabled = data.get("enabled")
        if not strategy_name:
            return {"error": "缺少 strategy 字段"}
        if strategy_name not in b._strategy_toggles:
            return {"error": f"未知策略: {strategy_name}",
                    "available": list(b._strategy_toggles.keys())}
        if enabled is None:
            b._strategy_toggles[strategy_name] = not b._strategy_toggles[strategy_name]
        else:
            b._strategy_toggles[strategy_name] = bool(enabled)
        new_state = b._strategy_toggles[strategy_name]
        logger.info(f"🕹️ 策略开关 [{strategy_name}] → {'🟢 已启用' if new_state else '🔴 已暂停'}")
        return {"status": "ok", "strategy": strategy_name,
                "enabled": new_state,
                "message": "🟢 已启用" if new_state else "🔴 已暂停",
                "toggles": dict(b._strategy_toggles)}

    # ── 静态页面 ──
    @app.get("/")
    def index():
        return FileResponse(_dashboard_html, media_type="text/html")

    @app.get("/dashboard")
    def dashboard_page():
        return FileResponse(_dashboard_html, media_type="text/html")

    @app.get("/chart.umd.min.js")
    def chart_js():
        return FileResponse(_chart_js, media_type="application/javascript")

    return app


# ════════════════════════════════════════════════════════════════════
# 启动 (daemon 线程内跑 uvicorn)
# ════════════════════════════════════════════════════════════════════

def start_api_server(host: str = "0.0.0.0", port: int = 3002) -> bool:
    """
    在后台线程启动 FastAPI + uvicorn 服务器。

    bot.py 主循环 (asyncio) 不受影响; 服务器线程拥有独立事件循环。
    返回是否启动成功。
    """
    global _APP, _SERVER, _THREAD
    if _THREAD and _THREAD.is_alive():
        return True
    try:
        import uvicorn
        _APP = _create_app()
        config = uvicorn.Config(_APP, host=host, port=port,
                                log_level="warning", access_log=False)
        _SERVER = uvicorn.Server(config)

        def _run():
            try:
                _SERVER.run()  # 独立线程内自建事件循环
            except Exception as e:
                logger.error(f"API 服务器异常退出: {e}")

        _THREAD = threading.Thread(target=_run, name="api-server", daemon=True)
        _THREAD.start()
        logger.info(f"🌐 FastAPI 儀表板 http://{host}:{port} (docs: /docs)")
        return True
    except Exception as e:
        logger.warning(f"FastAPI 启动失败 (请 pip install fastapi uvicorn): {e}")
        return False


def stop_api_server():
    """停止服务器 (bot 退出时调用)"""
    global _SERVER
    if _SERVER:
        try:
            _SERVER.should_exit = True
        except Exception:
            pass
