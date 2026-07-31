#!/usr/bin/env python3
"""
adapters/polymarket_5min_adapter.py — Benjam1nCup 5min Bot ↔ HighTempTation 统一适配层
======================================================================================

职责 (对应集成需求 1-7):
  1. 子模块引入: 承载 polymarket_5min_bot (四大策略: 套利/狙击/动量/阶梯)
  2. 统一适配层: 把 5min 策略信号翻译为 HighTempTation 通用信号结构,
     并桥接共享风控 / 统一账户 / 看板数据
  3. 依赖合并: 见 requirements.txt (websockets/ccxt/polymarket-client 可选)
  4. 共享风控: SharedRiskGate (日亏损上限 + 总仓位限制 + 信号质量熔断),
     与天气 Bot 共用同一实例 → 任何一侧超限同时熔断
  5. 统一账户: AccountManager 单例 (全局订单锁 + nonce + 余额记账),
     杜绝两 Bot 并发下单冲突
  6. Streamlit 看板: 暴露 status() → api_server /api/5min → dashboard 标签页
  7. DRY_RUN 验证: 默认 DRY_RUN=true, 见 scripts/verify_5min_integration.py

使用 (bot.py main 集成):
  from adapters.polymarket_5min_adapter import Polymarket5MinAdapter
  _adapter = Polymarket5MinAdapter(cfg) if cfg.PM5_ENABLED else None
  if _adapter:
      _adapter_task = asyncio.create_task(_adapter.run())
"""
import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("polymarket_5min.adapter")

# ── 共享基础设施 (HighTempTation 根目录) ──
_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BOT_DIR not in __import__("sys").path:
    __import__("sys").path.insert(0, _BOT_DIR)

from account_manager import get_account_manager, OrderGateBlocked
from shared_risk import get_shared_risk
from polymarket_5min_bot.config import FiveMinBotConfig
from polymarket_5min_bot.engine import FiveMinEngine, SignalExecutor
from polymarket_5min_bot.strategies.base import Signal


class GuardedExecutor(SignalExecutor):
    """共享风控 + 统一账户门控的信号执行器。

    包装默认 SignalExecutor: 每个信号执行前过 SharedRiskGate 与
    AccountManager, 执行后上报持仓/盈亏/余额, 保证与天气 Bot 共用
    同一套风控与账户。
    """

    def __init__(self, clob, cfg, account=None, risk=None):
        super().__init__(clob, cfg)
        self.account = account or get_account_manager()
        self.risk = risk or get_shared_risk()
        self.blocked: list = []

    async def execute(self, signal: Signal, engine) -> Optional[object]:
        # 1. 共享风控检查 (日亏损/总仓位/质量熔断/单笔上限)
        reason = self.risk.check(
            strategy=f"5min-{signal.strategy}",
            amount_usd=signal.size_usd,
            signal_quality={"winrate": None},
        )
        if reason:
            self.blocked.append({**signal.to_dict(), "reason": reason,
                                 "ts": time.time()})
            self.blocked = self.blocked[-200:]
            logger.info(f"  🛡️ 5min[{signal.strategy}] 被共享风控拦截: {reason}")
            engine.history.record({**signal.to_dict(), "status": "RISK_BLOCKED",
                                   "reason": reason})
            return None

        # 2. 统一账户门控 (全局订单锁 + nonce + 余额) — 整个下单序列持锁
        try:
            async with self.account.order_gate(
                    strategy=f"5min-{signal.strategy}",
                    amount_usd=signal.size_usd):
                pos = await super().execute(signal, engine)
        except OrderGateBlocked as e:
            logger.info(f"  🔒 5min[{signal.strategy}] 账户门控拒绝: {e}")
            engine.history.record({**signal.to_dict(), "status": "ACCOUNT_BLOCKED",
                                   "reason": str(e)})
            return None

        # 3. 执行结果记账
        if pos:
            self.risk.report_open(+1)
            self.account.commit_order(
                nonce=self.account._nonce, status="FILLED",
                amount_usd=pos.size_usd, strategy=f"5min-{signal.strategy}",
                note=signal.reason)
        else:
            self.account.commit_order(
                nonce=self.account._nonce, status="REJECTED",
                amount_usd=0.0, strategy=f"5min-{signal.strategy}")
        return pos


