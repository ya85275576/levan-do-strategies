#!/usr/bin/env python3
"""
HighTempTation — 共享风控规则 (跨 Bot 统一)
============================================

把"日亏损上限 + 总仓位限制"从天气 Bot 与 5min Bot 中抽离为
单一权威 (SharedRiskGate 单例), 两个 Bot 都向它上报仓位与盈亏,
任何一侧超限都同时熔断两侧。

与 portfolio_risk.py (PortfolioRiskManager) 的关系:
  - PortfolioRiskManager: 天气信号的事件级风控 (同事件/相邻桶/相关性熔断),
    继续保留, 只服务天气 Bot
  - SharedRiskGate: 账户级共享风控 (日亏损/总仓位/信号质量熔断),
    两个 Bot 共用

规则 (环境变量, 与 bot.py Config 同源):
  INITIAL_CAPITAL       初始资金 (默认 10000)
  MAX_DAILY_LOSS_PCT    日亏损上限 (默认 5%)
  MAX_CONCURRENT        总持仓上限 (天气 + 5min 合计, 默认 30)
  SHARED_RISK_ENABLED   总开关 (默认 true)
  SHARED_RISK_CIRCUIT_COOLDOWN  信号质量熔断冷却秒 (默认 1800)
"""
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("shared_risk")

_INSTANCE: Optional["SharedRiskGate"] = None
_INSTANCE_LOCK = threading.Lock()


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


class SharedRiskGate:
    """账户级共享风控闸门 (单例)"""

    def __init__(self):
        self.enabled = os.getenv("SHARED_RISK_ENABLED", "true").lower() == "true"
        self.initial_capital = _env_float("INITIAL_CAPITAL", 10000.0)
        self.max_daily_loss_pct = _env_float("MAX_DAILY_LOSS_PCT", 0.05)
        self.max_concurrent = _env_int("MAX_CONCURRENT", 30)
        self.circuit_cooldown = _env_int("SHARED_RISK_CIRCUIT_COOLDOWN", 1800)

        # 状态
        self._daily_pnl = 0.0
        self._day = self._today()
        self._open_count = 0                    # 当前总持仓 (两 bot 合计)
        self._circuit_until = 0.0               # 信号质量熔断截止
        self._last_circuit_reason = ""
        self._consecutive_losses = 0
        self.n_rejected = 0
        self.n_allowed = 0
        self.events: list = []

    @staticmethod
    def _today() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).date().isoformat()

    # ── 单例 ──
    @classmethod
    def instance(cls) -> "SharedRiskGate":
        global _INSTANCE
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = cls()
            return _INSTANCE

    # ── 日亏损 ──
    def _roll_day(self):
        today = self._today()
        if today != self._day:
            self._day = today
            self._daily_pnl = 0.0

    def report_open(self, delta: int = 1):
        """上报持仓数变化 (两 bot 开平仓都调用)"""
        self._open_count = max(0, self._open_count + delta)

    def report_pnl(self, pnl: float):
        """上报已实现盈亏 (两 bot 结算都调用)"""
        self._roll_day()
        self._daily_pnl += pnl
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
        # 连亏 ≥5 笔 → 信号质量熔断
        if self._consecutive_losses >= 5 and self.enabled:
            self._circuit_until = time.time() + self.circuit_cooldown
            self._last_circuit_reason = f"连续 {self._consecutive_losses} 笔亏损, 熔断 {self.circuit_cooldown}s"
            self.events.append({
                "ts": self._today(), "type": "circuit_breaker",
                "reason": self._last_circuit_reason,
                "until": self._circuit_until,
            })
            logger.warning(f"⛔ 共享风控熔断: {self._last_circuit_reason}")
            self._consecutive_losses = 0

    @property
    def daily_loss_limit(self) -> float:
        return self.initial_capital * self.max_daily_loss_pct

    def daily_loss_breaked(self) -> bool:
        self._roll_day()
        return self._daily_pnl <= -self.daily_loss_limit

    # ── 闸门 ──
    def check(self, strategy: str = "", amount_usd: float = 0.0,
              signal_quality: Optional[dict] = None) -> Optional[str]:
        """
        开仓前统一风控检查。

        Args:
            strategy: 策略名 (WEATHER / 5min-ARB / ...)
            amount_usd: 本次拟投入金额
            signal_quality: 可选信号质量 {winrate, edge} 等

        Returns:
            None = 放行; str = 拒绝原因
        """
        if not self.enabled:
            return None
        self._roll_day()

        # 1. 日亏损熔断
        if self._daily_pnl <= -self.daily_loss_limit:
            self.n_rejected += 1
            return (f"日亏损熔断: 今日 {self._daily_pnl:+.2f} ≤ "
                    f"-{self.daily_loss_limit:.0f} (MAX_DAILY_LOSS_PCT={self.max_daily_loss_pct:.0%})")

        # 2. 总仓位限制
        if self._open_count >= self.max_concurrent:
            self.n_rejected += 1
            return f"总持仓超限 {self._open_count}/{self.max_concurrent}"

        # 3. 信号质量熔断
        if time.time() < self._circuit_until:
            self.n_rejected += 1
            return f"信号质量熔断冷却中 ({self._last_circuit_reason})"

        # 4. 单笔金额上限 (不超过资金 5%)
        if amount_usd > self.initial_capital * 0.05:
            self.n_rejected += 1
            return f"单笔金额超限 ${amount_usd:.0f} > ${self.initial_capital*0.05:.0f}"

        # 5. 信号质量软检查 (如有)
        if signal_quality:
            wr = signal_quality.get("winrate")
            if wr is not None and wr < 0.30:
                self.n_rejected += 1
                return f"历史胜率过低 {wr:.0%} < 30%"

        self.n_allowed += 1
        return None

    def stats(self) -> dict:
        self._roll_day()
        return {
            "enabled": self.enabled,
            "initial_capital": self.initial_capital,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "daily_loss_limit": round(self.daily_loss_limit, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_loss_breaked": self.daily_loss_breaked(),
            "open_count": self._open_count,
            "max_concurrent": self.max_concurrent,
            "circuit_until": self._circuit_until,
            "circuit_reason": self._last_circuit_reason,
            "consecutive_losses": self._consecutive_losses,
            "n_allowed": self.n_allowed,
            "n_rejected": self.n_rejected,
            "events": self.events[-20:],
        }


def get_shared_risk() -> SharedRiskGate:
    """获取全局共享风控闸门 (单例)"""
    return SharedRiskGate.instance()
