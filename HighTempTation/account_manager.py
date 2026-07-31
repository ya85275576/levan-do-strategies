#!/usr/bin/env python3
"""
HighTempTation — 统一账户管理 (跨 Bot 共享)
============================================

问题: 天气 Bot (bot.py) 与 5 分钟 Bot (polymarket_5min_bot) 若各自
      独立管理余额 / nonce / 下单, 会出现:
        - 并发下单 nonce 冲突 (CLOB 拒绝)
        - 余额双重计算 (两个引擎各认为余额充足, 实际共享一个钱包)
        - 日亏损上限被绕过 (每个 bot 各算各的)

方案: AccountManager 单例 (模块级 _INSTANCE), 所有策略/引擎下单前
      必须通过 acquire() 获取全局订单锁:
        - asyncio.Lock: 同一时刻只有一个订单在飞 (防 nonce 冲突)
        - nonce 全局递增: 每个订单唯一编号 (实盘 CLOB nonce)
        - 余额统一记账: initial_capital 只扣一次
        - 日亏损熔断: 与 shared_risk.SharedRiskGate 联动

用法 (适配层注入):
  from account_manager import get_account_manager
  am = get_account_manager(initial_capital=cfg.INITIAL_CAPITAL)
  async with am.order_gate(strategy="5min-ARB", amount_usd=10.0):
      order = await clob.place_order(...)
"""
import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("account_manager")

# 模块级单例 (多进程各自独立; 同一进程内天气+5min 共享)
_INSTANCE: Optional["AccountManager"] = None
_INSTANCE_LOCK = threading.Lock()


class AccountManager:
    def __init__(self, initial_capital: float = 10000.0,
                 max_daily_loss_pct: float = 0.05,
                 max_concurrent: int = 30):
        self.initial_capital = initial_capital
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_concurrent = max_concurrent

        # 订单锁 + nonce (防并发冲突)
        self._lock = asyncio.Lock()
        self._nonce = 0

        # 记账
        self.balance = initial_capital
        self.committed = 0.0          # 在途订单占用资金
        self._daily_pnl = 0.0
        self._pnl_day = datetime.now(timezone.utc).date().isoformat()

        # 统计
        self.stats = {
            "orders_total": 0, "orders_filled": 0, "orders_rejected": 0,
            "gates_acquired": 0, "gates_blocked": 0,
        }
        self.last_orders: list = []
        self._maxlen = 200

    # ── 单例构造 ──
    @classmethod
    def instance(cls) -> "AccountManager":
        global _INSTANCE
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = cls(
                    initial_capital=float(os.getenv("INITIAL_CAPITAL", "10000")),
                    max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05")),
                    max_concurrent=int(os.getenv("MAX_CONCURRENT", "30")),
                )
            return _INSTANCE

    # ── 订单门控 ──
    @asynccontextmanager
    async def order_gate(self, strategy: str = "", amount_usd: float = 0.0,
                         check_daily_loss: bool = True, check_balance: bool = True):
        """异步上下文管理器: 获取全局订单锁 + 风控检查。

        Yields: 订单 nonce (int)
        Raises: OrderGateBlocked (风控拒绝)
        """
        await self._lock.acquire()
        self.stats["gates_acquired"] += 1
        try:
            if check_daily_loss:
                self._roll_day()
                if self._daily_pnl <= -self.initial_capital * self.max_daily_loss_pct:
                    self.stats["gates_blocked"] += 1
                    raise OrderGateBlocked(
                        f"日亏损熔断: 今日 {self._daily_pnl:+.2f} ≤ "
                        f"-{self.initial_capital * self.max_daily_loss_pct:.0f}")
            if check_balance and amount_usd > 0:
                if self.committed + amount_usd > self.balance:
                    self.stats["gates_blocked"] += 1
                    raise OrderGateBlocked(
                        f"余额不足: 需 ${amount_usd:.0f}, 可用 ${self.balance - self.committed:.0f}")
            self._nonce += 1
            if amount_usd > 0:
                self.committed += amount_usd
            yield self._nonce
        finally:
            self._lock.release()

    # ── 记账 ──
    def commit_order(self, nonce: int, status: str, amount_usd: float,
                     strategy: str = "", note: str = ""):
        """订单完成后记账 (释放占用, 记录统计)"""
        self.stats["orders_total"] += 1
        if status == "FILLED":
            self.stats["orders_filled"] += 1
            self.balance -= amount_usd
        elif status == "REJECTED":
            self.stats["orders_rejected"] += 1
        self.committed = max(0.0, self.committed - amount_usd)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "nonce": nonce, "status": status, "amount_usd": round(amount_usd, 2),
            "strategy": strategy, "note": note,
        }
        self.last_orders.append(rec)
        if len(self.last_orders) > self._maxlen:
            self.last_orders = self.last_orders[-self._maxlen:]

    def record_pnl(self, pnl: float, day: Optional[str] = None):
        """结算盈亏入账 (天气 bot 与 5min bot 都调用 → 共享日亏损)"""
        self._roll_day(force_day=day)
        self._daily_pnl += pnl

    def get_daily_pnl(self) -> float:
        self._roll_day()
        return self._daily_pnl

    def _roll_day(self, force_day: Optional[str] = None):
        today = force_day or datetime.now(timezone.utc).date().isoformat()
        if today != self._pnl_day:
            self._pnl_day = today
            self._daily_pnl = 0.0

    # ── 状态 ──
    def status(self) -> dict:
        self._roll_day()
        return {
            "initial_capital": self.initial_capital,
            "balance": round(self.balance, 2),
            "committed": round(self.committed, 2),
            "available": round(self.balance - self.committed, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_loss_limit": round(self.initial_capital * self.max_daily_loss_pct, 2),
            "daily_loss_breaked": self._daily_pnl <= -self.initial_capital * self.max_daily_loss_pct,
            "max_concurrent": self.max_concurrent,
            "nonce": self._nonce,
            "stats": self.stats,
            "last_orders": self.last_orders[-20:],
        }


class OrderGateBlocked(Exception):
    """订单门控拒绝 (风控)"""
    pass


def get_account_manager() -> AccountManager:
    """获取全局共享账户管理器 (单例)"""
    return AccountManager.instance()
