"""
Polymarket 互补套利机器人 — 配置管理器

从环境变量加载所有配置。
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


def get_config() -> dict:
    """
    获取完整配置字典。
    配置来源优先级：环境变量 > 默认值
    """
    return {
        # ---- 运行模式 ----
        "dry_run": _env_bool("DRY_RUN", "true"),

        # ---- Polymarket CLOB API ----
        "clob_api_url": os.environ.get(
            "POLYMARKET_CLOB_API",
            "https://clob.polymarket.com",
        ).rstrip("/"),

        # ---- 套利参数 ----
        # 当 YES+NO < threshold 时才触发（例如 0.98 表示至少 2% 折价）
        "arbitrage_threshold": _env_float("ARBITRAGE_THRESHOLD", 0.98),
        # 每次套利模拟买入数量（美元面值）
        "trade_size": _env_float("TRADE_SIZE", 100.0),

        # ---- 扫描参数 ----
        # 两次扫描之间的间隔秒数
        "scan_interval_sec": _env_int("SCAN_INTERVAL_SEC", 60),
        # 扫描时最多获取多少页（每页 100 个市场）
        "max_pages": _env_int("MAX_PAGES", 5),
        # 信任的做市商地址（仅当交易对手是这些地址时才考虑，逗号分隔）
        # 留空表示信任所有做市商
        "trusted_makers": os.environ.get("TRUSTED_MAKERS", ""),

        # ---- 做市商过滤 ----
        # 仅扫描收盘价在 [min_price, max_price] 范围内的机会
        # 避免极端低流动性的深价外期权
        "min_yes_price": _env_float("MIN_YES_PRICE", 0.02),
        "max_yes_price": _env_float("MAX_YES_PRICE", 0.98),
        "min_no_price": _env_float("MIN_NO_PRICE", 0.02),
        "max_no_price": _env_float("MAX_NO_PRICE", 0.98),

        # ---- 最低流动性过滤（USDC） ----
        # 当任意一边的盘口深度低于此值时跳过
        "min_liquidity_usdc": _env_float("MIN_LIQUIDITY_USDC", 100.0),

        # ---- 通知 ----
        # Slack / Discord Webhook URL（留空则不发送）
        "webhook_url": os.environ.get("ARBITRAGE_WEBHOOK_URL", ""),

        # ---- 日志 ----
        "log_level": os.environ.get("LOG_LEVEL", "INFO").upper(),

        # ---- 状态文件 ----
        "state_file": os.environ.get(
            "STATE_FILE",
            "/tmp/polymarket-arbitrage-state.json",
        ),
        # 机会记录文件
        "opportunities_file": os.environ.get(
            "OPPORTUNITIES_FILE",
            "/tmp/polymarket-arbitrage-opportunities.json",
        ),

        # ---- Polygon RPC（仅查询代币信息用，非必需） ----
        "polygon_rpc_url": os.environ.get(
            "POLYGON_RPC_URL",
            "",
        ),
    }


# 单例配置
_config = None


def load_config():
    global _config
    if _config is None:
        _config = get_config()
    return _config
