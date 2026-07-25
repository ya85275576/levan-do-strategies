"""
LE VAN DO® OKX 原生交易机器人 — 配置管理器

从环境变量加载所有配置，与现有 services/config/index.js 保持一致的配置来源。
支持多交易对（逗号分隔的 TRADING_SYMBOLS 环境变量）。
"""
import os
import json
from pathlib import Path


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


def _parse_symbols(raw: str) -> list:
    """解析逗号分隔的交易对列表，去空白"""
    return [s.strip() for s in raw.split(",") if s.strip()]


def get_config():
    """
    获取完整配置字典。
    配置来源优先级：环境变量 > 默认值
    """
    network = os.environ.get("EXCHANGE_NETWORK", "testnet").strip().lower()
    if network not in ("production", "live"):
        network = "testnet"
    is_testnet = network == "testnet"

    # OKX WebSocket & REST API base URLs
    ws_urls = {
        "testnet": "wss://wspap.okx.com:8443/ws/v5/public",
        "production": "wss://ws.okx.com:8443/ws/v5/public",
    }
    rest_urls = {
        "testnet": "https://www.okx.com",
        "production": "https://www.okx.com",
    }

    # ---- 解析交易对列表 ----
    default_symbols = "BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,DOGE-USDT,ADA-USDT,AVAX-USDT,DOT-USDT,LINK-USDT,MATIC-USDT,UNI-USDT,SHIB-USDT,LTC-USDT,BCH-USDT,ATOM-USDT,ETC-USDT,XLM-USDT,TRX-USDT,FIL-USDT,APT-USDT,ARB-USDT,OP-USDT,SUI-USDT,PEPE-USDT,INJ-USDT,TIA-USDT,SEI-USDT,RUNE-USDT,FET-USDT,GRT-USDT,NEAR-USDT,ICP-USDT,RENDER-USDT,IMX-USDT,MKR-USDT,AAVE-USDT,CRV-USDT,SNX-USDT,COMP-USDT,EOS-USDT,ALGO-USDT,FLOW-USDT,SAND-USDT,MANA-USDT,AXS-USDT,THETA-USDT,FTM-USDT,CVX-USDT,1INCH-USDT,STX-USDT"
    raw_symbols = os.environ.get("TRADING_SYMBOLS", default_symbols)
    symbols = _parse_symbols(raw_symbols)
    if not symbols:
        symbols = ["BTC-USDT"]

    config = {
        # ---- 交易所 ----
        "exchange": "OKX",
        "network": network,
        "is_testnet": is_testnet,

        # ---- API 端点 ----
        "ws_url": ws_urls.get(network, ws_urls["testnet"]),
        "rest_url": rest_urls.get(network, rest_urls["testnet"]),

        # ---- OKX API 凭据 ----
        "api_key": os.environ.get("OKX_API_KEY", ""),
        "api_secret": os.environ.get("OKX_API_SECRET", ""),
        "api_passphrase": os.environ.get("OKX_API_PASSPHRASE", ""),

        # ---- 模拟模式 ----
        "dry_run": _env_bool("DRY_RUN", "true"),

        # ---- 策略参数（与 Pine Script 保持一致） ----
        # 交易模式
        "tps_type": os.environ.get("TPS_TYPE", "Trailing"),  # ATR | Trailing | Options
        "setup_type": os.environ.get("SETUP_TYPE", "Open/Close"),  # Open/Close | Renko

        # 基础时间框架（分钟）
        "base_timeframe_min": _env_int("BASE_TIMEFRAME_MIN", 15),
        # 高时间框架倍数 (tfmult=18)
        "tf_mult": _env_int("TF_MULT", 18),

        # ---- Sideways 过滤器 ----
        "sideways_filter": os.environ.get(
            "SIDEWAYS_FILTER",
            "No Filtering"
        ),

        # RSI 参数
        "rsi_length": _env_int("RSI_LENGTH", 7),
        "rsi_top_limit": _env_int("RSI_TOP_LIMIT", 45),
        "rsi_bot_limit": _env_int("RSI_BOT_LIMIT", 10),

        # ATR 过滤参数
        "atr_filter_len": _env_int("ATR_FILTER_LEN", 5),
        "atr_ma_len": _env_int("ATR_MA_LEN", 5),

        # Renko 参数
        "renko_atr_len": _env_int("RENKO_ATR_LEN", 3),
        "renko_ema1_length": _env_int("RENKO_EMA1_LENGTH", 2),
        "renko_ema2_length": _env_int("RENKO_EMA2_LENGTH", 10),

        # 风险管理
        "atr_length": _env_int("ATR_LENGTH", 20),
        "profit_factor": _env_float("PROFIT_FACTOR", 2.5),
        "stop_factor": _env_float("STOP_FACTOR", 1.0),

        # 三级止盈百分比
        "tp1_qty_pct": _env_float("TP1_QTY_PCT", 50.0),
        "tp2_qty_pct": _env_float("TP2_QTY_PCT", 30.0),
        "tp3_qty_pct": _env_float("TP3_QTY_PCT", 20.0),

        # 默认杠杆
        "default_leverage": _env_int("DEFAULT_LEVERAGE", 1),
        "position_mode": os.environ.get("POSITION_MODE", "isolated"),

        # 交易对列表（多交易对）
        "symbols": symbols,
        # 第一个交易对作为默认（向后兼容）
        "symbol": symbols[0] if symbols else "BTC-USDT",

        # ---- 交易数量（根据余额百分比） ----
        "trade_qty_pct": _env_float("TRADE_QTY_PCT", 50.0),

        # ---- 模拟初始资金（每个交易对分配） ----
        "initial_capital": _env_float("INITIAL_CAPITAL", 5000.0),

        # ---- PM2 / 日志 ----
        "log_level": os.environ.get("LOG_LEVEL", "INFO").upper(),

        # ---- Webhook 回调（可选） ----
        "webhook_url": os.environ.get("BOT_WEBHOOK_URL", ""),
    }

    # 有效性检查
    if config["log_level"] not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        config["log_level"] = "INFO"

    return config


# 单例配置
_config = None


def load_config():
    global _config
    if _config is None:
        _config = get_config()
    return _config