class Polymarket5MinAdapter:
    """5min Bot 集成适配器 (生命周期管理 + 状态桥接)"""

    def __init__(self, cfg=None, weather_engine=None,
                 account=None, risk=None):
        self.cfg = cfg or FiveMinBotConfig()
        self.weather_engine = weather_engine   # 天气 Bot Engine (可选, 用于共享日盈亏)
        self.account = account or get_account_manager()
        self.risk = risk or get_shared_risk()

        # 子模块引擎 (executor 用门控包装)
        from polymarket_5min_bot.clob import make_clob_client
        self.clob = make_clob_client(self.cfg)
        self.engine = FiveMinEngine(self.cfg, clob=self.clob)
        self.engine.executor = GuardedExecutor(self.clob, self.cfg,
                                               self.account, self.risk)
        # 结算回调 → 共享风控: 释放总仓位计数 + 盈亏入账 (日亏损熔断联动)
        self.engine.on_position_settled = self._on_5min_settled

        self._task: Optional[asyncio.Task] = None
        self._weather_last_pnl = 0.0
        self._weather_last_open = 0
        self._sync_interval = 60.0
        self.started = False

    # ── 5min 结算上报 (仓位释放 + 盈亏入账) ──
    def _on_5min_settled(self, win: float):
        """5min 持仓结算回调: 共享风控仓位 -1, 盈亏计入共享日亏损"""
        try:
            self.risk.report_open(-1)
        except Exception as e:
            logger.warning(f"结算释放仓位失败: {e}")
        self.sync_settled_pnl(win)

    # ── 生命周期 ──
    async def start(self):
        if self.started:
            return
        await self.engine.start()
        self.started = True
        logger.info("🤝 5min 适配器就绪: 共享风控/账户/看板已桥接")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.engine.stop()
        self.started = False

    async def run(self):
        """主协程: 引擎循环 + 天气盈亏同步"""
        await self.start()
        try:
            while True:
                try:
                    await self.engine.scan_once()
                    await self._sync_weather_pnl()
                except Exception as e:
                    logger.error(f"5min 适配器扫描异常: {e}", exc_info=True)
                await asyncio.sleep(self.cfg.SCAN_INTERVAL_SEC)
        finally:
            await self.stop()

    def start_background(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """在外部主循环中后台运行"""
        self._task = asyncio.get_event_loop().create_task(self.run())
        return self._task

    # ── 天气 Bot 盈亏共享 (增量上报到 SharedRiskGate) ──
    async def _sync_weather_pnl(self):
        if not self.weather_engine:
            return
        try:
            s = self.weather_engine.summary()
            total = float(s.get("total_pnl", 0.0) or 0.0)
            delta = total - self._weather_last_pnl
            if abs(delta) > 1e-9:
                self._weather_last_pnl = total
                # 增量结转给共享风控 (只在方向变化时记录; 首轮为基线)
                if self._weather_last_pnl != 0.0 or abs(delta) > 0:
                    self.risk.report_pnl(delta)
            # 天气持仓数同步到共享总仓位 (增量对齐, 防漂移)
            w_open = int(s.get("open_count", 0) or 0)
            diff = w_open - self._weather_last_open
            if diff:
                self.risk.report_open(diff)
                self._weather_last_open = w_open
        except Exception as e:
            logger.debug(f"天气盈亏同步失败: {e}")

    # ── 结算盈亏共享 (5min 侧) ──
    def sync_settled_pnl(self, pnl: float):
        """5min 持仓结算后调用, 上报共享日亏损"""
        self.risk.report_pnl(pnl)

    # ── 状态桥接 (api_server /api/5min + Streamlit 看板) ──
    def status(self) -> dict:
        s = self.engine.status() if self.started else {"enabled": False}
        s.update({
            "adapter": True,
            "dry_run": self.cfg.DRY_RUN,
            "shared_risk": self.risk.stats(),
            "account": self.account.status(),
            "guarded_blocked": len(self.engine.executor.blocked)
            if self.started else 0,
        })
        return s

    def toggles(self) -> dict:
        """策略开关状态 (供 api_server /api/strategy/toggle)"""
        return {
            "5min-ARB": self.cfg.ARB_ENABLED,
            "5min-SNIPER": self.cfg.SNIPER_ENABLED,
            "5min-MOMENTUM": self.cfg.MOMENTUM_ENABLED,
            "5min-LADDER": self.cfg.LADDER_ENABLED,
            "5min-STAIR": self.cfg.STAIR_ENABLED,
        }

    def set_toggle(self, name: str, enabled: bool) -> bool:
        """运行时开关策略 (热更新)"""
        mapping = {
            "5min-ARB": "ARB_ENABLED",
            "5min-SNIPER": "SNIPER_ENABLED",
            "5min-MOMENTUM": "MOMENTUM_ENABLED",
            "5min-LADDER": "LADDER_ENABLED",
            "5min-STAIR": "STAIR_ENABLED",
        }
        key = mapping.get(name)
        if not key:
            return False
        setattr(self.cfg, key, enabled)
        return True


def make_adapter(cfg=None, weather_engine=None) -> Polymarket5MinAdapter:
    """工厂: 读取 PM5_ENABLED 环境变量, 启用则返回适配器"""
    if os.getenv("PM5_ENABLED", "true").lower() == "true":
        return Polymarket5MinAdapter(cfg, weather_engine)
    return None
