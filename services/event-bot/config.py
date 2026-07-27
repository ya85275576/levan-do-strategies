"""
OKX 事件合約自動交易機器人 — 配置管理器

從環境變數加載所有配置。
"""
import os


def _env_bool(key: str, default: str = "false") -> bool:
    return os.environ.get(key, default).strip().lower() == "true"


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def get_config():
    """
    獲取完整配置字典。
    配置來源優先級：環境變數 > 預設值
    """
    config = {
        # ---- 事件合約系列（逗號分隔） ----
        "trading_series": [
            s.strip() for s in os.environ.get(
                "EVENT_TRADING_SERIES",
                "BTC-UPDOWN-5MIN,ETH-UPDOWN-5MIN,SOL-UPDOWN-5MIN"
            ).split(",") if s.strip()
        ],

        # ---- 模擬模式 ----
        "dry_run": _env_bool("EVENT_DRY_RUN", "true"),

        # ---- 交易金額（帳戶餘額百分比） ----
        "trade_qty_pct": _env_float("EVENT_TRADE_QTY_PCT", 10.0),
        # 每筆最大合約數（1 合約 = 1 USDT）
        "max_position_size": _env_int("EVENT_MAX_POSITION_SIZE", 100),
        # 最小合約數
        "min_position_size": _env_int("EVENT_MIN_POSITION_SIZE", 10),

        # ---- 初始資金 ----
        "initial_capital": _env_float("EVENT_INITIAL_CAPITAL", 500.0),

        # ---- 風控 ----
        # 每日虧損上限（USDT），達到後暫停交易
        "daily_loss_limit": _env_float("EVENT_DAILY_LOSS_LIMIT", 50.0),
        # 連續虧損次數上限
        "consecutive_loss_limit": _env_int("EVENT_CONSECUTIVE_LOSS_LIMIT", 5),
        # 最大同時持有合約數
        "max_concurrent_positions": _env_int("EVENT_MAX_CONCURRENT", 3),

        # ---- 策略參數 ----
        # 動量比較的 K 線數量（最近的 N 根 1 分鐘 K 線）
        "momentum_lookback": _env_int("EVENT_MOMENTUM_LOOKBACK", 3),
        # 價格變動閾值（百分比），低於此值視為橫盤不交易
        "momentum_threshold_pct": _env_float("EVENT_MOMENTUM_THRESHOLD_PCT", 0.05),
        # 最大願意支付的價格（超過此價格不買，0.001-0.999）
        "max_buy_price": _env_float("EVENT_MAX_BUY_PRICE", 0.7),

        # ---- 輪詢間隔（秒） ----
        "poll_interval": _env_int("EVENT_POLL_INTERVAL", 30),

        # ---- 日誌 ----
        "log_level": os.environ.get("EVENT_LOG_LEVEL", "INFO").upper(),

        # ---- OKX CLI Profile ----
        "okx_profile": os.environ.get("OKX_PROFILE", "demo"),

        # ---- 狀態檔案（供儀表板讀取） ----
        "status_file": os.environ.get(
            "EVENT_STATUS_FILE",
            "/tmp/event-bot-status.json",
        ),
        "closed_trades_file": os.environ.get(
            "EVENT_CLOSED_TRADES_FILE",
            "/tmp/event-bot-closed.json",
        ),
    }

    if config["log_level"] not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        config["log_level"] = "INFO"

    return config


# 單例配置
_config = None


def load_config():
    global _config
    if _config is None:
        _config = get_config()
    return _config
