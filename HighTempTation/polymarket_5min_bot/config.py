"""polymarket_5min_bot — 配置管理 (环境变量驱动, 与 HighTempTation Config 同风格)"""
import os
from typing import List


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").lower() == "true"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class FiveMinBotConfig:
    """5 分钟市场交易子模块配置。

    与 HighTempTation 共享: DRY_RUN / INITIAL_CAPITAL / MAX_DAILY_LOSS_PCT /
    MAX_CONCURRENT (通过 account_manager 与 shared_risk 读取主配置)。
    """

    def __init__(self):
        # === 运行模式 ===
        self.DRY_RUN = _env_bool("DRY_RUN", True)
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
        self.SCAN_INTERVAL_SEC = _env_int("PM5_SCAN_INTERVAL_SEC", 15)

        # === 市场 ===
        self.GAMMA_API = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
        self.CLOB_API = os.getenv("CLOB_API", "https://clob.polymarket.com")
        # 目标资产 (逗号分隔, 默认 BTC/ETH)
        self.TARGET_ASSETS = [a.strip().upper() for a in
                              os.getenv("PM5_TARGET_ASSETS", "BTC,ETH").split(",") if a.strip()]
        # 市场窗口分钟数 (默认 5 分钟 up/down 周期)
        self.MARKET_WINDOW_MIN = _env_int("PM5_MARKET_WINDOW_MIN", 5)
        # 模拟市场回退: 真实市场不可用时生成模拟市场
        self.SIM_MARKETS_ENABLED = _env_bool("PM5_SIM_MARKETS_ENABLED", True)
        # 每分钟最多多少个模拟周期 (DRY_RUN 演示用)
        self.SIM_CYCLES_PER_SCAN = _env_int("PM5_SIM_CYCLES_PER_SCAN", 1)
        # 市场搜索关键词 (Gamma text 搜索)
        self.SEARCH_TERMS = os.getenv(
            "PM5_SEARCH_TERMS",
            "btc up or down,eth up or down,bitcoin up or down,ethereum up or down,"
            "next 5 minutes,5 minute,up or down in the next").split(",")

        # === 套利 (互补套利 Buy1+Buy2) ===
        self.ARB_ENABLED = _env_bool("PM5_ARB_ENABLED", True)
        # Buy1 方向最小价格 (买高概率侧)
        self.ARB_MIN_BASE_PRICE = _env_float("PM5_ARB_MIN_BASE_PRICE", 0.55)
        # Buy2 互补侧价格上限: 组合成本 ≤ COMBINED_TARGET
        self.ARB_COMBINED_TARGET = _env_float("PM5_ARB_COMBINED_TARGET", 0.95)
        self.ARB_MAX_COMBINED = _env_float("PM5_ARB_MAX_COMBINED", 0.97)
        self.ARB_SIZE_USD = _env_float("PM5_ARB_SIZE_USD", 10.0)
        self.ARB_MIN_EDGE = _env_float("PM5_ARB_MIN_EDGE", 0.015)   # 组合回报 ≥ 1.5%
        # 配对单触发间隔 (秒, 防止 Buy1 后 Buy2 前价格跑掉)
        self.ARB_LEG2_MAX_WAIT = _env_int("PM5_ARB_LEG2_MAX_WAIT", 8)

        # === 狙击 (Endcycle Sniper) ===
        self.SNIPER_ENABLED = _env_bool("PM5_SNIPER_ENABLED", True)
        # 结算前多少秒进入狙击窗口
        self.SNIPER_WINDOW_SEC = _env_int("PM5_SNIPER_WINDOW_SEC", 45)
        # 买入价格阈值 (价格 ≥ 此值才狙击)
        self.SNIPER_PRICE_THRESH = _env_float("PM5_SNIPER_PRICE_THRESH", 0.95)
        self.SNIPER_SIZE_USD = _env_float("PM5_SNIPER_SIZE_USD", 10.0)
        # 拒绝在价格 < 此值时狙击 (保护: 结算前价格崩盘不接飞刀)
        self.SNIPER_MIN_PRICE = _env_float("PM5_SNIPER_MIN_PRICE", 0.80)

        # === 动量 (流动性动量 OBI) ===
        self.MOMENTUM_ENABLED = _env_bool("PM5_MOMENTUM_ENABLED", True)
        # OBI 阈值: 订单簿不均衡度 |OBI| ≥ 此值触发
        self.MOMENTUM_OBI_THRESH = _env_float("PM5_MOMENTUM_OBI_THRESH", 0.35)
        # 现货价相对行权价的偏离 (绝对比例) 需 ≥ 此值才确认
        self.MOMENTUM_PRICE_DEV = _env_float("PM5_MOMENTUM_PRICE_DEV", 0.0008)
        # 信号冷却 (同一市场两次入场间隔)
        self.MOMENTUM_COOLDOWN = _env_int("PM5_MOMENTUM_COOLDOWN", 45)
        self.MOMENTUM_SIZE_USD = _env_float("PM5_MOMENTUM_SIZE_USD", 10.0)

        # === 阶梯 (Ladder 做市 + Stair 出场) ===
        self.LADDER_ENABLED = _env_bool("PM5_LADDER_ENABLED", True)
        # 只在 YES+NO 组合价差 ≥ 此值 (即组合价值 > 1) 时双向挂单
        self.LADDER_MIN_SPREAD = _env_float("PM5_LADDER_MIN_SPREAD", 0.012)
        # 挂单深度 (每侧 $)
        self.LADDER_SIZE_USD = _env_float("PM5_LADDER_SIZE_USD", 5.0)
        # 距结算多少秒停止做市 (Ladder 风险控制)
        self.LADDER_STOP_BEFORE_END = _env_int("PM5_LADDER_STOP_BEFORE_END", 20)
        self.STAIR_ENABLED = _env_bool("PM5_STAIR_ENABLED", True)
        # Stair 分批次数
        self.STAIR_STEPS = _env_int("PM5_STAIR_STEPS", 3)
        # 每批退出价格梯度 (占盘口最优价的偏移比例)
        self.STAIR_STEP_OFFSET = _env_float("PM5_STAIR_STEP_OFFSET", 0.002)

        # === 风险 (主配置在 shared_risk.py, 这里是子模块自身兜底) ===
        self.MAX_POS_PER_MARKET = _env_int("PM5_MAX_POS_PER_MARKET", 2)
        self.MAX_TOTAL_POSITIONS = _env_int("PM5_MAX_TOTAL_POSITIONS", 20)

    def summarize(self) -> List[str]:
        lines = [
            f"🎯 5min 子模块: DRY_RUN={self.DRY_RUN} 扫描={self.SCAN_INTERVAL_SEC}s "
            f"资产={self.TARGET_ASSETS} 窗口={self.MARKET_WINDOW_MIN}min",
            f"   🧲 套利={'ON' if self.ARB_ENABLED else 'OFF'} "
            f"(组合目标 ${self.ARB_COMBINED_TARGET:.2f}, 最小edge {self.ARB_MIN_EDGE:.1%}, "
            f"每笔 ${self.ARB_SIZE_USD:.0f})",
            f"   🎯 狙击={'ON' if self.SNIPER_ENABLED else 'OFF'} "
            f"(窗口 {self.SNIPER_WINDOW_SEC}s, 阈值 ≥{self.SNIPER_PRICE_THRESH:.2f}, "
            f"每笔 ${self.SNIPER_SIZE_USD:.0f})",
            f"   ⚡ 动量={'ON' if self.MOMENTUM_ENABLED else 'OFF'} "
            f"(OBI ≥{self.MOMENTUM_OBI_THRESH:.2f}, 价差 ≥{self.MOMENTUM_PRICE_DEV:.3%}, "
            f"每笔 ${self.MOMENTUM_SIZE_USD:.0f})",
            f"   🪜 阶梯={'ON' if self.LADDER_ENABLED else 'OFF'} "
            f"(价差 ≥{self.LADDER_MIN_SPREAD:.1%}, 每侧 ${self.LADDER_SIZE_USD:.0f}) | "
            f"Stair={'ON' if self.STAIR_ENABLED else 'OFF'} ({self.STAIR_STEPS}批)",
        ]
        return lines
