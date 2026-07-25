"""
MT5 交易橋接模組 — Python MetaTrader5 封裝

功能：
  - 初始化 MT5 終端連線（帳戶號、密碼、伺服器）
  - 帳戶資訊查詢（餘額、權益、持倉）
  - 市價單下單（買/賣、止盈止損）
  - 平倉功能
  - 訂單查詢

平台相容性：
  - Windows: 完整功能，需安裝 MT5 終端並啟用自動交易
  - Linux/macOS: 僅支援模擬模式（DRY_RUN=true），不實際連接 MT5

注意：
  MetaTrader5 套件僅支援 Windows（依賴 MT5 終端 DLL）。
  在非 Windows 平台，匯入套件會失敗，模組自動降級為模擬模式。
"""

import os
import json
import sys
import time
from datetime import datetime
from decimal import Decimal

# ---- 防護性導入：僅在 Windows 且 MT5 可用時加載 ----
MT5_AVAILABLE = False
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    pass


# ════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def is_dry_run() -> bool:
    """是否為模擬模式"""
    return _get_env("DRY_RUN", "false").lower() == "true"


def get_mt5_config() -> dict:
    """取得 MT5 連線配置（來自環境變數）"""
    return {
        "account": int(_get_env("MT5_ACCOUNT", "0")),
        "password": _get_env("MT5_PASSWORD", ""),
        "server": _get_env("MT5_SERVER", "ICMarkets-Demo"),
        "timeout_sec": int(_get_env("MT5_TIMEOUT_SEC", "60")),
        "path": _get_env("MT5_PATH", ""),  # MT5 終端 exe 路徑（可選）
    }


# ════════════════════════════════════════════════════════════════
# 連線管理
# ════════════════════════════════════════════════════════════════

_initialized = False


def initialize() -> dict:
    """
    初始化 MT5 連線。

    傳回值：
      {"success": true, "message": "..."}
      或 {"success": false, "error": "..."}
    """
    global _initialized

    if _initialized:
        return {"success": True, "message": "MT5 已連線（重複初始化）"}

    if is_dry_run():
        _initialized = True
        msg = f"[MT5 模擬] 初始化成功（DRY_RUN 模式，未實際連接）"
        print(msg, file=sys.stderr)
        return {"success": True, "message": msg}

    if not MT5_AVAILABLE:
        return {
            "success": False,
            "error": "MetaTrader5 套件不可用。僅支援 Windows + MT5 終端環境。請設定 DRY_RUN=true 啟用模擬模式。",
        }

    cfg = get_mt5_config()

    if not cfg["account"] or not cfg["password"]:
        return {
            "success": False,
            "error": "MT5 帳戶憑證未配置（需 MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER）",
        }

    # 初始化 MT5 終端
    kwargs = {}
    if cfg["path"]:
        kwargs["path"] = cfg["path"]

    result = mt5.initialize(**kwargs)
    if not result:
        error_msg = f"MT5 初始化失敗: {mt5.last_error()}"
        print(error_msg, file=sys.stderr)
        return {"success": False, "error": error_msg}

    # 登入帳戶
    authorized = mt5.login(
        login=cfg["account"],
        password=cfg["password"],
        server=cfg["server"],
    )
    if not authorized:
        err = mt5.last_error()
        error_msg = f"MT5 登入失敗 (帳戶: {cfg['account']}, 伺服器: {cfg['server']}): {err}"
        print(error_msg, file=sys.stderr)
        mt5.shutdown()
        return {"success": False, "error": error_msg}

    _initialized = True
    print(f"[MT5] ✅ 已連線: 帳戶 {cfg['account']} @ {cfg['server']}", file=sys.stderr)
    return {"success": True, "message": f"MT5 連線成功: {cfg['account']}"}


def shutdown() -> dict:
    """關閉 MT5 連線"""
    global _initialized

    if not _initialized:
        return {"success": True, "message": "MT5 未初始化，無需關閉"}

    if is_dry_run():
        _initialized = False
        return {"success": True, "message": "[MT5 模擬] 連線已關閉"}

    if MT5_AVAILABLE:
        mt5.shutdown()

    _initialized = False
    return {"success": True, "message": "MT5 連線已關閉"}


# ════════════════════════════════════════════════════════════════
# 帳戶資訊
# ════════════════════════════════════════════════════════════════

