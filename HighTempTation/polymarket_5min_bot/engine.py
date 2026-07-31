"""polymarket_5min_bot — FiveMinEngine 主循环

职责:
  - 市场生命周期管理 (扫描 → 活跃窗口 → 结算 → 轮换)
  - 策略信号汇总 → 执行 (经注入的 executor, 默认直连 CLOB)
  - 持仓管理与结算赎回 (DRY_RUN 下模拟结算: 按价格 >0.5 的一侧判定胜负)
  - 状态暴露 (供 adapter / api_server / 看板)

与 HighTempTation 集成时, executor 由 adapter 注入 (account_manager 门控
+ shared_risk 拦截), 本引擎不感知上层风控, 保持模块独立可运行。
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import List, Optional

from .config import FiveMinBotConfig
from .markets import FiveMinMarket, MarketScanner
from .spot_price import SpotPriceFeed
from .strategies import (ArbitrageStrategy, BaseStrategy, SniperStrategy,
                         MomentumStrategy, LadderStrategy, StairStrategy,
                         StrategyContext, SignalHistory)

logger = logging.getLogger("polymarket_5min.engine")

_ENGINE = None  # 模块级单例 (StairStrategy 等读取持仓)


def get_engine():
    return _ENGINE


class Position:
    """5min 策略持仓 (单腿或双腿)"""

    __slots__ = ("market", "side", "token_id", "entry_price", "shares",
                 "size_usd", "strategy", "entry_time", "is_open",
                 "realized", "exit_price", "exit_time", "exit_reason", "legs")

    def __init__(self, market, side, token_id, entry_price, shares,
                 size_usd, strategy, legs=None):
        self.market = market
        self.side = side
        self.token_id = token_id
        self.entry_price = entry_price
        self.shares = shares
        self.size_usd = size_usd
        self.strategy = strategy
        self.entry_time = datetime.now(timezone.utc).isoformat()
        self.is_open = True
        self.realized = 0.0
        self.exit_price = 0.0
        self.exit_time = ""
        self.exit_reason = ""
        self.legs = legs or []

    def to_dict(self) -> dict:
        return {
            "market_id": self.market.event_id,
            "asset": self.market.asset,
            "side": self.side,
            "token_id": self.token_id,
            "entry_price": round(self.entry_price, 4),
            "shares": round(self.shares, 2),
            "size_usd": round(self.size_usd, 2),
            "strategy": self.strategy,
            "entry_time": self.entry_time,
            "is_open": self.is_open,
            "realized": round(self.realized, 4),
            "exit_reason": self.exit_reason,
            "seconds_left": round(self.market.seconds_left, 1),
            "n_legs": len(self.legs),
        }


class SignalExecutor:
    """默认执行器: 直连 CLOB (子模块独立运行)"""

    def __init__(self, clob, cfg):
        self.clob = clob
        self.cfg = cfg
        self._fired: dict = {}  # market_id -> ts (防同周期重复)

    async def execute(self, signal, engine) -> Optional[Position]:
        now = time.time()
        # 同周期重复信号去重 (冷却由各策略内部 + 此处双保险)
        if now - self._fired.get((signal.market.event_id, signal.strategy), 0.0) < 20:
            return None
        self._fired[(signal.market.event_id, signal.strategy)] = now

        price = signal.price
        shares = signal.size_usd / price if price > 0 else 0.0
        order = await self.clob.place_order(
            market_id=signal.market.yes_market_id if signal.side == "YES"
            else signal.market.no_market_id,
            token_id=signal.token_id, side="BUY", price=price,
            size=shares, strategy=signal.strategy)
        if order.status != "FILLED":
            logger.info(f"  ⛔ {signal.strategy} 信号被拒: {order.status}")
            return None

        pos = Position(signal.market, signal.side, signal.token_id,
                       order.fill_price, order.filled_size,
                       signal.size_usd, signal.strategy, legs=signal.legs)
        engine._positions.append(pos)
        engine.history.record({**signal.to_dict(), "status": "EXECUTED",
                               "fill_price": order.fill_price})

        # 第二腿 (套利/动量互补对冲/Ladder 配对)
        if signal.legs:
            await asyncio.sleep(0.2)
            for leg in signal.legs:
                lpx = leg.get("price", 0.0)
                lsz = leg.get("size_usd", 0.0)
                if lpx <= 0 or lsz <= 0:
                    continue
                lshares = lsz / lpx
                lorder = await self.clob.place_order(
                    market_id=signal.market.no_market_id if leg["side"] == "NO"
                    else signal.market.yes_market_id,
                    token_id=leg["token_id"], side="BUY", price=lpx,
                    size=lshares, strategy=f"{signal.strategy}-LEG2")
                if lorder.status == "FILLED":
                    pos.legs.append({"side": leg["side"], "token_id": leg["token_id"],
                                     "entry_price": lorder.fill_price,
                                     "shares": lorder.filled_size,
                                     "size_usd": lsz})
                else:
                    logger.warning(f"⚠️ {signal.strategy} 第二腿未成交: {lorder.status} "
                                   f"(单腿风险, 结算按胜负判定)")
        return pos


class FiveMinEngine:
    def __init__(self, cfg: Optional[FiveMinBotConfig] = None,
                 clob=None, spot: Optional[SpotPriceFeed] = None,
                 scanner: Optional[MarketScanner] = None,
                 executor: Optional[SignalExecutor] = None):
        global _ENGINE
        self.cfg = cfg or FiveMinBotConfig()
        self.clob = clob
        self.spot = spot or SpotPriceFeed(self.cfg.TARGET_ASSETS)
        self.scanner = scanner or MarketScanner(self.cfg)
        self.executor = executor or SignalExecutor(self.clob, self.cfg)
        self.history = SignalHistory()
        self._positions: List[Position] = []
        self._closed: List[Position] = []
        self._markets: List[FiveMinMarket] = []
        self._strategies: List[BaseStrategy] = [
            ArbitrageStrategy(self.cfg),
            SniperStrategy(self.cfg),
            MomentumStrategy(self.cfg),
            LadderStrategy(self.cfg),
            StairStrategy(self.cfg),
        ]
        self._ctx = StrategyContext(self.clob, self.cfg, self.spot)
        self.last_scan = 0
        self.stats = {"scan_count": 0, "signals": 0, "executed": 0,
                      "rejected": 0, "settled": 0, "wins": 0, "losses": 0}
        _ENGINE = self

    # ── 持仓访问 (Stair 出场策略使用) ──
    def open_positions(self) -> List[dict]:
        out = []
        for p in self._positions:
            if not p.is_open:
                continue
            out.append({"market": p.market, "token_id": p.token_id,
                        "size_usd": p.size_usd, "mid_price": p.entry_price})
        return out

    # ── 生命周期 ──
    async def start(self):
        await self.spot.start()
        await self.scanner.start()
        if self.clob is None:
            from .clob import make_clob_client
            self.clob = make_clob_client(self.cfg)
            self._ctx.clob = self.clob
            self.executor.clob = self.clob
        await self.clob.start()
        # 现货后台轮询
        asyncio.get_event_loop().create_task(self.spot.loop())

    async def stop(self):
        await self.clob.stop()
        await self.scanner.stop()
        await self.spot.stop()

    # ── 单次扫描 ──
    async def scan_once(self):
        self.stats["scan_count"] += 1
        self.last_scan = time.time()

        # 1. 市场轮换: 过期市场标记结算, 拉取新市场
        await self._settle_finished()
        markets = await self.scanner.scan()
        self._markets = [m for m in markets if m.is_live]
        if not self._markets:
            logger.debug("无活跃 5min 市场")
            return

        # 2. 现货价同步到市场 (供动量确认)
        for m in self._markets:
            m.spot_price = self.spot.get(m.asset)

        # 3. 各策略扫描 → 信号
        all_signals = []
        for st in self._strategies:
            try:
                sigs = await st.scan(self._ctx, self._markets)
                if sigs:
                    all_signals.extend(sigs)
            except Exception as e:
                logger.warning(f"策略 {st.name} 扫描异常: {e}")
        self.stats["signals"] += len(all_signals)
        if not all_signals:
            return

        # 4. 执行 (executor 可能被 adapter 包装成共享风控门控)
        for sig in all_signals:
            pos = await self.executor.execute(sig, self)
            if pos:
                self.stats["executed"] += 1
            else:
                self.stats["rejected"] += 1
                self.history.record({**sig.to_dict(), "status": "REJECTED"})

    # ── 结算 ──
    async def _settle_finished(self):
        """检查已结束市场的持仓并结算 (DRY_RUN: 按入场价>0.5 判定胜负)"""
        for p in list(self._positions):
            if not p.is_open:
                continue
            if p.market.is_live:
                continue
            # 结算判定: 组合成本 <1 的持仓必然盈利 (两腿合计赎回 $1)
            combined = p.entry_price + sum(
                l.get("entry_price", 0.0) for l in p.legs)
            if combined <= 0:
                combined = p.entry_price
            win = 1.0 - combined  # 每 $1 组合的收益
            p.realized = p.size_usd + sum(l.get("size_usd", 0.0) for l in p.legs)
            p.realized *= max(win, 0.0) if win > 0 else win
            p.is_open = False
            p.exit_reason = "SETTLED"
            p.exit_time = datetime.now(timezone.utc).isoformat()
            p.exit_price = 1.0
            self._closed.append(p)
            self.stats["settled"] += 1
            self.stats["wins" if win >= 0 else "losses"] += 1
            logger.info(f"🧾 结算 {p.market.event_id} {p.strategy}: "
                        f"组合成本 ${1-win:.3f} → 收益 {win*100:+.1f}%")
        # 清理已结束市场
        self._markets = [m for m in self._markets if m.is_live]

    # ── 主循环 ──
    async def loop(self):
        await self.start()
        logger.info("🚀 FiveMinEngine 启动: " + " | ".join(self.cfg.summarize()))
        try:
            while True:
                try:
                    await self.scan_once()
                except Exception as e:
                    logger.error(f"扫描异常: {e}", exc_info=True)
                await asyncio.sleep(self.cfg.SCAN_INTERVAL_SEC)
        finally:
            await self.stop()

    # ── 状态导出 ──
    def status(self) -> dict:
        return {
            "enabled": True,
            "dry_run": self.cfg.DRY_RUN,
            "scan_interval": self.cfg.SCAN_INTERVAL_SEC,
            "last_scan": self.last_scan,
            "active_markets": [m.to_dict() for m in self._markets[:30]],
            "open_positions": [p.to_dict() for p in self._positions if p.is_open][:50],
            "recent_closed": [p.to_dict() for p in self._closed[-30:]],
            "recent_signals": self.history.recent(50),
            "signal_counts": self.history.count_by_strategy(),
            "stats": self.stats,
            "balance": round(self.clob.get_balance(), 2) if self.clob else 0.0,
        }