def get_account_info() -> dict:
    """查詢帳戶資訊（餘額、權益、盈虧等）"""
    if not _ensure_initialized():
        return _error("MT5 未初始化")

    if is_dry_run():
        return {
            "success": True,
            "data": {
                "login": 0,
                "balance": 100000.0,
                "equity": 100000.0,
                "profit": 0.0,
                "margin": 0.0,
                "margin_free": 100000.0,
                "margin_level": 0.0,
                "name": "Simulated Account",
                "server": "Simulated",
                "currency": "USD",
                "leverage": 100,
                "trade_allowed": True,
                "_mode": "simulated",
            },
        }

    if not MT5_AVAILABLE:
        return _error("MetaTrader5 套件不可用")

    info = mt5.account_info()
    if info is None:
        return _error(f"無法取得帳戶資訊: {mt5.last_error()}")

    return {
        "success": True,
        "data": _serialize_account_info(info),
    }


def _serialize_account_info(info) -> dict:
    """將 MT5 AccountInfo 物件序列化為 dict"""
    return {
        "login": info.login,
        "balance": info.balance,
        "equity": info.equity,
        "profit": info.profit,
        "margin": info.margin,
        "margin_free": info.margin_free,
        "margin_level": info.margin_level,
        "name": info.name,
        "server": info.server,
        "currency": info.currency,
        "leverage": info.leverage,
        "trade_allowed": info.trade_allowed,
        "_mode": "live",
    }


# ════════════════════════════════════════════════════════════════
# 持倉查詢
# ════════════════════════════════════════════════════════════════

def get_positions(symbol: str = None) -> dict:
    """
    查詢當前持倉。

    Args:
        symbol: 交易品種（如 "BTCUSD"），None 表示全部

    Returns:
        {"success": true, "data": [position, ...]}
    """
    if not _ensure_initialized():
        return _error("MT5 未初始化")

    if is_dry_run():
        return _simulated_get_positions(symbol)

    if not MT5_AVAILABLE:
        return _error("MetaTrader5 套件不可用")

    if symbol:
        positions = mt5.positions_get(symbol=symbol)
    else:
        positions = mt5.positions_get()

    if positions is None:
        return {
            "success": True,
            "data": [],
            "message": "無持倉或查詢失敗",
        }

    return {
        "success": True,
        "data": [_serialize_position(p) for p in positions],
    }


def _serialize_position(pos) -> dict:
    """將 MT5 Position 物件序列化為 dict"""
    return {
        "ticket": pos.ticket,
        "symbol": pos.symbol,
        "type": "buy" if pos.type == 0 else "sell",
        "volume": pos.volume,
        "price_open": pos.price_open,
        "sl": pos.sl,
        "tp": pos.tp,
        "profit": pos.profit,
        "swap": pos.swap,
        "comment": pos.comment,
        "magic": pos.magic,
        "time": str(pos.time),
    }


# ════════════════════════════════════════════════════════════════
# 下單
# ════════════════════════════════════════════════════════════════

# 模擬持倉和訂單記錄
_simulated_positions: dict = {}
_simulated_orders: list = []
_order_counter: int = 0


def place_order(params: dict) -> dict:
    """
    執行市價單。

    參數（與 Node.js exchange API 保持一致）：
      - symbol: 交易品種（如 "BTCUSD"）
      - side: "buy" | "sell"
      - qty: 數量（手數，如 0.01）
      - order_type: "market" | "limit" (預設 "market")
      - price: 限價單價格（可選）
      - sl: 止損價（可選）
      - tp: 止盈價（可選）
      - comment: 訂單註釋（可選）

    Returns:
      {"success": true, "data": {"order": ..., "ticket": ...}}
    """
    global _order_counter

    if not _ensure_initialized():
        return _error("MT5 未初始化")

    symbol = params.get("symbol", "").upper()
    side = params.get("side", "").lower()
    qty = float(params.get("qty", 0))
    order_type = params.get("order_type", "market").lower()
    price = float(params["price"]) if params.get("price") else None
    sl = float(params["sl"]) if params.get("sl") else 0.0
    tp = float(params["tp"]) if params.get("tp") else 0.0
    comment = params.get("comment", "")

    # ── 參數驗證 ──
    if not symbol:
        return _error("缺少必填參數: symbol")
    if side not in ("buy", "sell"):
        return _error(f"無效方向: {side}，須為 buy 或 sell")
    if qty <= 0:
        return _error(f"無效數量: {qty}，須大於 0")
    if order_type == "limit" and not price:
        return _error("限價單必須指定 price")

    # ── 模擬模式 ──
    if is_dry_run():
        _order_counter += 1
        ticket = _order_counter
        order_record = {
            "ticket": ticket,
            "symbol": symbol,
            "type": side,
            "volume": qty,
            "price": price or 0.0,
            "sl": sl,
            "tp": tp,
            "order_type": order_type,
            "time": datetime.utcnow().isoformat(),
            "comment": comment,
            "_mode": "simulated",
        }
        _simulated_orders.append(order_record)

        # 更新模擬持倉
        pos_key = symbol
        current_qty = _simulated_positions.get(pos_key, 0.0)
        if side == "buy":
            _simulated_positions[pos_key] = current_qty + qty
        else:
            _simulated_positions[pos_key] = current_qty - qty

        # 若持倉歸零則移除
        if abs(_simulated_positions[pos_key]) < 1e-10:
            _simulated_positions.pop(pos_key, None)

        msg = f"[MT5 模擬] {side.upper()} {qty} {symbol} @ {'市價' if order_type == 'market' else f'限價 {price}'}"
        print(msg, file=sys.stderr)

        return {
            "success": True,
            "data": {
                "ticket": ticket,
                "order": order_record,
                "message": f"模擬下單成功 (ticket #{ticket})",
            },
        }

    # ── 實盤模式 ──
    if not MT5_AVAILABLE:
        return _error("MetaTrader5 套件不可用，無法實盤下單")

    # 建立交易請求
    mt5_side = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    mt5_order_type = mt5.ORDER_TYPE_MARKET if order_type == "market" else mt5.ORDER_TYPE_LIMIT

    request = {
        "action": mt5.TRADE_ACTION_DEAL if order_type == "market" else mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": qty,
        "type": mt5_side,
        "price": 0.0,  # 市價單由 MT5 填寫
        "sl": sl,
        "tp": tp,
        "deviation": 20,  # 允許點差 slippage
        "magic": 234000,
        "comment": comment or "LE VAN DO",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    if order_type == "limit" and price:
        request["price"] = price

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        error_msg = f"MT5 下單失敗 (retcode={result.retcode}): {result.comment}"
        print(error_msg, file=sys.stderr)
        return {"success": False, "error": error_msg}

    print(f"[MT5] ✅ 下單成功: {side.upper()} {qty} {symbol} (ticket #{result.order})", file=sys.stderr)

    return {
        "success": True,
        "data": {
            "ticket": result.order,
            "price": result.price,
            "volume": result.volume,
            "comment": result.comment,
        },
    }


# ════════════════════════════════════════════════════════════════
# 平倉
# ════════════════════════════════════════════════════════════════

def close_position(symbol: str = None, ticket: int = None) -> dict:
    """
    平倉。

    Args:
        symbol: 交易品種，None 表示平所有持倉
        ticket: 指定訂單號平倉（可選）

    Returns:
        {"success": true, "data": {...}}
    """
    if not _ensure_initialized():
        return _error("MT5 未初始化")

    if is_dry_run():
        return _simulated_close_position(symbol, ticket)

    if not MT5_AVAILABLE:
        return _error("MetaTrader5 套件不可用")

    # 取得待平倉持倉清單
    if ticket:
        positions = mt5.positions_get(ticket=ticket)
    elif symbol:
        positions = mt5.positions_get(symbol=symbol)
    else:
        positions = mt5.positions_get()

    if not positions or len(positions) == 0:
        return {
            "success": True,
            "data": [],
            "message": "無持倉需要平倉",
        }

    closed = []
    for pos in positions:
        close_side = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_side,
            "position": pos.ticket,
            "price": 0.0,
            "deviation": 20,
            "magic": 234000,
            "comment": "Close by LE VAN DO",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        closed.append({
            "ticket": pos.ticket,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "success": result.retcode == mt5.TRADE_RETCODE_DONE,
            "retcode": result.retcode,
            "comment": result.comment,
        })

    success_count = sum(1 for c in closed if c["success"])
    print(f"[MT5] ✅ 平倉完成: {success_count}/{len(closed)} 成功", file=sys.stderr)

    return {
        "success": True,
        "data": closed,
        "message": f"平倉完成: {success_count}/{len(closed)} 成功",
    }


def _simulated_get_positions(symbol: str = None) -> dict:
    """模擬持倉查詢"""
    if symbol:
        pos_key = symbol.upper()
        qty = _simulated_positions.get(pos_key, 0.0)
        if abs(qty) < 1e-10:
            return {"success": True, "data": [], "message": f"[模擬] {pos_key} 無持倉"}
        return {
            "success": True,
            "data": [{
                "ticket": 0,
                "symbol": pos_key,
                "type": "buy" if qty > 0 else "sell",
                "volume": abs(qty),
                "price_open": 0.0,
                "sl": 0.0,
                "tp": 0.0,
                "profit": 0.0,
                "swap": 0.0,
                "comment": "simulated",
                "magic": 234000,
                "time": datetime.utcnow().isoformat(),
            }],
        }

    # 返回所有持倉
    all_positions = []
    for sym, qty in _simulated_positions.items():
        if abs(qty) < 1e-10:
            continue
        all_positions.append({
            "ticket": 0,
            "symbol": sym,
            "type": "buy" if qty > 0 else "sell",
            "volume": abs(qty),
            "price_open": 0.0,
            "sl": 0.0,
            "tp": 0.0,
            "profit": 0.0,
            "swap": 0.0,
            "comment": "simulated",
            "magic": 234000,
            "time": datetime.utcnow().isoformat(),
        })

    return {"success": True, "data": all_positions}


def _simulated_close_position(symbol: str = None, ticket: int = None) -> dict:
    """模擬平倉"""
    global _simulated_positions

    if symbol:
        pos_key = symbol.upper()
        qty = _simulated_positions.get(pos_key, 0.0)
        if qty == 0:
            return {"success": True, "data": [], "message": f"[模擬] {pos_key} 無持倉"}
        side = "long" if qty > 0 else "short"
        print(f"[MT5 模擬] 平倉: {pos_key} ({side}) {abs(qty)}", file=sys.stderr)
        _simulated_positions.pop(pos_key, None)
        return {
            "success": True,
            "data": [{"ticket": 0, "symbol": pos_key, "volume": abs(qty), "success": True}],
            "message": f"[模擬] 平倉成功: {pos_key}",
        }

    # 平所有
    total = len(_simulated_positions)
    keys = list(_simulated_positions.keys())
    _simulated_positions.clear()
    print(f"[MT5 模擬] 平所有持倉: {total} 個品種", file=sys.stderr)
    return {
        "success": True,
        "data": [{"ticket": 0, "symbol": k, "volume": 0, "success": True} for k in keys],
        "message": f"[模擬] 全部平倉完成: {total} 個持倉",
    }


# ════════════════════════════════════════════════════════════════
# 訂單查詢
# ════════════════════════════════════════════════════════════════

def get_orders(symbol: str = None, ticket: int = None) -> dict:
    """
    查詢歷史訂單。

    Args:
        symbol: 交易品種（可選）
        ticket: 訂單號（可選）

    Returns:
        {"success": true, "data": [order, ...]}
    """
    if not _ensure_initialized():
        return _error("MT5 未初始化")

    if is_dry_run():
        return {
            "success": True,
            "data": _simulated_orders,
            "message": f"[模擬] {len(_simulated_orders)} 筆記錄",
        }

    if not MT5_AVAILABLE:
        return _error("MetaTrader5 套件不可用")

    # 取得近期歷史訂單（最近 100 筆）
    from datetime import timedelta
    now = datetime.now()
    from_date = now - timedelta(days=7)

    if ticket:
        order = mt5.history_order_get(ticket=ticket)
        if not order:
            return {"success": True, "data": [], "message": f"未找到訂單 #{ticket}"}
        return {"success": True, "data": [_serialize_order(order)]}

    orders = mt5.history_orders_get(from_date, now, group=symbol or "*")
    if not orders:
        return {"success": True, "data": []}

    return {
        "success": True,
        "data": [_serialize_order(o) for o in orders],
    }


def _serialize_order(order) -> dict:
    """將 MT5 TradeOrder 物件序列化為 dict"""
    return {
        "ticket": order.ticket,
        "symbol": order.symbol,
        "type": "buy" if order.type in (0, 2) else "sell",
        "volume": order.volume,
        "price_open": order.price_open,
        "sl": order.sl,
        "tp": order.tp,
        "profit": getattr(order, "profit", 0),
        "comment": order.comment,
        "magic": order.magic,
        "time_setup": str(order.time_setup),
        "time_done": str(order.time_done),
    }


# ════════════════════════════════════════════════════════════════
# 模擬工具
# ════════════════════════════════════════════════════════════════

def get_simulated_orders() -> list:
    """取得模擬訂單記錄"""
    return list(_simulated_orders)


def get_simulated_positions() -> dict:
    """取得模擬持倉快照"""
    return dict(_simulated_positions)


def reset_simulation() -> dict:
    """重置模擬狀態"""
    global _simulated_positions, _simulated_orders, _order_counter
    _simulated_positions.clear()
    _simulated_orders.clear()
    _order_counter = 0
    return {"success": True, "message": "模擬狀態已重置"}


# ════════════════════════════════════════════════════════════════
# 共用工具
# ════════════════════════════════════════════════════════════════

def _error(msg: str) -> dict:
    return {"success": False, "error": msg}


def _ensure_initialized() -> bool:
    """確保已初始化（模擬模式自動初始化）"""
    global _initialized
    if not _initialized and is_dry_run():
        _initialized = True
    return _initialized


def ping() -> dict:
    """連線狀態檢查"""
    return {
        "success": True,
        "data": {
            "initialized": _initialized,
            "dry_run": is_dry_run(),
            "mt5_available": MT5_AVAILABLE,
            "python_version": sys.version,
        },
    }
